"""A frozen browser-selected image must survive Monolith's lazy-load heuristic."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

import html5lib

from capture.models import CaptureRequest
from capture.runner import capture


SELECTED_IMAGE = b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="blue"/></svg>'


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class LazyImageSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requested_paths = []

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                cls.requested_paths.append(self.path)
                status = 200
                if self.path == '/selected.svg':
                    body, mime = SELECTED_IMAGE, 'image/svg+xml'
                elif self.path in ('/responsive', '/loaded'):
                    # Squarespace keeps data-src at the original URL after srcset
                    # chooses a resized image. Only the selected URL was fetched.
                    attrs = ('src="/original.jpg" srcset="/selected.svg 1000w" sizes="100vw"'
                             if self.path == '/responsive' else 'src="/selected.svg"')
                    body = ('<!doctype html><html><body><h1>Lazy image fixture</h1>'
                            f'<img id="photo" width="80" height="80" {attrs} '
                            'data-src="/original.jpg" data-image-kind="portrait" loading="lazy">'
                            '</body></html>').encode()
                    mime = 'text/html'
                else:
                    body, mime, status = b'missing original', 'text/plain', 404
                self.send_response(status)
                self.send_header('Content-Type', mime)
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

    def assert_preserved(self, path, variant):
        self.requested_paths.clear()
        with tempfile.TemporaryDirectory() as directory:
            result = capture(CaptureRequest(attempt_uuid=str(uuid4()), generation='test', library_epoch=1,
                url=f'http://127.0.0.1:{self.server.server_port}{path}', output_dir=directory,
                development=True, allow_test_hosts=('127.0.0.1',), variant=variant))
            tree = html5lib.parse(Path(result.artifact_path).read_text(encoding='utf-8'), namespaceHTMLElements=False)
            photo = next(element for element in tree.iter('img') if element.get('id') == 'photo')
            self.assertEqual(photo.get('src'), 'data:image/svg+xml;base64,' + base64.b64encode(SELECTED_IMAGE).decode())
            self.assertEqual(photo.get('data-capture-source-image'), 'loaded')
            self.assertEqual(photo.get('data-image-kind'), 'portrait')
            self.assertFalse(any('broken-images' in warning for warning in result.warnings))
        self.assertIn('/selected.svg', self.requested_paths)
        self.assertNotIn('/original.jpg', self.requested_paths)

    def test_responsive_image_retains_browser_selected_resource_on_both_devices(self):
        for variant in ('desktop', 'mobile'):
            with self.subTest(variant=variant):
                self.assert_preserved('/responsive', variant)

    def test_loaded_image_does_not_revert_to_stale_lazy_source(self):
        self.assert_preserved('/loaded', 'desktop')
