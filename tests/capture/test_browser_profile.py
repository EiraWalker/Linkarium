"""Both capture variants send a browser profile suitable for public web pages."""
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
class BrowserProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents = []
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                ua = self.headers.get('User-Agent', '')
                cls.agents.append(ua)
                denied = 'HeadlessChrome' in ua or self.path == '/denied'
                body = b'<html><body><h1>Public page content</h1></body></html>'
                self.send_response(403 if denied else 200)
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
        cls.thread.join(timeout=2)

    def request(self, directory, variant='desktop', path='/'):
        return CaptureRequest(str(uuid4()), 'test', 1,
            f'http://127.0.0.1:{self.server.server_port}{path}', directory, variant=variant,
            development=True, allow_test_hosts=('127.0.0.1',))

    def test_desktop_public_page_accepts_browser_profile_and_replays(self):
        self.agents.clear()
        with tempfile.TemporaryDirectory() as directory:
            result = capture(self.request(directory))
            self.assertIn('Public page content', Path(result.artifact_path).read_text('utf8'))
        self.assertTrue(self.agents)
        self.assertTrue(all('Chrome/' in ua and 'HeadlessChrome' not in ua and 'Mobile' not in ua for ua in self.agents))

    def test_mobile_profile_stays_mobile(self):
        self.agents.clear()
        with tempfile.TemporaryDirectory() as directory:
            result = capture(self.request(directory, 'mobile'))
            self.assertIn('Public page content', Path(result.artifact_path).read_text('utf8'))
        self.assertTrue(self.agents)
        self.assertTrue(all('Mobile' in ua and 'Android' in ua for ua in self.agents))

    def test_actual_access_denial_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CaptureError) as caught:
                capture(self.request(directory, path='/denied'))
            self.assertEqual(caught.exception.code, 'access_denied')
            self.assertFalse(list(Path(directory).glob('*.html')))
