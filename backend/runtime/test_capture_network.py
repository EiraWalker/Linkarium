"""Capture proxy settings exercise the authenticated API without touching live configuration."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from library.models import LibrarySettings
from runtime.models import RuntimeState


@override_settings(BOOKMARK_CAPTURE_MODE="development",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CaptureNetworkApiTests(TestCase):
    databases = {"default", "library"}
    endpoint = "/api/v1/capture-network"

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        self.owner = get_user_model().objects.create_user("owner", password="test-password")
        self.client.force_login(self.owner)
        temporary = tempfile.TemporaryDirectory(prefix="capture-network-api-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch("config.api.DATA_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        detection = patch("capture.proxy_config.detect_clash", return_value="http://127.0.0.1:7897")
        self.detect_clash = detection.start()
        self.addCleanup(detection.stop)

    @property
    def config_file(self):
        return self.root / "runtime" / "capture-network.json"

    def save(self, data):
        return self.client.patch(self.endpoint, json.dumps(data), content_type="application/json")

    def test_authentication_required_for_read_and_write(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.endpoint).status_code, 401)
        self.assertEqual(self.save({"mode": "clash"}).status_code, 401)
        self.assertFalse(self.config_file.exists())
        self.detect_clash.assert_not_called()

    def test_each_mode_persists_and_returns_the_effective_proxy(self):
        cases = [
            ({"mode": "clash"}, "", "http://127.0.0.1:7897"),
            ({"mode": "manual", "proxy_url": "http://127.0.0.1:8888/"},
                "http://127.0.0.1:8888", "http://127.0.0.1:8888"),
            ({"mode": "direct"}, "", None),
        ]
        for request, saved_url, active_url in cases:
            with self.subTest(mode=request["mode"]):
                response = self.save(request)
                self.assertEqual(response.status_code, 200, response.content)
                expected = {"mode": request["mode"], "proxy_url": saved_url,
                    "detected_proxy_url": "http://127.0.0.1:7897", "active_proxy_url": active_url}
                self.assertEqual(response.json()["data"], expected)
                self.assertEqual(self.client.get(self.endpoint).json()["data"], expected)
                self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8")),
                    {"mode": request["mode"], "proxy_url": saved_url})

    def test_read_detects_clash_without_enabling_or_persisting_it(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {"mode": "direct", "proxy_url": "",
            "detected_proxy_url": "http://127.0.0.1:7897", "active_proxy_url": None})
        self.detect_clash.assert_called_once_with()
        self.assertFalse(self.config_file.exists())

    def test_remote_and_authenticated_proxies_are_rejected_without_changing_config(self):
        self.assertEqual(self.save({"mode": "clash"}).status_code, 200)
        before = self.config_file.read_bytes()
        for proxy_url in ("http://8.8.8.8:7897", "http://192.168.1.2:7897",
                "http://name:DO_NOT_SAVE@127.0.0.1:7897"):
            with self.subTest(proxy_url=proxy_url):
                response = self.save({"mode": "manual", "proxy_url": proxy_url})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["error"]["code"], "CAPTURE_PROXY_CONFIG")
                self.assertEqual(self.config_file.read_bytes(), before)
                self.assertNotIn(b"DO_NOT_SAVE", response.content)

    def test_isolated_mode_rejects_writes_without_changing_config(self):
        self.assertEqual(self.save({"mode": "manual", "proxy_url": "http://127.0.0.1:7897"}).status_code, 200)
        before = self.config_file.read_bytes()
        with override_settings(BOOKMARK_CAPTURE_MODE="isolated"):
            for mode in ("direct", "clash", "manual"):
                with self.subTest(mode=mode):
                    response = self.save({"mode": mode, "proxy_url": "http://127.0.0.1:8888"})
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertEqual(response.json()["error"]["code"], "CAPTURE_PROXY_CONFIG")
                    self.assertEqual(self.config_file.read_bytes(), before)

    def test_valid_session_still_requires_csrf_for_changes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        response = client.patch(self.endpoint, json.dumps({"mode": "clash"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.config_file.exists())
