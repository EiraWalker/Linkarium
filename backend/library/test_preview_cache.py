from runtime.testing import force_credential_login
import hashlib
import base64
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from . import catalog, queue, preview_cache
from .models import Artifact, CaptureState, LibrarySettings, SnapshotVersion
from .worker import publish_snapshot
from runtime.models import RuntimeState


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PreviewCacheTests(TestCase):
    databases = {"default", "library"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.workspace = tempfile.TemporaryDirectory()
            cls.jpeg = Path(cls.workspace.name) / "sample.jpg"
            # Browser-encoded fixture, shared once; these tests cover storage/API.
            # Real viewport screenshots are covered by capture integration tests.
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[2] / "tools" / "browsers"))
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1366, "height": 768})
                encoded = page.evaluate("""() => { const c = document.createElement('canvas');
                    c.width = 1366; c.height = 768;
                    const ctx = c.getContext('2d'); ctx.fillStyle = '#ddeeff';
                    ctx.fillRect(0, 0, c.width, c.height); return c.toDataURL('image/jpeg', .82).split(',')[1]; }""")
                cls.jpeg.write_bytes(base64.b64decode(encoded))
                browser.close()
        except BaseException:
            # unittest skips tearDownClass when subclass setup fails after Django
            # opens its class atomics. Release them before later transaction tests.
            try:
                if hasattr(cls, "workspace"):
                    cls.workspace.cleanup()
            finally:
                super().tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.workspace.cleanup()
        finally:
            super().tearDownClass()

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        user = get_user_model().objects.create_user("owner", password="test")
        force_credential_login(self.client, user)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for module in ("library.preview_cache", "library.artifacts", "library.worker"):
            patcher = patch(f"{module}.generation_root", return_value=self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.site = catalog.create_bookmark({"url": "https://example.org/preview", "snapshot_enabled": True})[0]
        queue.request_refresh([self.site.id])
        self.job, self.attempt = queue.claim_next()
        self.html = self.root / "source.html"
        self.html.write_text("<!doctype html><h1>Preserved original</h1>", encoding="utf-8")

    def publish(self, with_image=True):
        result = {"artifact_path": self.html, "viewport_width": 1366, "viewport_height": 768}
        if with_image:
            result.update(preview_path=self.jpeg, preview_sha256=hashlib.sha256(self.jpeg.read_bytes()).hexdigest(), preview_byte_size=self.jpeg.stat().st_size)
        return publish_snapshot(self.job, self.attempt, result)

    def test_capture_publishes_image_and_keeps_html_history(self):
        version = self.publish()
        self.assertEqual(preview_cache.image_path(version).read_bytes(), self.jpeg.read_bytes())
        self.assertEqual(publish_snapshot(self.job, self.attempt, {}).id, version.id)
        self.assertEqual(SnapshotVersion.objects.count(), 1)
        detail = self.client.get(f"/api/v1/bookmarks/{self.site.id}").json()["data"]
        self.assertIn("/thumbnail?v=1", detail["preview"]["thumbnail_url"])
        html = self.client.get(f"/api/v1/snapshots/{version.id}/preview")
        self.assertEqual(b"".join(html.streaming_content), self.html.read_bytes())
        self.assertIn("sandbox", html["Content-Security-Policy"])
        html.close()

    def test_image_revalidates_only_after_authentication_and_existence(self):
        version = self.publish()
        url = f"/api/v1/snapshots/{version.id}/thumbnail?v=1"
        image = self.client.get(url)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image["Content-Type"], "image/jpeg")
        self.assertEqual(b"".join(image.streaming_content), self.jpeg.read_bytes())
        etag = image["ETag"]
        image.close()
        cached = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(cached.status_code, 304)
        self.assertIn("private", cached["Cache-Control"])
        self.assertIn("must-revalidate", cached["Cache-Control"])
        self.assertEqual(Client().get(url, HTTP_IF_NONE_MATCH=etag).status_code, 401)
        head = self.client.head(url)
        self.assertEqual(b"".join(head.streaming_content), b"")
        head.close()
        CaptureState.objects.filter(current_version=version).update(current_version=None)
        version.delete()
        self.assertEqual(self.client.get(url, HTTP_IF_NONE_MATCH=etag).status_code, 404)

    def test_missing_image_queues_only_derivative_work_and_deduplicates(self):
        version = self.publish(False)
        before = (SnapshotVersion.objects.count(), Artifact.objects.count(), self.site.capture_epoch)
        for _ in range(2):
            response = self.client.get(f"/api/v1/snapshots/{version.id}/thumbnail")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(len(list(preview_cache.cache_root().glob("*.request"))), 1)
        self.assertEqual(preview_cache.next_missing("initial").id, version.id)
        self.assertEqual(before, (SnapshotVersion.objects.count(), Artifact.objects.count(), self.site.capture_epoch))

    def test_thumbnail_reuses_the_request_library_generation(self):
        version = self.publish()
        def request_root(generation):
            self.assertEqual(generation, 'initial', 'HTTP must reuse its already-read library state')
            return self.root
        with patch('library.preview_cache.generation_root', side_effect=request_root):
            response = self.client.get(f'/api/v1/snapshots/{version.id}/thumbnail')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), self.jpeg.read_bytes())
            response.close()

    def test_invalid_image_does_not_turn_valid_archive_into_capture_failure(self):
        with patch("library.preview_cache.store_preview", side_effect=ValueError("bad jpeg")):
            version = self.publish()
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, "succeeded")
        self.assertFalse(preview_cache.image_path(version).exists())
        self.assertEqual(CaptureState.objects.get(bookmark=self.site, variant=self.job.variant).last_error_code, "")

    def test_image_identity_includes_content_device_viewport_and_generation(self):
        version = self.publish()
        initial = preview_cache.image_path(version)
        version.variant = "mobile"
        self.assertNotEqual(initial, preview_cache.image_path(version))
        version.variant = "desktop"
        version.viewport_width = 1024
        self.assertNotEqual(initial, preview_cache.image_path(version))
        version.viewport_width = 1366
        with patch("library.preview_cache.generation_root", return_value=self.root / "new-generation"):
            self.assertNotEqual(initial, preview_cache.image_path(version))

    def test_backfill_failures_cool_down_without_changing_capture_state(self):
        version = self.publish(False)
        preview_cache.request_preview(version)
        with patch("library.preview_cache.render_version", side_effect=TimeoutError), patch("library.preview_cache.current_generation", return_value="initial"):
            self.assertTrue(preview_cache.backfill_one())
        self.assertIsNone(preview_cache.next_missing("initial"))
        response = self.client.get(f"/api/v1/snapshots/{version.id}/thumbnail")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "PREVIEW_UNAVAILABLE")
        self.assertGreater(int(response["Retry-After"]), 3500)
        self.assertEqual(CaptureState.objects.get(bookmark=self.site, variant=self.job.variant).last_error_code, "")

    def test_cleanup_preserves_referenced_images_and_original_archives(self):
        version = self.publish()
        cached = preview_cache.image_path(version)
        orphan = cached.with_name("a" * 64 + "-desktop-1366x768.jpg")
        orphan.write_bytes(self.jpeg.read_bytes())
        os.utime(orphan, (0, 0))
        preview_cache.cleanup_previews()
        self.assertFalse(orphan.exists())
        self.assertTrue(cached.exists())
        self.assertEqual(Artifact.objects.count(), 1)

    def test_corrupt_html_is_not_converted_to_a_trusted_image(self):
        version = self.publish(False)
        path = self.root / version.artifact.relative_path
        path.write_bytes(b"tampered")
        with patch("library.preview_cache.subprocess.Popen") as spawn:
            from .errors import ApiError
            with self.assertRaises(ApiError):
                preview_cache.render_version(version, "initial")
            spawn.assert_not_called()
