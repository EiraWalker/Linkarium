"""Connection behavior through the existing authenticated API and startup boundary."""
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from django.test import TestCase, override_settings

from runtime import test_authentication as auth_tests


@unittest.skipUnless(os.name == "nt", "Windows adapter inventory boundary")
class AdapterSelectionTests(unittest.TestCase):
    def test_startup_chooses_physical_wifi_over_wired_and_virtual_networks(self):
        from runtime.network_access import select_adapter
        adapters = [
            {"name": "VPN", "index": 3, "kind": "wifi", "hardware": False, "up": True, "ipv4": ["198.18.0.1"]},
            {"name": "Wired", "index": 4, "kind": "ethernet", "hardware": True, "up": True, "ipv4": ["192.0.2.10"]},
            {"name": "Office Wi-Fi", "index": 9, "kind": "wifi", "hardware": True, "up": True, "ipv4": ["192.0.2.20"]},
        ]
        with patch("runtime.network_access.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(adapters))):
            adapter = select_adapter()
        self.assertEqual((adapter.name, adapter.ipv4), ("Office Wi-Fi", "192.0.2.20"))

    def test_no_wifi_falls_back_to_wired_but_explicit_invalid_adapter_never_does(self):
        from runtime.network_access import select_adapter
        inventory = [
            {"name": "Wi-Fi", "index": 9, "kind": "wifi", "hardware": True, "up": False, "ipv4": ["192.0.2.20"]},
            {"name": "Wired", "index": 4, "kind": "ethernet", "hardware": True, "up": True, "ipv4": ["192.0.2.10"]},
        ]
        with patch("runtime.network_access.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(inventory))):
            self.assertEqual(select_adapter().ipv4, "192.0.2.10")
            self.assertEqual(select_adapter("4").name, "Wired")
            with self.assertRaisesRegex(RuntimeError, "不可用"):
                select_adapter("Wi-Fi")
            with self.assertRaisesRegex(RuntimeError, "未找到"):
                select_adapter("Missing")

    def test_virtual_apipa_and_ambiguous_addresses_are_not_silently_selected(self):
        from runtime.network_access import select_adapter
        fixtures = [
            [{"name": "VPN", "index": 3, "kind": "ethernet", "hardware": False, "up": True, "ipv4": ["198.18.0.1"]}],
            [{"name": "Wi-Fi", "index": 9, "kind": "wifi", "hardware": True, "up": True, "ipv4": ["169.254.1.1"]}],
            [{"name": "Wi-Fi", "index": 9, "kind": "wifi", "hardware": True, "up": True, "ipv4": ["192.0.2.20", "192.0.2.21"]}],
            [{"name": f"Wi-Fi {index}", "index": index, "kind": "wifi", "hardware": True, "up": True, "ipv4": [f"192.0.2.{index}"]} for index in (9, 10)],
        ]
        for inventory in fixtures:
            with self.subTest(inventory=inventory), patch("runtime.network_access.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(inventory))):
                with self.assertRaises(RuntimeError):
                    select_adapter()


@override_settings(BOOKMARK_AUTH_DEBUG=False, BOOKMARK_PASSKEY_ORIGIN="",
    AUTHENTICATION_BACKENDS=auth_tests.PUBLIC_BACKENDS,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConnectionApiTests(TestCase):
    databases = {"default", "library"}
    setUp = auth_tests.AuthenticationTests.setUp
    post = auth_tests.AuthenticationTests.post
    options = auth_tests.AuthenticationTests.options
    credential = auth_tests.AuthenticationTests.credential
    register = auth_tests.AuthenticationTests.register

    def test_http_access_link_is_server_owned_and_cannot_issue_device_authority(self):
        self.register()
        connection = {"mode": "http", "adapter": "Office Wi-Fi", "ipv4": "192.0.2.20",
            "access_url": "http://192.0.2.20:18001/", "certificate_url": None}
        with patch.dict(os.environ, {"BOOKMARK_CONNECTION": json.dumps(connection)}):
            response = self.client.get("/api/v1/runtime/status", HTTP_X_FORWARDED_HOST="evil.example")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["connection"], {**connection, "device_enrollment": False})
            rejected = self.post("passkey/grants", {})
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(rejected.json()["error"]["code"], "DEVICE_HTTPS_REQUIRED")
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)

    def test_https_grant_keeps_canonical_origin_and_private_runtime_fields_private(self):
        self.register()
        origin = "https://localhost"
        connection = {"mode": "lan_https", "adapter": "Wi-Fi", "ipv4": "192.0.2.20",
            "access_url": origin + "/", "certificate_url": "http://192.0.2.20:18081/"}
        encoded = {**connection, "private_key_path": "/not-public", "authorization_code": "fixture-secret"}
        with override_settings(BOOKMARK_PASSKEY_ORIGIN=origin), patch.dict(os.environ, {"BOOKMARK_CONNECTION": json.dumps(encoded)}):
            status = self.client.get("/api/v1/runtime/status", secure=True, HTTP_X_FORWARDED_HOST="evil.example")
            self.assertEqual(status.json()["data"]["connection"], {**connection, "device_enrollment": True})
            token = self.client.get("/api/v1/session", secure=True).json()["data"]["csrf_token"]
            granted = self.client.post("/api/v1/session/passkey/grants", data="{}", content_type="application/json",
                secure=True, HTTP_ORIGIN=origin, HTTP_X_CSRFTOKEN=token, HTTP_X_FORWARDED_HOST="evil.example")
            self.assertEqual(granted.status_code, 201, granted.content)
            data = granted.json()["data"]
            self.assertTrue(data["enrollment_url"].startswith(origin + "/login#enroll="))
            self.assertNotIn(data["authorization_code"], data["enrollment_url"])
            bad_origin = self.client.post("/api/v1/session/passkey/grants", data="{}", content_type="application/json",
                secure=True, HTTP_ORIGIN="https://evil.example", HTTP_X_CSRFTOKEN=token)
            self.assertEqual(bad_origin.status_code, 403)
