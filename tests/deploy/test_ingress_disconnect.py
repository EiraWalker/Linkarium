"""Additional real-socket regressions for pre-body upload admission."""
import time
import unittest
from unittest.mock import patch

from runtime.ingress import ANONYMOUS_LIMIT
from tests.deploy import test_ingress as fixtures


class UploadLeaseLifetimeTests(unittest.TestCase):
    def setUp(self):
        fixtures.IngressTests.setUp(self)

    def connect(self):
        return fixtures.IngressTests.connect(self)

    def test_disconnect_mid_upload_releases_capacity(self):
        size = ANONYMOUS_LIMIT + 100
        token = self.admissions.issue('/api/v1/restores/validate', size)
        stream = self.connect()
        stream.sendall(f'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nX-Linkarium-Upload: {token}\r\nContent-Length: {size}\r\n\r\n'.encode())
        self.assertIn(b'100 Continue', stream.recv(4096))
        self.assertIn(token, self.admissions.active)
        stream.sendall(b'partial body')
        stream.close()
        deadline = time.monotonic() + 3
        while self.admissions.active and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.admissions.active, set())
        self.assertTrue(self.admissions.issue('/api/v1/restores/validate', size))

    def test_duplicate_forged_and_underscore_token_headers_cannot_raise_anonymous_limit(self):
        size = ANONYMOUS_LIMIT + 100
        token = self.admissions.issue('/api/v1/restores/validate', size)
        for header in (f'X-Linkarium-Upload: {token}\r\nX-Linkarium-Upload: {token}',
                       'X-Linkarium-Upload: forged', f'X_Linkarium_Upload: {token}'):
            with self.subTest(header=header.split(':', 1)[0]):
                stream = self.connect()
                stream.sendall(f'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\n{header}\r\nContent-Length: {size}\r\n\r\n'.encode())
                response = stream.recv(4096)
                self.assertIn(b'413', response)
                self.assertNotIn(b'100 Continue', response)
        self.assertEqual(self.admissions.active, set())

    def test_absolute_upload_deadline_releases_a_connection_that_keeps_sending_bytes(self):
        size = ANONYMOUS_LIMIT + 100
        token = self.admissions.issue('/api/v1/restores/validate', size)
        stream = self.connect()
        with patch('runtime.ingress.UPLOAD_MAX_SECONDS', 0.15):
            stream.sendall(f'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nX-Linkarium-Upload: {token}\r\nContent-Length: {size}\r\n\r\n'.encode())
            self.assertIn(b'100 Continue', stream.recv(4096))
        deadline = time.monotonic() + 3
        while self.admissions.active and time.monotonic() < deadline:
            try:
                stream.sendall(b'x')
            except OSError:
                pass
            time.sleep(0.01)
        self.assertEqual(self.admissions.active, set())
        self.assertEqual(stream.recv(4096), b'')
        self.assertTrue(self.admissions.issue('/api/v1/restores/validate', size))

    def test_authorized_chunked_upload_uses_its_granted_budget_and_releases_it(self):
        size = ANONYMOUS_LIMIT + 100
        token = self.admissions.issue('/api/v1/restores/validate', size + 65536)
        stream = self.connect()
        stream.sendall(f'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nX-Linkarium-Upload: {token}\r\nTransfer-Encoding: chunked\r\n\r\n'.encode())
        content = b'x' * size
        for offset in range(0, len(content), 65536):
            part = content[offset:offset + 65536]
            stream.sendall(f'{len(part):x}\r\n'.encode() + part + b'\r\n')
        stream.sendall(b'0\r\n\r\n')
        response = b''
        while block := stream.recv(4096):
            response += block
        self.assertIn(b'200 OK', response)
        self.assertTrue(response.endswith(str(size).encode()))
        self.assertEqual(self.admissions.active, set())
