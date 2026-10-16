"""Exercise image acceptance through the real browser, rewriter and Monolith."""
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from capture.models import CaptureError, CaptureRequest
from capture.rewrite import DocumentRewriter
from capture.runner import capture


BROKEN = 'data:image/png;base64,broken'


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class ImageQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/good.svg':
                    body = b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="blue"/></svg>'
                    mime = 'image/svg+xml'
                else:
                    good = '<img width="80" height="80" src="/good.svg" data-capture-source-image="broken">'
                    bad = f'<img width="80" height="80" src="{BROKEN}">'
                    layouts = {
                        '/source-broken': bad,
                        '/below': '<div style="height:2000px"></div>' + good,
                        '/hidden': '<div style="opacity:0">' + good + '</div>',
                        '/clipped': '<div style="overflow:hidden;width:10px;height:10px"><div style="margin-top:100px">' + good + '</div></div>',
                        '/regression': bad + good,
                        '/collapsed': '<img alt="" src="/good.svg">',
                    }
                    body = ('<!doctype html><html><body><h1>Image acceptance fixture</h1>' + layouts.get(self.path, good) + '</body></html>').encode()
                    mime = 'text/html'
                self.send_response(200)
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

    def run_capture(self, path, variant='desktop', corrupt=False):
        original = DocumentRewriter.data_url

        def rewrite(rewriter, value, base):
            if corrupt and value.endswith('/good.svg'):
                return 'data:image/png;base64,newlybroken'
            # Repair the original broken image while corrupting the healthy one:
            # equal total bad-image counts must not hide a new first-screen loss.
            if corrupt and path == '/regression' and value == BROKEN:
                return original(rewriter, '/good.svg', base)
            return original(rewriter, value, base)

        with tempfile.TemporaryDirectory() as directory, patch.object(DocumentRewriter, 'data_url', rewrite):
            result = capture(CaptureRequest(attempt_uuid=str(uuid.uuid4()), generation='test', library_epoch=1,
                url=f'http://127.0.0.1:{self.server.server_port}{path}', output_dir=directory,
                development=True, allow_test_hosts=('127.0.0.1',), variant=variant))
            self.assertTrue(Path(result.artifact_path).is_file())
            return result

    def test_original_broken_image_is_saved_with_warning_on_both_devices(self):
        for variant in ('desktop', 'mobile'):
            with self.subTest(variant=variant):
                result = self.run_capture('/source-broken', variant)
                self.assertIn('source-broken-images:1', result.warnings)

    def test_new_offscreen_or_hidden_damage_is_warning_only(self):
        for path in ('/below', '/hidden', '/clipped'):
            with self.subTest(path=path):
                result = self.run_capture(path, corrupt=True)
                self.assertIn('offscreen-broken-images:1', result.warnings)

    def test_new_visible_damage_is_rejected_even_when_total_count_is_unchanged(self):
        with self.assertRaises(CaptureError) as caught:
            self.run_capture('/regression', corrupt=True)
        self.assertEqual(caught.exception.code, 'replay_failed')
        self.assertIn('first-screen', str(caught.exception))

    def test_healthy_image_remains_accepted(self):
        result = self.run_capture('/healthy')
        self.assertFalse(any('broken-images' in warning for warning in result.warnings))

    def test_new_damage_cannot_escape_by_collapsing_image_dimensions(self):
        with self.assertRaises(CaptureError) as caught:
            self.run_capture('/collapsed', corrupt=True)
        self.assertEqual(caught.exception.code, 'replay_failed')
        self.assertIn('first-screen', str(caught.exception))
