"""The storage boundary is usable without Django or a database."""
import gzip
import hashlib
import importlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class ArtifactStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def storage(self):
        self.assertIsNotNone(importlib.util.find_spec("library.artifact_storage"),
                             "The representation-independent artifact storage boundary is missing")
        return importlib.import_module("library.artifact_storage")

    def canonical(self, data, root=None):
        digest = hashlib.sha256(data).hexdigest()
        return (root or self.root) / "artifacts" / digest[:2] / f"{digest}.html"

    def test_compressible_html_roundtrips_with_deterministic_gzip(self):
        storage = self.storage()
        data = b"<!doctype html>\r\n" + "原始 HTML".encode() * 10000 + b"\x00\xff"
        source = self.root / "capture.html"
        source.write_bytes(data)
        target = self.canonical(data)
        storage.write_artifact(source, target)
        physical = storage.storage_path(target)
        self.assertEqual(physical, Path(str(target) + ".gz"))
        self.assertFalse(target.exists())
        self.assertLess(physical.stat().st_size, len(data))
        self.assertEqual(gzip.decompress(physical.read_bytes()), data)
        self.assertEqual(physical.read_bytes()[3:8], b"\x00" * 5)  # no filename, mtime=0
        with storage.open_artifact(target, expected_size=len(data)) as stream:
            self.assertFalse(stream.seekable())
            self.assertFalse(getattr(stream, "name", ""))
            self.assertEqual(stream.read(7) + stream.read(), data)
        self.assertTrue(stream.closed)
        self.assertEqual(storage.read_artifact(target), data)
        duplicate = self.canonical(data, self.root / "another")
        storage.write_artifact(source, duplicate)
        self.assertEqual(storage.storage_path(duplicate).read_bytes(), physical.read_bytes())
        self.assertEqual(source.read_bytes(), data)

    def test_reader_rejects_invalid_content_and_closes_on_failure(self):
        storage = self.storage()
        data = b"logical HTML" * 1000
        target = self.canonical(data)
        target.parent.mkdir(parents=True)
        encoded = gzip.compress(data, mtime=0)
        bad_crc = bytearray(encoded)
        bad_crc[-8] ^= 1
        for name, payload, compressed in (
            ("sha256", b"other bytes", False),
            ("gzip-sha256", gzip.compress(b"other bytes"), True),
            ("truncated", encoded[:-5], True),
            ("crc", bytes(bad_crc), True),
            ("trailing-garbage", encoded + b"garbage", True),
        ):
            with self.subTest(name=name):
                physical = Path(str(target) + ".gz") if compressed else target
                physical.write_bytes(payload)
                stream = storage.open_artifact(target)
                try:
                    with self.assertRaises(storage.ArtifactStorageError):
                        stream.read()
                    self.assertTrue(stream.closed)
                finally:
                    stream.close()
                    physical.unlink()

    def test_reader_enforces_logical_size_for_raw_gzip_and_members(self):
        storage = self.storage()
        data = b"x" * 257
        target = self.canonical(data)
        target.parent.mkdir(parents=True)
        for encoding in ("raw", "gzip", "members"):
            with self.subTest(encoding=encoding):
                physical = target if encoding == "raw" else Path(str(target) + ".gz")
                physical.write_bytes(data if encoding == "raw" else
                    gzip.compress(data[:128]) + gzip.compress(data[128:]) if encoding == "members" else gzip.compress(data))
                with patch.object(storage, "MAX_CAPTURE_BYTES", 256):
                    with self.assertRaises(storage.ArtifactStorageError):
                        storage.read_artifact(target)
                for expected in (256, 258, -1, True):
                    with self.subTest(expected=expected), self.assertRaises(storage.ArtifactStorageError):
                        storage.read_artifact(target, expected_size=expected)
                physical.unlink()

    def test_legacy_raw_is_read_in_chunks_and_verified_at_eof(self):
        storage = self.storage()
        data = b"unchanged legacy HTML\xff\x00"
        target = self.canonical(data)
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        self.assertEqual(storage.storage_path(target), target)
        with storage.open_artifact(target, expected_size=len(data)) as stream:
            self.assertEqual(stream.read(0), b"")
            with self.assertRaises(io.UnsupportedOperation):
                stream.seek(0)
            self.assertEqual(stream.read(5) + stream.read(), data)
            self.assertEqual(stream.read(), b"")
        target.write_bytes(data[:-1] + b"!")
        with storage.open_artifact(target) as stream:
            self.assertEqual(stream.read(5), data[:5])
            with self.assertRaises(storage.ArtifactStorageError):
                stream.read()

    def test_writer_keeps_raw_when_gzip_would_expand_it(self):
        storage = self.storage()
        data = b"<p>x</p>"
        source = self.root / "source.html"
        source.write_bytes(data)
        target = self.canonical(data)
        storage.write_artifact(source, target)
        self.assertEqual(storage.storage_path(target), target)
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(Path(str(target) + ".gz").exists())

    def test_writer_verifies_source_before_reusing_either_representation(self):
        storage = self.storage()
        data = b"stable content" * 1000
        source = self.root / "source.html"
        target = self.canonical(data)
        target.parent.mkdir(parents=True)
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                physical = Path(str(target) + ".gz") if compressed else target
                payload = gzip.compress(data, mtime=123) if compressed else data
                physical.write_bytes(payload)
                os.utime(physical, (10, 10))
                before = physical.stat()
                source.write_bytes(data)
                storage.write_artifact(source, target)
                self.assertEqual(physical.read_bytes(), payload)
                self.assertEqual(physical.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(physical.stat().st_ino, before.st_ino)
                source.write_bytes(b"wrong digest")
                with self.assertRaises(storage.ArtifactStorageError):
                    storage.write_artifact(source, target)
                self.assertEqual(physical.read_bytes(), payload)
                physical.unlink()

    def test_writer_failures_preserve_source_and_existing_object(self):
        storage = self.storage()
        data = b"validated content" * 1000
        source = self.root / "source.html"
        source.write_bytes(data)
        target = self.canonical(data)
        with patch.object(storage, "MAX_CAPTURE_BYTES", 10), self.assertRaises(storage.ArtifactStorageError):
            storage.write_artifact(source, target)
        for stage in ("fsync", "replace"):
            with self.subTest(stage=stage), patch.object(storage.os, stage, side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    storage.write_artifact(source, target)
                self.assertEqual(source.read_bytes(), data)
                self.assertEqual(list(target.parent.iterdir()), [])
        target.write_bytes(b"existing corrupt object must not be silently replaced")
        with self.assertRaises(storage.ArtifactStorageError):
            storage.write_artifact(source, target)
        self.assertEqual(target.read_bytes(), b"existing corrupt object must not be silently replaced")
        self.assertFalse(Path(str(target) + ".gz").exists())
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_storage_rejects_noncanonical_names_and_parent_traversal(self):
        storage = self.storage()
        source = self.root / "source.html"
        source.write_bytes(b"fixture")
        canonical = self.canonical(b"fixture")
        for target in (self.root / "other.html", self.root / ("A" * 64 + ".html"),
                       Path(str(canonical) + ".gz"), self.root / "child" / ".." / canonical.name):
            with self.subTest(target=target):
                with self.assertRaises(storage.ArtifactStorageError):
                    storage.write_artifact(source, target)
                with self.assertRaises(storage.ArtifactStorageError):
                    storage.storage_path(target)

    def test_storage_refuses_symlink_source_file_and_parents(self):
        storage = self.storage()
        data = b"not a secret, only a test fixture"
        source = self.root / "source.html"
        source.write_bytes(data)
        target = self.canonical(data)
        target.parent.mkdir(parents=True)
        try:
            target.symlink_to(source)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        try:
            for action in (storage.storage_path, storage.open_artifact, storage.delete_artifact):
                with self.subTest(action=action.__name__), self.assertRaises(storage.ArtifactStorageError):
                    action(target)
            with self.assertRaises(storage.ArtifactStorageError):
                storage.write_artifact(source, target)
        finally:
            target.unlink()
        compressed = Path(str(target) + ".gz")
        compressed.symlink_to(source)
        try:
            with self.assertRaises(storage.ArtifactStorageError):
                storage.open_artifact(target)
        finally:
            compressed.unlink()
        linked_source = self.root / "linked-source.html"
        linked_source.symlink_to(source)
        with self.assertRaises(storage.ArtifactStorageError):
            storage.write_artifact(linked_source, target)
        parent = self.root / "linked-parent"
        parent.symlink_to(target.parent, target_is_directory=True)
        with self.assertRaises(storage.ArtifactStorageError):
            storage.write_artifact(source, parent / target.name)
        self.assertEqual(source.read_bytes(), data)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_storage_refuses_windows_directory_junctions(self):
        storage = self.storage()
        actual = self.root / "actual"
        actual.mkdir()
        link = self.root / "junction"
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(actual)], capture_output=True)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.addCleanup(lambda: link.rmdir())
        data = b"junction test fixture"
        target = link / (hashlib.sha256(data).hexdigest() + ".html")
        source = actual / target.name
        source.write_bytes(data)
        for action in (storage.storage_path, storage.read_artifact, storage.delete_artifact):
            with self.subTest(action=action.__name__), self.assertRaises(storage.ArtifactStorageError):
                action(target)
        with self.assertRaises(storage.ArtifactStorageError):
            storage.write_artifact(source, target)
        self.assertEqual(source.read_bytes(), data)

    def test_delete_removes_both_representations_and_is_idempotent(self):
        storage = self.storage()
        target = self.canonical(b"raw")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"raw")
        compressed = Path(str(target) + ".gz")
        compressed.write_bytes(gzip.compress(b"raw"))
        other = self.root / "unrelated.html"
        other.write_bytes(b"keep")
        storage.delete_artifact(target)
        storage.delete_artifact(target)
        self.assertFalse(target.exists())
        self.assertFalse(compressed.exists())
        self.assertEqual(other.read_bytes(), b"keep")

    def test_publication_syncs_parent_after_atomic_publish(self):
        storage = self.storage()
        data = b"durable publication" * 1000
        source = self.root / "source.html"
        source.write_bytes(data)
        target = self.canonical(data)
        events = []
        replace = storage.os.replace
        fsync = storage.os.fsync
        def sync_file(fd):
            events.append("file")
            fsync(fd)
        def publish(temporary, destination):
            self.assertIn("file", events)
            replace(temporary, destination)
            events.append("publish")
        def sync_directory(directory):
            self.assertIn("publish", events)
            self.assertTrue(artifact_storage_path := storage.storage_path(target))
            self.assertTrue(artifact_storage_path.exists())
            events.append(directory)
        with patch.object(storage.os, "fsync", side_effect=sync_file), \
                patch.object(storage.os, "replace", side_effect=publish), \
                patch.object(storage, "_fsync_directory", side_effect=sync_directory, create=True):
            storage.write_artifact(source, target)
        self.assertIn(target.parent, events)
        self.assertLess(events.index("publish"), events.index(target.parent))
        self.assertEqual(storage.read_artifact(target), data)

    def test_directory_sync_failure_keeps_both_source_and_published_bytes(self):
        storage = self.storage()
        data = b"recoverable publication" * 1000
        source = self.root / "source.html"
        source.write_bytes(data)
        target = self.canonical(data)
        with patch.object(storage, "_fsync_directory", side_effect=OSError("directory fsync failure"), create=True):
            with self.assertRaises(OSError):
                storage.write_artifact(source, target)
        self.assertEqual(source.read_bytes(), data)
        self.assertEqual(storage.read_artifact(target), data)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_retry_syncs_reused_object_after_previous_directory_sync_failure(self):
        storage = self.storage()
        data = b"retry a durable publication" * 1000
        source = self.root / "source.html"
        source.write_bytes(data)
        target = self.canonical(data)
        with patch.object(storage, "_fsync_directory", side_effect=OSError("first failure")):
            with self.assertRaises(OSError):
                storage.write_artifact(source, target)
        with patch.object(storage, "_fsync_directory") as synced, patch.object(storage.os, "replace") as replaced:
            storage.write_artifact(source, target)
            self.assertIn(((target.parent,), {}), synced.call_args_list)
            replaced.assert_not_called()
        self.assertEqual(storage.read_artifact(target), data)

    def test_posix_directory_sync_closes_its_descriptor(self):
        storage = self.storage()
        self.assertTrue(hasattr(storage, "_fsync_directory"), "Publication must sync the directory entry")
        for failure in (False, True):
            with self.subTest(failure=failure), patch.object(storage.os, "name", "posix"), \
                    patch.object(storage.os, "open", return_value=123) as opened, \
                    patch.object(storage.os, "fsync", side_effect=OSError("fsync") if failure else None) as synced, \
                    patch.object(storage.os, "close") as closed:
                if failure:
                    with self.assertRaises(OSError):
                        storage._fsync_directory(self.root)
                else:
                    storage._fsync_directory(self.root)
                opened.assert_called_once()
                synced.assert_called_once_with(123)
                closed.assert_called_once_with(123)

    def test_retry_covers_created_ancestors_after_repeated_directory_sync_failures(self):
        storage = self.storage()
        real_sync = storage._fsync_directory
        for data in (b"x", b"durable compressed publication" * 1000):
            # Exercise failure at the shard, above the storage root, and at
            # the preexisting parent of the entire new hierarchy.
            for failure_level in (0, 3, 5):
                with self.subTest(compressed=len(data) > 1, failure_level=failure_level):
                    source = self.root / "source.html"
                    source.write_bytes(data)
                    new_root = self.root / f"case-{len(data)}-{failure_level}" / "new" / "generation"
                    target = self.canonical(data, new_root)
                    required = []
                    for parent in target.parents:
                        required.append(parent)
                        if parent == self.root:
                            break
                    failed_directory = required[failure_level]

                    def fail_directory(directory):
                        if directory == failed_directory:
                            raise OSError("injected ancestor fsync failure")
                        real_sync(directory)

                    # The second attempt sees only existing directories. That
                    # must not erase the first attempt's durability obligation.
                    with patch.object(storage, "_fsync_directory", side_effect=fail_directory):
                        for attempt in range(2):
                            with self.assertRaisesRegex(OSError, "ancestor fsync failure"):
                                storage.write_artifact(source, target)
                            self.assertEqual(source.read_bytes(), data)
                            self.assertEqual(storage.read_artifact(target), data)
                            self.assertEqual(list(target.parent.glob("*.tmp")), [])
                    physical = storage.storage_path(target)
                    before = physical.read_bytes(), physical.stat().st_ino, physical.stat().st_mtime_ns
                    synced = []

                    def record_sync(directory):
                        real_sync(directory)
                        synced.append(directory)

                    with patch.object(storage, "_fsync_directory", side_effect=record_sync), \
                            patch.object(storage.os, "replace", wraps=storage.os.replace) as replaced:
                        self.assertEqual(storage.write_artifact(source, target), physical)
                        replaced.assert_not_called()
                    self.assertEqual([path for path in synced if path in required], required)
                    self.assertEqual((physical.read_bytes(), physical.stat().st_ino,
                                      physical.stat().st_mtime_ns), before)
                    self.assertEqual(source.read_bytes(), data)
                    self.assertEqual(physical.suffix == ".gz", len(data) > 1)
                    self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_storage_import_and_roundtrip_need_only_python_standard_library(self):
        backend = str(Path(__file__).resolve().parents[1])
        script = f"""
import sys
sys.path.insert(0, {backend!r})
from pathlib import Path
import hashlib
from library.artifact_storage import MAX_CAPTURE_BYTES, write_artifact, read_artifact
from capture.models import MAX_CAPTURE_BYTES as capture_limit
assert MAX_CAPTURE_BYTES == capture_limit
assert 'playwright' not in sys.modules and 'django' not in sys.modules
assert 'capture.runner' not in sys.modules
root = Path({str(self.root)!r})
data = b'isolated standard-library roundtrip' * 1000
source = root / 'source.html'
source.write_bytes(data)
canonical = root / (hashlib.sha256(data).hexdigest() + '.html')
write_artifact(source, canonical)
assert read_artifact(canonical) == data
# Preserve the public export without forcing browser imports on model users.
from types import ModuleType
stub = ModuleType('capture.runner')
stub.capture = lambda request: request
sys.modules['capture.runner'] = stub
from capture import capture
assert capture is stub.capture
print('stdlib-only roundtrip and lazy public API: OK')
"""
        result = subprocess.run([sys.executable, "-S", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdlib-only roundtrip and lazy public API: OK", result.stdout)
