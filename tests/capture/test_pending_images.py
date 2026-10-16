"""Pending image loads must not receive the pre-existing broken-image exemption."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import unittest

from playwright.sync_api import sync_playwright

from capture.rewrite import DocumentRewriter
from capture.runner import IMAGE_DIAGNOSTICS, IMAGE_VISIBILITY


ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / 'backend' / 'capture' / 'freeze.js'


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium')
class PendingImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stop = threading.Event()

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/pending.svg':
                    cls.stop.wait(15)
                    if cls.stop.is_set():
                        return
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools/browsers'))
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.stop.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def freeze_and_replay(self, state):
        with self.browser.new_context(viewport={'width': 500, 'height': 300}) as context:
            source, replay = context.new_page(), context.new_page()
            source.set_content('<html><body><h1>Image fixture</h1><img id="subject" width="80" height="60"></body></html>')
            if state == 'loaded':
                url = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60"><rect width="80" height="60" fill="red"/></svg>'
            else:
                url = f'http://127.0.0.1:{self.server.server_port}/{state}.svg'
            source.locator('#subject').evaluate('(image,src)=>image.src=src', url)
            if state != 'pending':
                source.wait_for_function('document.querySelector("img").complete', timeout=3000)
            else:
                self.assertFalse(source.locator('#subject').evaluate('image=>image.complete'))
            frozen = source.evaluate('() => (' + FREEZE.read_text(encoding='utf8') + ')(' + IMAGE_VISIBILITY + ')')
            html = DocumentRewriter({}, [], 64 * 1024 * 1024).html(frozen['html'], 'https://fixture.example/')
            replay.set_content(html)
            marker = replay.locator('#subject').get_attribute('data-capture-source-image')
            stats = replay.evaluate(IMAGE_DIAGNOSTICS)
            return marker, stats

    def test_pending_image_is_not_exempt_from_new_visible_damage(self):
        marker, stats = self.freeze_and_replay('pending')
        self.assertEqual(marker, 'pending')
        self.assertEqual(stats['visible_broken'], 1)
        self.assertEqual(stats['new_visible_broken'], 1)

    def test_completed_failed_image_keeps_source_broken_exemption(self):
        marker, stats = self.freeze_and_replay('missing')
        self.assertEqual(marker, 'broken')
        self.assertEqual(stats['visible_broken'], 1)
        self.assertEqual(stats['new_visible_broken'], 0)

    def test_decoded_image_remains_loaded(self):
        marker, stats = self.freeze_and_replay('loaded')
        self.assertEqual(marker, 'loaded')
        self.assertEqual(stats['broken'], 0)
        self.assertEqual(stats['new_visible_broken'], 0)


if __name__ == '__main__':
    unittest.main()
