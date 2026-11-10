"""Offline migration tests use synthetic bytes, never application data."""
from contextlib import contextmanager
import gzip
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


class StorageOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.roots = [self.base / "active", self.base / "backup"]
        for root in self.roots:
            (root / "generations/initial/artifacts").mkdir(parents=True)
        self.payload = b"<!doctype html><p>synthetic fixture</p>" * 16000

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("library.storage_optimization"),
                             "offline optimization library is not implemented")
        return importlib.import_module("library.storage_optimization")

    def artifact(self, root, payload=None, generation="initial"):
        payload = self.payload if payload is None else payload
        digest = hashlib.sha256(payload).hexdigest()
        path = root / "generations" / generation / "artifacts" / digest[:2] / (digest + ".html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def hardlinked_pair(self):
        first = self.artifact(self.roots[0])
        second = self.roots[1] / first.relative_to(self.roots[0])
        second.parent.mkdir(parents=True, exist_ok=True)
        os.link(first, second)
        return first, second

    def artifact_state(self, roots=None):
        state = {}
        for root in self.roots if roots is None else roots:
            for path in (root / "generations").glob("*/artifacts/*/*"):
                info = path.stat()
                state[str(path)] = (path.read_bytes(), info.st_dev, info.st_ino,
                                    info.st_mode, info.st_nlink, info.st_mtime_ns,
                                    getattr(info, "st_file_attributes", 0))
        return state

    @unittest.skipUnless(os.name == "nt", "Windows read-only attribute")
    def test_windows_readonly_input_fails_before_cleanup_or_write(self):
        module = self.module()
        for restore in (False, True):
            with self.subTest(restore=restore):
                roots = [self.base / str(restore) / name for name in ("first", "last")]
                raw = self.artifact(roots[1])
                source = raw
                if restore:
                    source = raw.with_suffix(".html.gz")
                    source.write_bytes(gzip.compress(self.payload, mtime=0))
                    raw.unlink()
                alias = roots[0] / source.relative_to(roots[1])
                alias.parent.mkdir(parents=True)
                os.link(source, alias)
                # A writable residual must not be cleaned before detecting the
                # read-only input elsewhere in the complete inventory.
                stage = alias.with_name(alias.name + ".optimize-tmp")
                stage.write_bytes(b"unfinished writable staging")
                self.artifact(roots[0], b"other valid writable group" * 16000)
                os.chmod(source, stat.S_IREAD)
                self.addCleanup(os.chmod, source, stat.S_IWRITE)
                self.assertTrue(source.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
                before = self.artifact_state(roots)
                for apply in (False, True, True, False):
                    with self.subTest(apply=apply), \
                            patch.object(module, "_remove", wraps=module._remove) as remove, \
                            patch.object(module, "_rewrite_group", wraps=module._rewrite_group) as rewrite:
                        result = module.optimize_artifacts(roots, restore_raw=restore, apply=apply)
                        self.assertEqual(result["failures"], [{"code": "readonly_artifact"}])
                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(result["groups_changed"], 0)
                        self.assertEqual(result["temporaries_removed"], 0)
                        remove.assert_not_called()
                        rewrite.assert_not_called()
                        self.assertEqual(self.artifact_state(roots), before)

    @unittest.skipUnless(os.name == "nt", "Windows read-only recovery attributes")
    def test_windows_readonly_recovery_aliases_fail_before_any_cleanup(self):
        module = self.module()
        for restore in (False, True):
            with self.subTest(restore=restore):
                roots = [self.base / str(restore) / name for name in ("first", "last")]
                raw = self.artifact(roots[1])
                source = raw
                if restore:
                    source = raw.with_suffix(".html.gz")
                    source.write_bytes(gzip.compress(self.payload, mtime=0))
                    raw.unlink()
                alias = roots[0] / source.relative_to(roots[1])
                alias.parent.mkdir(parents=True)
                os.link(source, alias)
                suffix = ".html" if restore else ".html.gz"
                stage = source.with_name(raw.stem + suffix + ".optimize-tmp")
                stage.write_bytes(b"unfinished read-only recovery")
                link = alias.with_name(raw.stem + suffix + ".optimize-link")
                os.link(stage, link)
                writable_source = self.artifact(
                    roots[0], b"earlier writable cleanup group" * 16000, generation="000")
                writable_stage = writable_source.with_name(writable_source.name + ".gz.optimize-tmp")
                writable_stage.write_bytes(b"must not remove this earlier staging file")
                os.chmod(stage, stat.S_IREAD)
                self.addCleanup(os.chmod, stage, stat.S_IWRITE)
                before = self.artifact_state(roots)
                for apply in (False, True, True, False):
                    with self.subTest(apply=apply), \
                            patch.object(module, "_remove", wraps=module._remove) as remove, \
                            patch.object(module, "_rewrite_group", wraps=module._rewrite_group) as rewrite:
                        result = module.optimize_artifacts(roots, restore_raw=restore, apply=apply)
                        self.assertEqual(result["failures"], [{"code": "readonly_artifact"}])
                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(result["groups_changed"], 0)
                        self.assertEqual(result["temporaries_removed"], 0)
                        remove.assert_not_called()
                        rewrite.assert_not_called()
                        self.assertEqual(self.artifact_state(roots), before)

    @unittest.skipUnless(os.name == "posix", "POSIX unlink uses directory permissions")
    def test_posix_readonly_roundtrip_and_recovery_keep_existing_contract(self):
        paths = self.hardlinked_pair()
        os.chmod(paths[0], 0o444)
        original = paths[0].stat()
        stage = paths[0].with_name(paths[0].name + ".gz.optimize-tmp")
        stage.write_bytes(b"unfinished read-only staging")
        os.chmod(stage, 0o444)
        module = self.module()
        for restore in (False, True):
            before = self.artifact_state()
            dry = module.optimize_artifacts(self.roots, restore_raw=restore)
            self.assertEqual(dry["status"], "ok", dry)
            self.assertEqual(self.artifact_state(), before)
            result = module.optimize_artifacts(self.roots, restore_raw=restore, apply=True)
            self.assertEqual(result["status"], "ok", result)
            self.assertFalse(stage.exists())
            for path in paths:
                target = path if restore else path.with_suffix(".html.gz")
                info = target.stat()
                content = target.read_bytes()
                self.assertEqual(content if restore else gzip.decompress(content), self.payload)
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o444)
                self.assertEqual((info.st_uid, info.st_gid), (original.st_uid, original.st_gid))
                self.assertEqual(info.st_mtime_ns, original.st_mtime_ns)
                self.assertEqual(info.st_nlink, 2)
            self.assertEqual(module.optimize_artifacts(
                self.roots, restore_raw=restore, apply=True)["status"], "ok")

    @unittest.skipUnless(os.name == "nt", "Windows read-only CLI boundary")
    def test_windows_readonly_cli_returns_explicit_failure_in_both_modes(self):
        paths = self.hardlinked_pair()
        os.chmod(paths[0], stat.S_IREAD)
        self.addCleanup(os.chmod, paths[0], stat.S_IWRITE)
        before = self.artifact_state()
        for arguments in ((), ("--apply",), ("--restore-raw",), ("--restore-raw", "--apply")):
            with self.subTest(arguments=arguments):
                code, report = self.cli(*arguments)
                self.assertEqual(code, 1)
                self.assertEqual(report["failures"], [{"code": "readonly_artifact"}])
                self.assertEqual(self.artifact_state(), before)

    @contextmanager
    def retained_reader(self, path):
        if os.name != "nt":
            with path.open("rb") as stream:
                def allocation():
                    info = os.fstat(stream.fileno())
                    return {"allocated_bytes": info.st_blocks * 512, "links": info.st_nlink}
                yield allocation
            return
        import ctypes
        from ctypes import wintypes

        class FileStandardInfo(ctypes.Structure):
            _fields_ = [("allocation", ctypes.c_longlong), ("end", ctypes.c_longlong),
                        ("links", wintypes.DWORD), ("delete_pending", wintypes.BOOLEAN),
                        ("directory", wintypes.BOOLEAN)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # GENERIC_READ + FILE_SHARE_READ | WRITE | DELETE, OPEN_EXISTING.
        handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            def allocation():
                info = FileStandardInfo()
                if not kernel.GetFileInformationByHandleEx(
                        handle, 1, ctypes.byref(info), ctypes.sizeof(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                return {"allocated_bytes": info.allocation, "links": info.links,
                        "delete_pending": bool(info.delete_pending)}
            yield allocation
        finally:
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def test_open_delete_reader_tree_reduction_does_not_claim_reclamation(self):
        self.payload = b"bounded synthetic retained-reader artifact\n" * 100000
        paths = self.hardlinked_pair()
        module = self.module()
        real_usage = module.shutil.disk_usage
        observations = []

        def observe(path):
            usage = real_usage(path)
            observations.append(usage.free)
            return usage

        with self.retained_reader(paths[0]) as allocation, \
                patch.object(module.shutil, "disk_usage", side_effect=observe):
            held_before = allocation()
            result = module.optimize_artifacts(self.roots, apply=True)
            held_after = allocation()
            self.assertEqual(result["status"], "ok", result)
            self.assertGreater(held_before["allocated_bytes"], 0)
            self.assertEqual(held_after["allocated_bytes"], held_before["allocated_bytes"])
            self.assertEqual(held_after["links"], 0)
            if os.name == "nt":
                self.assertTrue(held_after["delete_pending"])
            for path in paths:
                self.assertFalse(path.exists())
                self.assertEqual(gzip.decompress(path.with_suffix(".html.gz").read_bytes()), self.payload)
            self.assertNotIn("reclaimed_allocated_bytes", result)
            self.assertEqual(result["tree_allocated_bytes_reduction"],
                             result["before"]["allocated_bytes"] - result["after"]["allocated_bytes"])
            self.assertGreater(result["tree_allocated_bytes_reduction"], 0)
            self.assertEqual(len(observations), 3)
            self.assertEqual(len(result["filesystems"]), 1)
            self.assertEqual(result["filesystems"][0]["free_before"], observations[0])
            self.assertEqual(result["filesystems"][0]["free_after"], observations[-1])
            self.assertEqual(result["filesystems"][0]["free_delta"], observations[-1] - observations[0])
            # Do not compare free_delta to the tree delta: unrelated allocation
            # can change free space. The still-allocated, zero-link handle is
            # direct proof that namespace removal need not release capacity.

    def test_filesystem_free_observations_deduplicate_shared_roots(self):
        paths = self.hardlinked_pair()
        device = paths[0].stat().st_dev
        module = self.module()
        real_usage = module.shutil.disk_usage
        for restore, apply in ((False, False), (False, True), (True, True)):
            observations = []

            def observe(path):
                usage = real_usage(path)
                observations.append((Path(path).stat().st_dev, usage.free))
                return usage

            with self.subTest(restore=restore, apply=apply), \
                    patch.object(module.shutil, "disk_usage", side_effect=observe):
                result = module.optimize_artifacts(self.roots, restore_raw=restore, apply=apply)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(len(observations), result["space_checked_groups"] + 2)
            self.assertEqual({item[0] for item in observations}, {device})
            self.assertEqual(result["filesystems"], [{
                "device": device, "free_before": observations[0][1],
                "free_after": observations[-1][1],
                "free_delta": observations[-1][1] - observations[0][1],
            }])
            self.assertEqual(result["tree_allocated_bytes_reduction"],
                             result["before"]["allocated_bytes"] - result["after"]["allocated_bytes"])
            if not apply:
                self.assertEqual(result["tree_allocated_bytes_reduction"], 0)
            if restore:
                self.assertLess(result["tree_allocated_bytes_reduction"], 0)
            self.assertNotIn(str(self.base), json.dumps(result))

    def test_filesystem_observation_failure_is_not_silenced(self):
        module = self.module()
        real_usage = module.shutil.disk_usage
        for failing_sample in (1, 2):
            for apply in (False, True):
                with self.subTest(failing_sample=failing_sample, apply=apply):
                    root = self.base / f"sample-{failing_sample}-{apply}"
                    path = self.artifact(root)
                    stage = path.with_name(path.name + ".gz.optimize-tmp")
                    stage.write_bytes(b"unfinished staging must survive preflight failure")
                    before = self.artifact_state([root])
                    samples = 0

                    def fail_sample(target):
                        nonlocal samples
                        if Path(target) == root:
                            samples += 1
                            if samples == failing_sample:
                                raise OSError("synthetic free-space query failure")
                        return real_usage(target)

                    with patch.object(module.shutil, "disk_usage", side_effect=fail_sample):
                        result = module.optimize_artifacts([root], apply=apply)
                    self.assertEqual(result["status"], "failed", result)
                    self.assertEqual(result["failures"], [{"code": "io_error"}])
                    self.assertEqual(result["failure_count"], 1)
                    self.assertEqual(samples, 2)
                    self.assertIsNotNone(result["after"])
                    self.assertIsNone(result["filesystems"][0]["free_delta"])
                    missing, present = (("free_before", "free_after") if failing_sample == 1
                                        else ("free_after", "free_before"))
                    self.assertIsNone(result["filesystems"][0][missing])
                    self.assertIsInstance(result["filesystems"][0][present], int)
                    if failing_sample == 1 or not apply:
                        self.assertEqual(result["groups_changed"], 0)
                        self.assertEqual(self.artifact_state([root]), before)
                    else:
                        self.assertEqual(result["groups_changed"], 1)
                        self.assertEqual(gzip.decompress(path.with_suffix(".html.gz").read_bytes()),
                                         self.payload)

    def test_final_filesystem_measurement_interruption_uses_failure_report(self):
        self.hardlinked_pair()
        module = self.module()
        real_usage = module.shutil.disk_usage
        samples = 0

        def interrupt_final_sample(path):
            nonlocal samples
            if Path(path) in self.roots:
                samples += 1
                if samples == 2:
                    raise KeyboardInterrupt()
            return real_usage(path)

        with patch.object(module.shutil, "disk_usage", side_effect=interrupt_final_sample):
            try:
                result = module.optimize_artifacts(self.roots, apply=True)
            except KeyboardInterrupt:
                self.fail("final filesystem measurement escaped the existing error model")
        self.assertEqual(result["failures"], [{"code": "interrupted"}])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["groups_changed"], 1)
        self.assertIsNone(result["filesystems"][0]["free_after"])
        self.assertIsNone(result["filesystems"][0]["free_delta"])
        self.assertEqual(module.optimize_artifacts(self.roots, apply=True)["status"], "ok")

    def test_external_hardlink_is_refused_before_any_write(self):
        first, second = self.hardlinked_pair()
        external = self.base / "not_authorized.html"
        os.link(first, external)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failures"], [{"code": "unauthorized_hardlink"}])
        self.assertEqual(result["groups_changed"], 0)
        self.assertEqual(first.stat().st_nlink, 3)
        for path in (first, second, external):
            self.assertEqual(path.read_bytes(), self.payload)

    def test_default_dry_run_verifies_without_claiming_compression(self):
        paths = self.hardlinked_pair()
        result = self.module().optimize_artifacts(self.roots)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["apply"])
        self.assertEqual(result["before"]["unique_inodes"], 1)
        self.assertEqual(result["before"]["raw_files"], 2)
        self.assertEqual(result["before"]["logical_objects"], 1)
        self.assertEqual(result["before"], result["after"])
        self.assertGreater(result["before"]["allocated_bytes"], 0)
        self.assertEqual(result["sha_verified_files"], 1)
        self.assertEqual(result["sha_verified_aliases"], 2)
        self.assertIsNone(result["estimated_reclaim_bytes"])
        self.assertIsNone(result["estimated_compression_ratio"])
        self.assertEqual(result["groups_changed"], 0)
        for path in paths:
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertFalse(path.with_suffix(".html.gz").exists())

    def test_overlapping_roots_and_parent_traversal_are_refused(self):
        path = self.artifact(self.roots[0])
        for roots in ([self.roots[0], self.roots[0]],
                      [self.roots[0], self.roots[0] / "generations"],
                      [self.roots[0] / "generations/../"]):
            with self.subTest(roots=roots):
                result = self.module().optimize_artifacts(roots, apply=True)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["groups_changed"], 0)
        self.assertEqual(path.read_bytes(), self.payload)

    def test_noncanonical_filename_is_refused(self):
        path = self.artifact(self.roots[0])
        malformed = path.with_name("unexpected.html")
        path.rename(malformed)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["failures"], [{"code": "noncanonical_artifact"}])
        self.assertTrue(malformed.exists())

    def test_optimizer_uses_shared_capture_size_limit(self):
        capture_models = importlib.import_module("capture.models")
        path = self.artifact(self.roots[0], b"limit fixture" * 200)
        module = self.module()
        try:
            with patch.object(capture_models, "MAX_CAPTURE_BYTES", 1024):
                importlib.reload(module)
                result = module.optimize_artifacts(self.roots, apply=True)
                self.assertEqual(result["failures"], [{"code": "size_limit"}])
                self.assertEqual(result["groups_changed"], 0)
                self.assertTrue(path.is_file())
        finally:
            importlib.reload(module)

    def test_root_inode_alias_is_rejected_even_when_spellings_do_not_overlap(self):
        self.artifact(self.roots[0])
        module = self.module()
        real_safe = module._safe_path

        def alias_info(path, **kwargs):
            # Models a short-name or bind-mount alias, not a symlink.
            return real_safe(self.roots[0] if path == self.roots[1] else path, **kwargs)

        with patch.object(module, "_safe_path", side_effect=alias_info):
            result = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["failures"], [{"code": "overlapping_roots"}])
        self.assertEqual(result["groups_changed"], 0)

    def test_orphan_staging_and_external_staging_links_fail_closed(self):
        path = self.artifact(self.roots[0])
        stage = path.with_name(path.name + ".gz.optimize-tmp")
        stage.write_bytes(b"unfinished")
        external = self.base / "unscanned"
        os.link(stage, external)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["failures"], [{"code": "unauthorized_hardlink"}])
        self.assertTrue(stage.exists())
        external.unlink()
        path.unlink()
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["failures"], [{"code": "orphan_temporary"}])
        self.assertEqual(stage.read_bytes(), b"unfinished")

    def test_symlink_is_refused_without_following_it(self):
        path = self.artifact(self.roots[0])
        link = self.roots[1] / path.relative_to(self.roots[0])
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(path)
        except OSError as error:
            self.skipTest("Host cannot create symlinks: " + str(error))
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["failures"], [{"code": "unsafe_path"}])
        self.assertTrue(link.is_symlink())
        self.assertEqual(path.read_bytes(), self.payload)

    def test_corrupt_raw_and_existing_gzip_are_not_deleted(self):
        for encoding in ("raw", "gzip"):
            with self.subTest(encoding=encoding):
                path = self.artifact(self.roots[0])
                gz = path.with_suffix(".html.gz")
                if encoding == "raw":
                    path.write_bytes(b"wrong original")
                else:
                    gz.write_bytes(gzip.compress(self.payload, mtime=0)[:-5])
                result = self.module().optimize_artifacts(self.roots, apply=True)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["groups_changed"], 0)
                self.assertTrue(path.exists())
                if gz.exists():
                    self.assertEqual(gz.read_bytes(), gzip.compress(self.payload, mtime=0)[:-5])
                    gz.unlink()

    def test_valid_gzip_is_verified_as_original_bytes(self):
        path = self.artifact(self.roots[0])
        path.with_suffix(".html.gz").write_bytes(gzip.compress(self.payload, mtime=0))
        path.unlink()
        result = self.module().optimize_artifacts(self.roots)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["before"]["gzip_files"], 1)
        self.assertEqual(result["sha_verified_files"], 1)

    def test_apply_compresses_all_authorized_aliases_to_shared_inode(self):
        paths = self.hardlinked_pair()
        before = paths[0].stat()
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["groups_changed"], 1)
        compressed = [p.with_suffix(".html.gz") for p in paths]
        for original, encoded in zip(paths, compressed):
            self.assertFalse(original.exists())
            self.assertEqual(gzip.decompress(encoded.read_bytes()), self.payload)
            info = encoded.stat()
            self.assertEqual(stat.S_IMODE(info.st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual((info.st_uid, info.st_gid), (before.st_uid, before.st_gid))
            self.assertEqual(info.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(compressed[0].stat().st_ino, compressed[1].stat().st_ino)
        self.assertEqual(compressed[0].stat().st_nlink, 2)
        self.assertEqual(result["after"]["raw_files"], 0)
        self.assertEqual(result["after"]["gzip_files"], 2)
        self.assertEqual(result["after"]["unique_inodes"], 1)
        self.assertGreater(result["tree_allocated_bytes_reduction"], 0)
        again = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(again["status"], "ok", again)
        self.assertEqual(again["groups_changed"], 0)
        self.assertEqual(again["skips"], {"already_compressed": 1})

    def test_equal_bytes_in_distinct_inodes_are_deduplicated(self):
        paths = [self.artifact(root) for root in self.roots]
        self.assertNotEqual(paths[0].stat().st_ino, paths[1].stat().st_ino)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["before"]["unique_inodes"], 2)
        self.assertEqual(result["after"]["unique_inodes"], 1)
        for path in paths:
            self.assertEqual(gzip.decompress(path.with_suffix(".html.gz").read_bytes()), self.payload)

    def test_low_space_stops_before_creating_compressed_files(self):
        paths = self.hardlinked_pair()
        with patch("shutil.disk_usage", return_value=type("Usage", (), {"free": 1})()):
            result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["failures"], [{"code": "insufficient_space"}])
        self.assertEqual(result["before"], result["after"])
        for path in paths:
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(path.read_bytes(), self.payload)

    def test_no_compression_gain_preserves_raw(self):
        path = self.artifact(self.roots[0], payload=b"tiny")
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["skips"], {"no_space_gain": 1})
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_publication_interruption_preserves_raw_and_rerun_recovers_staging(self):
        paths = self.hardlinked_pair()
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated publication interruption")
            real_replace(source, target)

        with patch.object(self.module().os, "replace", side_effect=fail_second):
            failed = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(failed["status"], "failed")
        self.assertIsNotNone(failed["after"], failed)
        self.assertGreater(failed["after"]["temporary_files"], 0)
        for path in paths:
            self.assertEqual(path.read_bytes(), self.payload)
        dry = self.module().optimize_artifacts(self.roots)
        self.assertEqual(dry["status"], "ok", dry)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["after"]["unique_inodes"], 1)
        self.assertEqual(result["after"]["temporary_files"], 0)
        for path in paths:
            self.assertFalse(path.exists())
            self.assertEqual(gzip.decompress(path.with_suffix(".html.gz").read_bytes()), self.payload)

    def test_deletion_interruption_keeps_every_alias_readable_and_resumes(self):
        paths = self.hardlinked_pair()
        module = self.module()
        real_remove = module._remove
        deleted = []

        def fail_after_raw_delete(path):
            if path.suffix == ".html":
                for original in paths:
                    self.assertTrue(original.with_suffix(".html.gz").exists())
                real_remove(path)
                deleted.append(path)
                raise OSError("simulated deletion interruption")
            real_remove(path)

        with patch.object(module, "_remove", side_effect=fail_after_raw_delete):
            failed = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(len(deleted), 1)
        for path in paths:
            self.assertEqual(gzip.decompress(path.with_suffix(".html.gz").read_bytes()), self.payload)
        result = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["after"]["raw_files"], 0)
        self.assertEqual(result["after"]["unique_inodes"], 1)

    def test_abandoned_partial_temp_is_only_removed_on_apply(self):
        path = self.artifact(self.roots[0])
        stage = path.with_name(path.name + ".gz.optimize-tmp")
        stage.write_bytes(b"unfinished gzip")
        dry = self.module().optimize_artifacts(self.roots)
        self.assertEqual(dry["status"], "ok", dry)
        self.assertEqual(stage.read_bytes(), b"unfinished gzip")
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertFalse(stage.exists())
        self.assertEqual(gzip.decompress(path.with_suffix(".html.gz").read_bytes()), self.payload)

    def test_restore_raw_recovers_exact_bytes_and_shared_inode(self):
        paths = self.hardlinked_pair()
        before = paths[0].stat()
        module = self.module()
        self.assertEqual(module.optimize_artifacts(self.roots, apply=True)["status"], "ok")
        dry = module.optimize_artifacts(self.roots, restore_raw=True)
        self.assertEqual(dry["mode"], "restore_raw")
        self.assertEqual(dry["before"], dry["after"])
        self.assertFalse(paths[0].exists())
        result = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["after"]["gzip_files"], 0)
        self.assertEqual(result["after"]["raw_files"], 2)
        self.assertEqual(result["after"]["unique_inodes"], 1)
        self.assertEqual(paths[0].stat().st_ino, paths[1].stat().st_ino)
        self.assertEqual(paths[0].stat().st_nlink, 2)
        self.assertEqual(paths[0].stat().st_mtime_ns, before.st_mtime_ns)
        for path in paths:
            self.assertEqual(path.read_bytes(), self.payload)
        again = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(again["skips"], {"already_raw": 1})

    def test_restore_raw_low_space_keeps_all_compressed_aliases(self):
        self.hardlinked_pair()
        module = self.module()
        self.assertEqual(module.optimize_artifacts(self.roots, apply=True)["status"], "ok")
        with patch("shutil.disk_usage", return_value=type("Usage", (), {"free": 1})()):
            result = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(result["failures"], [{"code": "insufficient_space"}])
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(result["after"]["gzip_files"], 2)

    def test_restore_publication_interruption_can_resume(self):
        paths = self.hardlinked_pair()
        module = self.module()
        self.assertEqual(module.optimize_artifacts(self.roots, apply=True)["status"], "ok")
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated restore publication interruption")
            real_replace(source, target)

        with patch.object(module.os, "replace", side_effect=fail_second):
            failed = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(failed["status"], "failed", failed)
        for path in paths:
            self.assertEqual(gzip.decompress(path.with_suffix(".html.gz").read_bytes()), self.payload)
        result = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["after"]["unique_inodes"], 1)
        self.assertEqual(result["after"]["gzip_files"], 0)
        self.assertEqual(result["after"]["temporary_files"], 0)

    def test_restore_deletion_interruption_preserves_complete_raw_aliases(self):
        paths = self.hardlinked_pair()
        module = self.module()
        self.assertEqual(module.optimize_artifacts(self.roots, apply=True)["status"], "ok")
        real_remove = module._remove

        def fail_after_gzip_delete(path):
            if path.suffix == ".gz":
                for original in paths:
                    self.assertEqual(original.read_bytes(), self.payload)
                real_remove(path)
                raise OSError("simulated restore deletion interruption")
            real_remove(path)

        with patch.object(module, "_remove", side_effect=fail_after_gzip_delete):
            failed = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(failed["status"], "failed", failed)
        result = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["after"]["gzip_files"], 0)
        self.assertEqual(result["after"]["unique_inodes"], 1)

    def test_real_child_exit_mid_publication_leaves_resumable_files(self):
        paths = self.hardlinked_pair()
        script = """
import os, sys
from library import storage_optimization as module
real = module._publish
def crash(source, target):
    real(source, target)
    os._exit(86)
module._publish = crash
module.optimize_artifacts(sys.argv[1:], apply=True)
"""
        child = subprocess.run([sys.executable, "-c", script, *map(str, self.roots)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 86, child.stderr)
        for path in paths:
            self.assertEqual(path.read_bytes(), self.payload)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["after"]["unique_inodes"], 1)
        self.assertEqual(result["after"]["temporary_files"], 0)

    def test_group_at_a_time_space_failure_keeps_remaining_objects(self):
        paths = [self.artifact(self.roots[0], payload=value * 16000)
                 for value in (b"first synthetic object", b"second synthetic object")]
        by_sha = sorted(paths, key=lambda path: path.name)
        calls = 0

        module = self.module()
        real_usage = module.shutil.disk_usage

        def free_space(_path):
            nonlocal calls
            if Path(_path) in self.roots:
                # Inventory observations do not fund subsequent group checks.
                return real_usage(_path)
            calls += 1
            if calls == 2:
                self.assertFalse(by_sha[0].exists())
                self.assertTrue(by_sha[0].with_suffix(".html.gz").exists())
            return type("Usage", (), {"free": 1024 ** 3 if calls == 1 else 1})()

        with patch("shutil.disk_usage", side_effect=free_space):
            result = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(calls, 2)
        self.assertEqual(result["failures"], [{"code": "insufficient_space"}])
        self.assertEqual(result["groups_changed"], 1)
        self.assertEqual(result["after"]["raw_files"], 1)
        self.assertEqual(result["after"]["gzip_files"], 1)
        self.assertTrue(by_sha[1].exists())

    def test_all_generations_are_scanned(self):
        first, second = self.hardlinked_pair()
        historical = self.roots[0] / "generations/history" / first.relative_to(self.roots[0] / "generations/initial")
        historical.parent.mkdir(parents=True)
        os.link(first, historical)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["before"]["raw_files"], 3)
        self.assertEqual(result["after"]["gzip_files"], 3)
        for path in (first, second, historical):
            self.assertEqual(path.with_suffix(".html.gz").stat().st_nlink, 3)

    def test_explicit_hardlinked_supervisor_locks_are_one_physical_lock(self):
        self.hardlinked_pair()
        for root in self.roots:
            (root / "runtime").mkdir()
        first = self.roots[0] / "runtime/supervisor.lock"
        first.write_bytes(b"0")
        os.link(first, self.roots[1] / "runtime/supervisor.lock")
        result = self.module().optimize_artifacts(self.roots)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["locked_data_roots"], 2)
        self.assertEqual(result["physical_locks"], 1)

    def test_cleanup_does_not_refresh_away_validated_inode_identity(self):
        path = self.artifact(self.roots[0])
        gz = path.with_suffix(".html.gz")
        gz.write_bytes(gzip.compress(self.payload, mtime=0))
        path.unlink()
        stage = path.with_name(path.name + ".gz.optimize-tmp")
        stage.write_bytes(b"unfinished")
        module = self.module()
        real_remove = module._remove

        def change_after_cleanup(target):
            real_remove(target)
            if target == stage:
                gz.write_bytes(b"corrupted during cleanup")

        with patch.object(module, "_remove", side_effect=change_after_cleanup):
            result = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["failures"], [{"code": "concurrent_change"}])

    def test_existing_gzip_changed_after_validation_never_replaces_raw(self):
        path = self.artifact(self.roots[0])
        gz = path.with_suffix(".html.gz")
        gz.write_bytes(gzip.compress(self.payload, mtime=0))
        module = self.module()
        real_check = module._space_check

        def corrupt_after_check(*args, **kwargs):
            real_check(*args, **kwargs)
            gz.write_bytes(b"corrupted after initial validation")

        with patch.object(module, "_space_check", side_effect=corrupt_after_check):
            result = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(path.read_bytes(), self.payload)

    def test_hardlink_added_during_preparation_stops_before_raw_deletion(self):
        path = self.artifact(self.roots[0])
        module = self.module()
        real_check = module._space_check
        external = self.base / "outside.html"

        def add_external_after_check(*args, **kwargs):
            real_check(*args, **kwargs)
            os.link(path, external)

        with patch.object(module, "_space_check", side_effect=add_external_after_check):
            result = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "failed", result)
        self.assertTrue(path.exists())
        self.assertEqual(external.read_bytes(), self.payload)

    def test_dry_run_checks_group_space_without_allocating_it(self):
        self.hardlinked_pair()
        with patch("shutil.disk_usage", return_value=type("Usage", (), {"free": 1})()):
            result = self.module().optimize_artifacts(self.roots)
        self.assertEqual(result["failures"], [{"code": "insufficient_space"}])
        self.assertEqual(result["before"], result["after"])
        self.assertIsNone(result["estimated_reclaim_bytes"])

    def test_existing_gzip_keeps_original_raw_timestamp(self):
        path = self.artifact(self.roots[0])
        os.utime(path, ns=(1600000000000000000, 1600000000000000000))
        original = path.stat()
        gz = path.with_suffix(".html.gz")
        gz.write_bytes(gzip.compress(self.payload, mtime=0))
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(gz.stat().st_mtime_ns, original.st_mtime_ns)

    @unittest.skipUnless(os.name == "posix", "POSIX modes and ownership")
    def test_original_posix_permissions_and_ownership_survive_roundtrip(self):
        paths = self.hardlinked_pair()
        os.chmod(paths[0], 0o640)
        if os.geteuid() == 0:
            os.chown(paths[0], 12345, 23456)
        original = paths[0].stat()
        module = self.module()
        result = module.optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["status"], "ok", result)
        encoded = paths[0].with_suffix(".html.gz").stat()
        self.assertEqual(stat.S_IMODE(encoded.st_mode), 0o640)
        self.assertEqual((encoded.st_uid, encoded.st_gid), (original.st_uid, original.st_gid))
        result = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
        self.assertEqual(result["status"], "ok", result)
        restored = paths[0].stat()
        self.assertEqual(stat.S_IMODE(restored.st_mode), 0o640)
        self.assertEqual((restored.st_uid, restored.st_gid), (original.st_uid, original.st_gid))

    @unittest.skipUnless(os.name == "posix", "POSIX staging confidentiality")
    def test_staging_never_exposes_private_original_permissions(self):
        path = self.artifact(self.roots[0])
        os.chmod(path, 0o600)
        module = self.module()
        real_verify = module._verify

        def check_staging(entry, sink=None):
            if sink is not None:
                temporary = list(path.parent.glob("*.optimize-tmp"))
                self.assertEqual(len(temporary), 1)
                self.assertEqual(stat.S_IMODE(temporary[0].stat().st_mode) & 0o077, 0)
            return real_verify(entry, sink)

        old_mask = os.umask(0o022)
        try:
            with patch.object(module, "_verify", side_effect=check_staging):
                self.assertEqual(module.optimize_artifacts(self.roots, apply=True)["status"], "ok")
                result = module.optimize_artifacts(self.roots, restore_raw=True, apply=True)
                self.assertEqual(result["status"], "ok", result)
        finally:
            os.umask(old_mask)

    def test_keyboard_interruption_returns_failure_report_and_can_resume(self):
        paths = self.hardlinked_pair()
        module = self.module()
        real_publish = module._publish

        def interrupt(source, target):
            real_publish(source, target)
            raise KeyboardInterrupt()

        with patch.object(module, "_publish", side_effect=interrupt):
            try:
                result = module.optimize_artifacts(self.roots, apply=True)
            except KeyboardInterrupt:
                self.fail("interruption did not return the aggregate report")
        self.assertEqual(result["failures"], [{"code": "interrupted"}])
        for path in paths:
            self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(module.optimize_artifacts(self.roots, apply=True)["status"], "ok")

    @unittest.skipUnless(os.name == "posix", "POSIX permission conflict")
    def test_incompatible_permissions_are_not_silently_deduplicated(self):
        paths = [self.artifact(root) for root in self.roots]
        os.chmod(paths[0], 0o600)
        os.chmod(paths[1], 0o644)
        result = self.module().optimize_artifacts(self.roots, apply=True)
        self.assertEqual(result["failures"], [{"code": "metadata_conflict"}])
        for path in paths:
            self.assertEqual(path.read_bytes(), self.payload)

    @unittest.skipUnless(os.environ.get("ARTIFACT_SECOND_FILESYSTEM"), "second filesystem not configured")
    def test_same_sha_on_different_filesystems_stays_in_separate_groups(self):
        with tempfile.TemporaryDirectory(dir=os.environ["ARTIFACT_SECOND_FILESYSTEM"]) as other:
            second_root = Path(other) / "data"
            first = self.artifact(self.roots[0])
            second = self.artifact(second_root)
            self.assertNotEqual(first.stat().st_dev, second.stat().st_dev)
            module = self.module()
            real_usage = module.shutil.disk_usage
            devices = {first.stat().st_dev, second.stat().st_dev}
            # Two authorized roots share one FS; a third root uses a real
            # different FS. Both directions report exactly one row per device.
            for restore in (False, True):
                observations = {device: [] for device in devices}

                def observe(path):
                    usage = real_usage(path)
                    observations[Path(path).stat().st_dev].append(usage.free)
                    return usage

                with patch.object(module.shutil, "disk_usage", side_effect=observe):
                    result = module.optimize_artifacts(
                        [*self.roots, second_root], restore_raw=restore, apply=True)
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["groups_changed"], 2)
                self.assertEqual(result["after"]["unique_inodes"], 2)
                self.assertEqual(result["filesystems"], [
                    {"device": device, "free_before": observations[device][0],
                     "free_after": observations[device][-1],
                     "free_delta": observations[device][-1] - observations[device][0]}
                    for device in sorted(devices)
                ])
                for samples in observations.values():
                    self.assertEqual(len(samples), 3)
                if restore:
                    self.assertLess(result["tree_allocated_bytes_reduction"], 0)
                for path in (first, second):
                    target = path if restore else path.with_suffix(".html.gz")
                    content = target.read_bytes()
                    self.assertEqual(content if restore else gzip.decompress(content), self.payload)

    def cli(self, *arguments):
        script = Path(__file__).resolve().parents[2] / "scripts/optimize-artifacts.py"
        self.assertTrue(script.is_file(), "offline CLI is not implemented")
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        command = [sys.executable, str(script)]
        for root in self.roots:
            command += ["--data-root", str(root)]
        command += list(arguments)
        completed = subprocess.run(command, capture_output=True, text=True,
                                   env=environment, timeout=30)
        self.assertEqual(completed.stderr, "")
        return completed.returncode, json.loads(completed.stdout)

    def test_cli_defaults_dry_run_applies_and_restores_without_pythonpath(self):
        paths = self.hardlinked_pair()
        sentinels = []
        for root in self.roots:
            for name in ("library.sqlite3", "manifest.json"):
                sentinel = root / "generations/initial" / name
                sentinel.write_bytes(b"opaque synthetic fixture: " + name.encode())
                sentinels.append((sentinel, sentinel.read_bytes()))
        code, dry = self.cli()
        self.assertEqual(code, 0)
        self.assertFalse(dry["apply"])
        self.assertTrue(paths[0].exists())
        code, applied = self.cli("--apply", "--safety-margin-mib", "64")
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["after"]["gzip_files"], 2)
        code, restored = self.cli("--restore-raw", "--apply")
        self.assertEqual(code, 0, restored)
        self.assertEqual(restored["after"]["gzip_files"], 0)
        for path, content in sentinels:
            self.assertEqual(path.read_bytes(), content)
        self.assertNotIn(str(self.base), json.dumps(restored))
        self.assertNotIn("synthetic fixture", json.dumps(restored))

    def test_cli_reports_failure_and_nonzero_exit(self):
        path = self.artifact(self.roots[0])
        path.write_bytes(b"wrong bytes")
        code, result = self.cli("--apply")
        self.assertEqual(code, 1)
        self.assertEqual(result["failures"], [{"code": "sha_mismatch"}])
        self.assertTrue(path.exists())

    def test_each_supervisor_lock_conflicts_with_real_child_process(self):
        self.hardlinked_pair()
        script = ("import sys; from pathlib import Path; "
                  "from runtime.instance_lock import InstanceLock; "
                  "lock = InstanceLock(Path(sys.argv[1])/'runtime/supervisor.lock'); "
                  "lock.__enter__(); print('ready', flush=True); "
                  "sys.stdin.readline(); lock.__exit__(None,None,None)")
        for root in self.roots:
            with self.subTest(root=root):
                child = subprocess.Popen([sys.executable, "-c", script, str(root)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    for apply in (False, True):
                        result = self.module().optimize_artifacts(self.roots, apply=apply)
                        self.assertEqual(result["failures"], [{"code": "lock_conflict"}])
                        self.assertEqual(result["groups_changed"], 0)
                finally:
                    child.communicate("\n", timeout=10)
                self.assertEqual(child.returncode, 0)
        self.assertEqual(self.module().optimize_artifacts(self.roots)["status"], "ok")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junction_root_is_refused(self):
        self.artifact(self.roots[0])
        junction = self.base / "junction"
        made = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                               str(junction), str(self.roots[0])],
                              capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        result = self.module().optimize_artifacts([junction], apply=True)
        self.assertEqual(result["failures"], [{"code": "unsafe_path"}])

    def test_limit_is_enforced_for_raw_and_expanded_gzip(self):
        for encoding in ("raw", "gzip"):
            with self.subTest(encoding=encoding):
                path = self.artifact(self.roots[0])
                if encoding == "gzip":
                    path.with_suffix(".html.gz").write_bytes(gzip.compress(self.payload))
                    path.unlink()
                with patch.object(self.module(), "MAX_ARTIFACT_BYTES", 1024):
                    result = self.module().optimize_artifacts(self.roots, apply=True)
                self.assertEqual(result["failures"], [{"code": "size_limit"}])


if __name__ == "__main__":
    unittest.main()
