"""Publication, HTTP, previews and GC use the logical artifact boundary."""
import gzip
import hashlib
import os
from pathlib import Path
import tempfile
import time
import subprocess
import unittest
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from runtime.models import RuntimeState
from runtime.testing import force_credential_login
from . import artifact_storage, catalog, preview_cache, queue
from .artifacts import artifact_path
from .models import Artifact, CaptureState, LibrarySettings, SnapshotVersion
from .worker import cleanup_artifacts, publish_snapshot


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ArtifactStorageIntegrationTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        user = get_user_model().objects.create_user("owner", password="test")
        force_credential_login(self.client, user)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for module in ("library.artifacts", "library.worker", "library.preview_cache"):
            patcher = patch(f"{module}.generation_root", return_value=self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bookmark = catalog.create_bookmark({"url": "https://example.org/storage", "snapshot_enabled": True})[0]
        self.data = b"<!doctype html>\r\n" + b"<p>immutable original bytes</p>" * 10000 + b"\x00\xff"

    def stored_version(self, data=None, *, compressed=True):
        data = self.data if data is None else data
        digest = hashlib.sha256(data).hexdigest()
        canonical = artifact_path(digest)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        physical = Path(str(canonical) + ".gz") if compressed else canonical
        physical.write_bytes(gzip.compress(data) if compressed else data)
        artifact, _ = Artifact.objects.get_or_create(sha256=digest,
            defaults={"relative_path": f"artifacts/{digest[:2]}/{digest}.html", "byte_size": len(data)})
        queue.enqueue("capture", self.bookmark.id, "desktop")
        job, attempt = queue.claim_next()
        queue.finish(job, attempt)
        version = SnapshotVersion.objects.create(bookmark=self.bookmark, artifact=artifact, capture_attempt=attempt,
            variant="desktop", captured_at=timezone.now(), source_url=self.bookmark.canonical_url,
            final_url=self.bookmark.canonical_url, viewport_width=1366, viewport_height=768,
            recipe_version="test", capture_epoch=1)
        return version, canonical, physical

    def test_publication_compresses_and_deduplicates_by_original_hash(self):
        source = self.root / "capture.html"
        source.write_bytes(self.data)
        queue.request_refresh([self.bookmark.id])
        versions = []
        for _ in range(2):
            job, attempt = queue.claim_next()
            with patch.object(Path, "read_bytes", side_effect=AssertionError("Publication must stream HTML")):
                version = publish_snapshot(job, attempt, {"artifact_path": source,
                    "viewport_width": 1366, "viewport_height": 768})
            self.assertEqual(publish_snapshot(job, attempt, {}).id, version.id)
            versions.append(version)
        self.assertEqual(Artifact.objects.count(), 1)
        self.assertEqual(SnapshotVersion.objects.count(), 2)
        self.assertEqual(versions[0].artifact_id, hashlib.sha256(self.data).hexdigest())
        artifact = versions[0].artifact
        self.assertEqual(artifact.byte_size, len(self.data))
        self.assertTrue(artifact.relative_path.endswith(".html"))
        canonical = artifact_path(artifact.sha256, artifact.relative_path)
        self.assertEqual(artifact_storage.storage_path(canonical), Path(str(canonical) + ".gz"))
        self.assertFalse(canonical.exists())
        self.assertEqual(artifact_storage.read_artifact(canonical, expected_size=artifact.byte_size), self.data)
        self.assertEqual(len(list((self.root / "artifacts").glob("*/*"))), 1)
        self.assertEqual(source.read_bytes(), self.data)

    def test_http_serves_logical_bytes_with_original_length_and_closes_streams(self):
        for compressed in (True, False):
            with self.subTest(compressed=compressed):
                version, canonical, physical = self.stored_version(compressed=compressed)
                opened = []
                def record(path, **kwargs):
                    reader = artifact_storage.open_artifact(path, **kwargs)
                    opened.append(reader)
                    return reader
                with patch("config.api.open_artifact", side_effect=record, create=True):
                    response = self.client.get(f"/api/v1/snapshots/{version.id}/preview")
                    try:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(int(response["Content-Length"]), len(self.data))
                        self.assertNotIn("Content-Encoding", response)
                        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
                        self.assertEqual(response["Cache-Control"], "private, no-store")
                        self.assertIn("sandbox", response["Content-Security-Policy"])
                        self.assertIn("script-src 'none'", response["Content-Security-Policy"])
                        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
                        self.assertEqual(b"".join(response.streaming_content), self.data)
                    finally:
                        response.close()
                    self.assertTrue(opened[-1].closed)
                    self.assertTrue(opened[-1]._file.closed)
                    self.assertTrue(opened[-1]._stream.closed)
                    response = self.client.get(f"/api/v1/snapshots/{version.id}/preview")
                    response.close()  # Client disconnect before first body iteration.
                    self.assertTrue(opened[-1].closed)
                    head = self.client.head(f"/api/v1/snapshots/{version.id}/preview")
                    try:
                        self.assertEqual(head.status_code, 200)
                        self.assertEqual(int(head["Content-Length"]), len(self.data))
                        self.assertEqual(b"".join(head.streaming_content), b"")
                    finally:
                        head.close()
                    self.assertTrue(opened[-1].closed)
                for method in ("get", "head"):
                    self.assertEqual(getattr(Client(), method)(f"/api/v1/snapshots/{version.id}/preview").status_code, 401)
                physical.unlink()

    def test_http_uses_request_generation_and_reports_missing_artifact(self):
        version, canonical, physical = self.stored_version()
        def request_root(generation):
            self.assertEqual(generation, "initial")
            return self.root
        with patch("library.artifacts.generation_root", side_effect=request_root):
            response = self.client.get(f"/api/v1/snapshots/{version.id}/preview")
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b"".join(response.streaming_content), self.data)
            finally:
                response.close()
        physical.unlink()
        response = self.client.get(f"/api/v1/snapshots/{version.id}/preview")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "ARTIFACT_MISSING")

    def test_http_corruption_fails_body_iteration_and_closes_reader(self):
        version, canonical, physical = self.stored_version()
        physical.write_bytes(physical.read_bytes()[:-5])
        response = self.client.get(f"/api/v1/snapshots/{version.id}/preview")
        try:
            self.assertEqual(response.status_code, 200)
            with self.assertRaises(artifact_storage.ArtifactStorageError):
                b"".join(response.streaming_content)
        finally:
            response.close()
        physical.unlink()  # Windows also verifies that the underlying file was closed.

    def test_gzip_thumbnail_queues_and_render_verifies_before_child(self):
        version, canonical, physical = self.stored_version()
        response = self.client.get(f"/api/v1/snapshots/{version.id}/thumbnail")
        self.assertEqual(response.status_code, 202)
        self.assertTrue(preview_cache.request_path(version.id).exists())
        with patch("library.preview_cache.subprocess.Popen") as spawn, patch("library.preview_cache.store_preview") as store:
            spawn.return_value.poll.return_value = 0
            spawn.return_value.returncode = 0
            self.assertTrue(preview_cache.render_version(version, "initial"))
            self.assertEqual(spawn.call_args.args[0][3], str(canonical))
            store.assert_called_once()
        self.assertFalse(canonical.exists())
        self.assertEqual(artifact_storage.read_artifact(canonical), self.data)

    def test_preview_rejects_invalid_gzip_size_or_hash_before_launch(self):
        version, canonical, physical = self.stored_version()
        original = physical.read_bytes()
        for payload in (original[:-5], gzip.compress(b"bad")):
            with self.subTest(payload_size=len(payload)):
                physical.write_bytes(payload)
                with patch("library.preview_cache.subprocess.Popen") as spawn:
                    from .errors import ApiError
                    with self.assertRaises(ApiError):
                        preview_cache.render_version(version, "initial")
                    spawn.assert_not_called()
        physical.write_bytes(original)
        version.artifact.byte_size += 1
        with patch("library.preview_cache.subprocess.Popen") as spawn:
            with self.assertRaises(ApiError):
                preview_cache.render_version(version, "initial")
            spawn.assert_not_called()

    def test_gc_handles_both_representations_refs_and_one_hour_grace(self):
        retained = []
        removed = []
        for compressed in (False, True):
            # A referenced historical version must survive regardless of age.
            version, canonical, physical = self.stored_version(b"referenced" + bytes([compressed]), compressed=compressed)
            os.utime(physical, (0, 0))
            retained.append(physical)
            # Old object with an unreferenced Artifact row.
            version, canonical, physical = self.stored_version(b"unreferenced" + bytes([compressed]), compressed=compressed)
            version.delete()
            os.utime(physical, (0, 0))
            removed.append(physical)
            # Old filesystem orphan, and a recent filesystem orphan.
            for recent in (False, True):
                version, canonical, physical = self.stored_version(b"orphan" + bytes([compressed, recent]), compressed=compressed)
                version.delete()
                version.artifact.delete()
                if not recent:
                    os.utime(physical, (0, 0))
                (retained if recent else removed).append(physical)
        # Either representation's recent publication protects the whole object.
        version, canonical, physical = self.stored_version(b"dual" * 1000)
        version.delete()
        canonical.write_bytes(b"dual" * 1000)
        os.utime(canonical, (0, 0))
        retained.extend((canonical, physical))
        invalid = canonical.parent / "not-a-canonical-object.html.gz"
        invalid.write_bytes(b"not ours")
        os.utime(invalid, (0, 0))
        retained.append(invalid)
        cleanup_artifacts()
        for path in removed:
            self.assertFalse(path.exists(), path.name)
        for path in retained:
            self.assertTrue(path.exists(), path.name)
        self.assertTrue(Artifact.objects.filter(pk=version.artifact_id).exists())
        self.assertEqual(SnapshotVersion.objects.count(), 2)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_canonical_path_and_gc_reject_directory_junctions(self):
        from .errors import ApiError
        digest = "a" * 64
        actual = self.root / "elsewhere"
        actual.mkdir()
        source = actual / f"{digest}.html.gz"
        source.write_bytes(b"fixture must not be removed")
        os.utime(source, (0, 0))
        link = self.root / "artifacts" / "aa"
        link.parent.mkdir()
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(actual)], capture_output=True)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.addCleanup(lambda: link.rmdir())
        with self.assertRaises(ApiError):
            artifact_path(digest)
        cleanup_artifacts()
        self.assertEqual(source.read_bytes(), b"fixture must not be removed")
