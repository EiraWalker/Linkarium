"""Storage encoding never changes the v1 portability contract."""
from contextlib import closing, contextmanager
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from typing import Literal
import unittest
from unittest.mock import patch
import zipfile

from runtime import maintenance


class ArtifactStoragePortabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="artifact-portability-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.generation = self.root / "source"
        self.generation.mkdir()
        self.database = self.generation / "library.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE library_bookmark(id TEXT PRIMARY KEY,canonical_url TEXT UNIQUE);
                CREATE TABLE library_folder(id TEXT PRIMARY KEY,parent_id TEXT);
                CREATE TABLE library_artifact(sha256 TEXT PRIMARY KEY,relative_path TEXT,byte_size INTEGER);
                CREATE TABLE library_snapshotversion(id TEXT PRIMARY KEY,bookmark_id TEXT,variant TEXT,
                    artifact_id TEXT REFERENCES library_artifact(sha256));
                CREATE TABLE library_capturestate(id INTEGER PRIMARY KEY,bookmark_id TEXT,variant TEXT,
                    current_version_id TEXT REFERENCES library_snapshotversion(id));
                INSERT INTO library_bookmark VALUES('b','https://example.com/');
            """)
        self.payload = b"<!doctype html><title>Saved bytes</title>" + b"<p>unchanged</p>" * 4096
        self.name = self.add_artifact(self.payload, "saved", compressed=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO library_snapshotversion VALUES('mobile','b','mobile',?)",
                               (hashlib.sha256(self.payload).hexdigest(),))
            connection.execute("INSERT INTO library_capturestate VALUES(1,'b','desktop','saved')")
            connection.execute("INSERT INTO library_capturestate VALUES(2,'b','mobile','mobile')")
            connection.commit()

    def add_artifact(self, payload, snapshot, *, compressed):
        checksum = hashlib.sha256(payload).hexdigest()
        name = f"artifacts/{checksum[:2]}/{checksum}.html"
        canonical = self.generation / name
        canonical.parent.mkdir(parents=True, exist_ok=True)
        if compressed:
            canonical.with_suffix(".html.gz").write_bytes(gzip.compress(payload, mtime=0))
        else:
            canonical.write_bytes(payload)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO library_artifact VALUES(?,?,?)", (checksum, name, len(payload)))
            connection.execute("INSERT INTO library_snapshotversion VALUES(?,'b','desktop',?)", (snapshot, checksum))
            connection.commit()
        return name

    def test_gzip_backed_backup_is_standard_v1_html_and_restores_references(self):
        package = self.root / "backup.bookmarklib.zip"
        result = maintenance.create_backup(self.generation, package)
        expected = {"size": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest()}
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "library.sqlite3", self.name})
            self.assertEqual(archive.read(self.name), self.payload)
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["files"][self.name], expected)
        self.assertEqual(result["manifest"], manifest)
        candidate = self.root / "restored"
        restored = maintenance.validate_package(package, candidate, expected_schema=self.database)
        self.assertEqual(restored["counts"], {"artifacts": 1, "snapshots": 2, "desktop_snapshots": 1,
                                              "mobile_snapshots": 1, "bookmarks": 1, "folders": 0})
        self.assertEqual((candidate / self.name).read_bytes(), self.payload)
        with closing(sqlite3.connect(candidate / "library.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT sha256,relative_path,byte_size FROM library_artifact").fetchall(),
                             [(expected["sha256"], self.name, len(self.payload))])
            self.assertEqual(connection.execute("SELECT artifact_id,count(*) FROM library_snapshotversion GROUP BY artifact_id").fetchall(),
                             [(expected["sha256"], 2)])
            self.assertEqual(connection.execute("SELECT variant,current_version_id FROM library_capturestate ORDER BY variant").fetchall(),
                             [("desktop", "saved"), ("mobile", "mobile")])

    def test_mixed_raw_gzip_candidate_revalidates_decoded_bytes(self):
        raw_payload = b"<!doctype html><p>Legacy raw snapshot</p>"
        raw_name = self.add_artifact(raw_payload, "raw", compressed=False)
        package = self.root / "mixed.zip"
        maintenance.create_backup(self.generation, package)
        candidate = self.root / "candidate"
        manifest = maintenance.validate_package(package, candidate, expected_schema=self.database)
        canonical = candidate / self.name
        canonical.with_suffix(".html.gz").write_bytes(gzip.compress(canonical.read_bytes(), mtime=0))
        canonical.unlink()
        self.assertEqual(maintenance.validate_candidate(candidate, expected_schema=self.database), manifest)
        self.assertEqual((candidate / raw_name).read_bytes(), raw_payload)
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(archive.read(raw_name), raw_payload)
            self.assertEqual(archive.read(self.name), self.payload)

    def legacy_package(self, *, payload=None):
        """Build a released v1 archive without invoking the new backup writer."""
        package = self.root / "legacy.zip"
        contents = {"library.sqlite3": self.database.read_bytes(),
                    self.name: self.payload if payload is None else payload}
        manifest = {"format": "bookmarklib", "format_version": 1, "schema_version": 1,
                    "created_at": "2026-01-01T00:00:00+00:00", "source_generation": "old-release",
                    "files": {name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                              for name, value in contents.items()},
                    "counts": {"artifacts": 1, "snapshots": 2, "desktop_snapshots": 1,
                               "mobile_snapshots": 1, "bookmarks": 1, "folders": 0}}
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, value in contents.items():
                archive.writestr(name, value)
        return package

    def test_legacy_package_restores_compact_candidate_inside_resource_boundary(self):
        package = self.legacy_package()
        candidate = self.root / "compact-candidate"
        with patch("runtime.maintenance._validate_database", side_effect=AssertionError("must run in child")):
            manifest = maintenance.validate_package(package, candidate, expected_schema=self.database,
                                                    compact_artifacts=True)
        canonical = candidate / self.name
        self.assertFalse(canonical.exists())
        self.assertEqual(gzip.decompress(canonical.with_suffix(".html.gz").read_bytes()), self.payload)
        self.assertEqual(maintenance.validate_candidate(candidate, expected_schema=self.database), manifest)
        self.assertEqual(set(path.relative_to(candidate).as_posix() for path in candidate.rglob("*") if path.is_file()),
                         {"manifest.json", "library.sqlite3", self.name + ".gz"})

    def large_compressible_package(self):
        for index in range(4):
            self.add_artifact(bytes([65 + index]) * (8 * 1024**2), f"large-{index}", compressed=True)
        package = self.root / "large.zip"
        maintenance.create_backup(self.generation, package)
        return package

    def quota(self, free_bytes):
        """Account for real writes, while imposing a portable per-test quota."""
        def used():
            return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        capacity = used() + free_bytes
        def disk_usage(_):
            consumed = used()
            return SimpleNamespace(total=capacity, used=consumed, free=capacity - consumed)
        return patch("runtime.maintenance.shutil.disk_usage", side_effect=disk_usage)

    def test_compact_restore_reserves_one_object_not_whole_logical_library(self):
        package = self.large_compressible_package()
        candidate = self.root / "quota-candidate"
        # The logical library plus safety reserve cannot fit; one verified
        # object and its compressed representation can. All writes are real.
        with self.quota(40 * 1024**2):
            manifest = maintenance._validate_package(package, candidate, expected_schema=self.database,
                                                     compact_artifacts=True)
        self.assertGreater(sum(item["size"] for item in manifest["files"].values()) + 16 * 1024**2,
                           40 * 1024**2)
        self.assertEqual(maintenance.validate_candidate(candidate, expected_schema=self.database), manifest)
        self.assertFalse(list((candidate / "artifacts").rglob("*.html")))

    def test_backup_budgets_actual_zip_bytes_and_verifies_compact_candidate(self):
        self.large_compressible_package()
        package = self.root / "quota-backup.zip"
        with self.quota(40 * 1024**2), patch("runtime.maintenance.validate_package",
                                          wraps=maintenance.validate_package) as validation:
            result = maintenance.create_backup(self.generation, package)
        self.assertTrue(validation.call_args.kwargs["compact_artifacts"])
        self.assertLess(result["byte_size"], 1024**2)
        with zipfile.ZipFile(package) as archive:
            for name, item in result["manifest"]["files"].items():
                payload = archive.read(name)
                self.assertEqual(len(payload), item["size"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])
        self.assertFalse(list(self.root.glob("*.partial")))
        self.assertFalse(list(self.root.glob("verify-*")))

    def test_forged_logical_hash_is_rejected_before_artifact_compression(self):
        package = self.legacy_package(payload=b"modified bytes with their own valid manifest SHA")
        candidate = self.root / "forged-candidate"
        from library.artifact_storage import write_artifact
        with patch("library.artifact_storage.write_artifact", wraps=write_artifact) as publish:
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance._validate_package(package, candidate, expected_schema=self.database,
                                              compact_artifacts=True)
        self.assertEqual(caught.exception.code, "BACKUP_ARTIFACT_INVALID")
        publish.assert_not_called()
        self.assertFalse(list(candidate.rglob("*.gz")))

    def test_corrupt_gzip_never_publishes_backup_or_changes_source(self):
        compressed = (self.generation / self.name).with_suffix(".html.gz")
        original = compressed.read_bytes()
        bad_crc = bytearray(original)
        bad_crc[-8] ^= 1
        cases = {"crc": bytes(bad_crc), "truncated": original[:-4],
                 "sha": gzip.compress(b"x" * len(self.payload), mtime=0),
                 "size": gzip.compress(self.payload + b"x", mtime=0)}
        for kind, value in cases.items():
            with self.subTest(kind=kind):
                compressed.write_bytes(value)
                package = self.root / f"corrupt-{kind}.zip"
                with self.assertRaises(maintenance.MaintenanceError) as caught:
                    maintenance.create_backup(self.generation, package)
                self.assertEqual(caught.exception.code, "BACKUP_ARTIFACT_CORRUPT")
                self.assertEqual(compressed.read_bytes(), value)
                self.assertFalse(package.exists())
                self.assertFalse(package.with_suffix(".zip.partial").exists())

    def test_corrupt_gzip_candidate_fails_second_validation(self):
        candidate = self.root / "candidate"
        maintenance.validate_package(self.legacy_package(), candidate, expected_schema=self.database,
                                     compact_artifacts=True)
        compressed = (candidate / self.name).with_suffix(".html.gz")
        invalid = bytearray(compressed.read_bytes())
        invalid[-8] ^= 1
        compressed.write_bytes(invalid)
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_candidate(candidate, expected_schema=self.database)
        self.assertEqual(caught.exception.code, "RESTORE_CANDIDATE_CHANGED")

    def test_gzip_expansion_above_96_mib_never_publishes_backup(self):
        compressed = (self.generation / self.name).with_suffix(".html.gz")
        with gzip.open(compressed, "wb") as destination:
            block = b"x" * 1024**2
            for _ in range(96):
                destination.write(block)
            destination.write(b"x")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE library_artifact SET byte_size=?", (maintenance.MAX_ARTIFACT_BYTES,))
            connection.commit()
        package = self.root / "oversized.zip"
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.code, "BACKUP_ARTIFACT_CORRUPT")
        self.assertFalse(package.exists())
        self.assertTrue(compressed.is_file())

    def test_compact_restore_keeps_logical_expansion_budget(self):
        package = self.legacy_package()
        candidate = self.root / "too-expanded"
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_package(package, candidate, expected_schema=self.database,
                                         compact_artifacts=True, expanded_limit=len(self.payload))
        self.assertEqual(caught.exception.code, "BACKUP_EXPANSION_LIMIT")
        self.assertEqual(list(candidate.iterdir()), [])

    def test_compact_restore_keeps_artifact_entry_size_budget(self):
        package = self.legacy_package()
        with patch("runtime.maintenance.MAX_ARTIFACT_BYTES", len(self.payload) - 1):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance._validate_package(package, self.root / "entry-limit", compact_artifacts=True)
        self.assertEqual(caught.exception.code, "BACKUP_ENTRY_TOO_LARGE")

    def test_compact_restore_requires_space_for_raw_and_publication_temporaries(self):
        package = self.legacy_package()
        with self.quota(16 * 1024**2 + len(self.payload)):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance._validate_package(package, self.root / "no-staging-space", compact_artifacts=True)
        self.assertEqual(caught.exception.code, "INSUFFICIENT_DISK_SPACE")

    def test_archive_writer_rechecks_space_as_actual_bytes_grow(self):
        self.add_artifact(os.urandom(2 * 1024**2), "entropy", compressed=False)
        package = self.root / "disk-full.zip"
        with self.quota(17 * 1024**2):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.code, "INSUFFICIENT_DISK_SPACE")
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_zip_output_errors_are_not_reported_as_corrupt_artifacts(self):
        compressed = (self.generation / self.name).with_suffix(".html.gz")
        original = compressed.read_bytes()
        real_open = zipfile.ZipFile.open
        for error_number in (errno.ENOSPC, errno.EIO):
            with self.subTest(errno=error_number):
                package = self.root / f"output-fault-{error_number}.zip"

                def failed_write(_):
                    raise OSError(error_number, "injected ZIP output failure")

                @contextmanager
                def faulty_destination(stream):
                    with stream:
                        yield SimpleNamespace(write=failed_write)

                def open_with_output_fault(archive, name, mode: Literal["r", "w"] = "r", *args, **kwargs):
                    stream = real_open(archive, name, mode, *args, **kwargs)
                    return faulty_destination(stream) if mode == "w" and name == self.name else stream

                expected_error = maintenance.MaintenanceError if error_number == errno.ENOSPC else OSError
                with patch.object(zipfile.ZipFile, "open", new=open_with_output_fault):
                    with self.assertRaises(expected_error) as caught:
                        maintenance.create_backup(self.generation, package)
                if error_number == errno.ENOSPC:
                    assert isinstance(caught.exception, maintenance.MaintenanceError)
                    self.assertEqual(caught.exception.code, "INSUFFICIENT_DISK_SPACE")
                else:
                    self.assertEqual(caught.exception.errno, errno.EIO)
                self.assertEqual(compressed.read_bytes(), original)
                self.assertFalse(package.exists())
                self.assertFalse(package.with_name(package.name + ".partial").exists())

    def test_backup_verification_uses_less_space_for_a_single_large_object(self):
        self.add_artifact(b"q" * (32 * 1024**2), "large", compressed=True)
        package = self.root / "single-large.zip"
        # Keep the same validator, but run this trusted fixture in-process so
        # its real writes participate in the portable quota accounting.
        with self.quota(64 * 1024**2), patch("runtime.maintenance.validate_package",
                                          side_effect=maintenance._validate_package):
            result = maintenance.create_backup(self.generation, package)
        self.assertEqual(result["manifest"]["counts"]["artifacts"], 2)
        self.assertTrue(package.is_file())
        self.assertFalse(list(self.root.glob("verify-*")))

    def test_gzip_storage_symlink_is_rejected_without_reading_outside_object(self):
        compressed = (self.generation / self.name).with_suffix(".html.gz")
        outside = self.root / "outside.gz"
        value = compressed.read_bytes()
        outside.write_bytes(value)
        compressed.unlink()
        try:
            compressed.symlink_to(outside)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink creation privilege unavailable")
            raise
        package = self.root / "symlink.zip"
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.create_backup(self.generation, package)
        self.assertFalse(package.exists())
        self.assertEqual(outside.read_bytes(), value)
