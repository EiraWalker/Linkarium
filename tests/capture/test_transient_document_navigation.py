"""A temporary HTTP error may self-navigate; error pages never become archives."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from capture.models import CaptureError, CaptureRequest
from capture.runner import capture


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class TransientDocumentNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                status = 200
                body = b'<html><body><h1>Real public content</h1></body></html>'
                if self.path in ('/recover', '/denied-recovery', '/stable-error'):
                    status = 503
                    body = b'<html><body><h1>Temporary error document</h1>'
                    if self.path != '/stable-error':
                        destination = '/denied' if self.path == '/denied-recovery' else '/real'
                        body += ('<script>setTimeout(()=>location.replace("' + destination + '"),150)</script>').encode()
                    body += b'</body></html>'
                elif self.path == '/denied':
                    status = 403
                self.send_response(status)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, directory, path):
        return CaptureRequest(str(uuid4()), 'test', 1, f'http://127.0.0.1:{self.server.server_port}{path}',
                              directory, development=True, allow_test_hosts=('127.0.0.1',))

    def test_self_recovering_503_archives_only_final_200_document(self):
        with tempfile.TemporaryDirectory() as directory:
            result = capture(self.request(directory, '/recover'))
            html = Path(result.artifact_path).read_text('utf-8')
            self.assertIn('Real public content', html)
            self.assertNotIn('Temporary error document', html)
            self.assertTrue(result.final_url.endswith('/real'))

    def test_stable_503_and_self_redirect_to_403_are_still_rejected(self):
        for path, error in (('/stable-error', 'navigation_failed'), ('/denied-recovery', 'access_denied')):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(CaptureError) as caught:
                    capture(self.request(directory, path))
                self.assertEqual(caught.exception.code, error)
                self.assertFalse(list(Path(directory).glob('*.html')))
