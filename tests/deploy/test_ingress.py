"""Exercise the real Waitress header/body boundary without allocating large uploads."""
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from waitress.server import create_server

from runtime.ingress import ANONYMOUS_LIMIT, UploadAdmissions, install_body_gate


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.admissions = UploadAdmissions(Path(self.work.name))
        transport = {}
        def app(environ, start):
            content = environ['wsgi.input'].read()
            start('200 OK', [('Content-Type', 'text/plain')])
            return [str(len(content)).encode()]
        self.server = create_server(app, host='127.0.0.1', port=0, map=transport,
            max_request_body_size=ANONYMOUS_LIMIT + 1, inbuf_overflow=ANONYMOUS_LIMIT * 2)
        install_body_gate(transport, self.admissions)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.close)
        self.addCleanup(self.server.task_dispatcher.shutdown)

    def connect(self):
        stream = socket.create_connection(('127.0.0.1', self.server.effective_port), timeout=3)
        self.addCleanup(stream.close)
        return stream

    def test_large_anonymous_body_rejected_from_headers_alone(self):
        for path in ('/api/v1/restores/validate', '/api/v1/session', '/'):
            with self.subTest(path=path):
                stream = self.connect()
                stream.sendall(f'POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {16 * 1024**3}\r\n\r\n'.encode())
                self.assertIn(b'413', stream.recv(4096))

    def test_expect_continue_cannot_bypass_header_rejection(self):
        stream = self.connect()
        stream.sendall(b'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nContent-Length: 17179869184\r\n\r\n')
        response = stream.recv(4096)
        self.assertIn(b'413', response)
        self.assertNotIn(b'100 Continue', response)

    def test_anonymous_chunked_request_is_bounded(self):
        stream = self.connect()
        stream.sendall(b'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n')
        content = b'x' * 65536
        try:
            for _ in range(18):
                stream.sendall(b'10000\r\n' + content + b'\r\n')
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.assertIn(b'413', stream.recv(4096))

    def test_authorized_ticket_allows_large_body_once(self):
        size = ANONYMOUS_LIMIT + 100
        ticket = self.admissions.issue('/api/v1/restores/validate', size)
        stream = self.connect()
        stream.sendall(f'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nX-Linkarium-Upload: {ticket}\r\nContent-Length: {size}\r\n\r\n'.encode())
        stream.sendall(b'x' * size)
        result = b''
        while block := stream.recv(4096):
            result += block
        self.assertIn(b'200 OK', result)
        self.assertTrue(result.endswith(str(size).encode()))
        replay = self.connect()
        replay.sendall(f'POST /api/v1/restores/validate HTTP/1.1\r\nHost: localhost\r\nX-Linkarium-Upload: {ticket}\r\nContent-Length: {size}\r\n\r\n'.encode())
        self.assertIn(b'413', replay.recv(4096))

    def test_ticket_cannot_authorize_another_route_or_method(self):
        ticket = self.admissions.issue('/api/v1/restores/validate', ANONYMOUS_LIMIT + 100)
        for method, path in (('POST', '/api/v1/session'), ('PUT', '/api/v1/restores/validate')):
            stream = self.connect()
            stream.sendall(f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nX-Linkarium-Upload: {ticket}\r\nContent-Length: {ANONYMOUS_LIMIT + 100}\r\n\r\n'.encode())
            self.assertIn(b'413', stream.recv(4096))

    def test_admissions_bound_concurrency_and_expire_unused_tickets(self):
        from library.errors import ApiError
        from unittest.mock import patch
        self.admissions.issue('/api/v1/restores/validate', 100)
        with self.assertRaises(ApiError):
            self.admissions.issue('/api/v1/restores/validate', 100)
        with patch('runtime.ingress.time.monotonic', return_value=10**12):
            self.assertTrue(self.admissions.issue('/api/v1/restores/validate', 100))


if __name__ == '__main__':
    unittest.main()
