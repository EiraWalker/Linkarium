"""Opt-in checks against an actual deployment's Nginx upload gate."""
import os
import socket
import ssl
import unittest
from urllib.parse import urlsplit

import httpx


@unittest.skipUnless(os.environ.get("DEPLOY_TEST_ORIGIN"), "Set DEPLOY_TEST_ORIGIN to an isolated deployment")
class UploadGateTests(unittest.TestCase):
    def setUp(self):
        self.origin = os.environ["DEPLOY_TEST_ORIGIN"].rstrip("/")
        self.verify = os.environ.get("DEPLOY_TEST_INSECURE_TLS") != "1"
        self.client = httpx.Client(verify=self.verify, trust_env=False, timeout=15)
        self.addCleanup(self.client.close)

    def test_anonymous_uploads_receive_401_including_above_server_default(self):
        for path in ("/api/v1/imports/preview", "/api/v1/restores/validate"):
            for size in (1, 2 * 1024 * 1024):
                with self.subTest(path=path, size=size):
                    response = self.client.post(self.origin + path, content=b"x" * size)
                    self.assertEqual(response.status_code, 401)

    def test_anonymous_chunked_upload_receives_401(self):
        chunks = (b"x" * 65536 for _ in range(32))
        response = self.client.post(self.origin + "/api/v1/restores/validate", content=chunks)
        self.assertEqual(response.status_code, 401)

    def test_passkey_origin_is_available(self):
        response = self.client.get(self.origin + "/api/v1/session")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["passkey_available"])

    def test_main_request_limits_still_apply_before_body_is_sent(self):
        target = urlsplit(self.origin)
        context = ssl.create_default_context() if self.verify else ssl._create_unverified_context()
        cases = (("/api/v1/bookmarks", 2 * 1024 * 1024),
                 ("/api/v1/imports/preview", 22 * 1024 * 1024),
                 ("/api/v1/restores/validate", 16 * 1024 ** 3 + 1))
        for path, size in cases:
            with self.subTest(path=path):
                with socket.create_connection((target.hostname, target.port or 443), timeout=5) as raw:
                    with context.wrap_socket(raw, server_hostname=target.hostname) as connection:
                        request = (f"POST {path} HTTP/1.1\r\nHost: {target.netloc}\r\n"
                                   f"Content-Length: {size}\r\nConnection: close\r\n\r\n")
                        connection.sendall(request.encode("ascii"))
                        status = int(connection.recv(4096).split(b" ", 2)[1])
                        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()
