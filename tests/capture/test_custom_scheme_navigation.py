"""A blocked app deep link does not discard the current public HTML document."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from capture.models import CaptureRequest
from capture.runner import capture


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class CustomSchemeNavigationTests(unittest.TestCase):
    def test_app_deep_link_keeps_current_successful_document(self):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = b'''<html><body><h1>Public readable article</h1>
                  <script>setTimeout(()=>location.href='bilibili://video/123',100)</script></body></html>'''
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(str(uuid4()), 'test', 1,
                    f'http://127.0.0.1:{server.server_port}/', directory,
                    development=True, allow_test_hosts=('127.0.0.1',))
                result = capture(request)
                self.assertIn('Public readable article', Path(result.artifact_path).read_text('utf-8'))
                self.assertTrue(result.final_url.startswith('http://127.0.0.1:'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
