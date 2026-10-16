"""Slow source navigation may use 60s without extending the capture deadline."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from playwright.sync_api import Page

from capture.models import CaptureError, CaptureRequest
from capture.runner import capture


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class SourceNavigationBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stopping = threading.Event()

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path.endswith('.js'):
                    # Defer blocks DOMContentLoaded until a real response is
                    # complete, without blocking the browser renderer thread.
                    delay = 32 if self.path == '/slow.js' else 8
                    if cls.stopping.wait(delay):
                        return
                    body = b'document.querySelector("h1").textContent="Completed slow source";'
                    mime = 'text/javascript'
                else:
                    script = '/slow.js' if self.path == '/slow' else '/budget.js'
                    body = ('<html><head><script defer src="' + script + '"></script></head>'
                            '<body><h1>Incomplete document</h1></body></html>').encode()
                    mime = 'text/html'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # The bounded-budget test intentionally closes early.

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stopping.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, directory, path, timeout=120):
        return CaptureRequest(str(uuid4()), 'test', 1,
            f'http://127.0.0.1:{self.server.server_port}{path}', directory,
            development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=timeout)

    def test_source_dcl_after_30_seconds_succeeds_and_replay_keeps_30_second_cap(self):
        navigations = []
        original = Page.goto

        def observe(page, target, *args, **kwargs):
            start = time.monotonic()
            response = original(page, target, *args, **kwargs)
            navigations.append({'source': not target.startswith('http://snapshot.invalid/'),
                'timeout': kwargs.get('timeout'), 'elapsed': time.monotonic() - start})
            return response

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(Page, 'goto', observe):
                result = capture(self.request(directory, '/slow'))
            html = Path(result.artifact_path).read_text('utf-8')
            self.assertIn('Completed slow source', html)
            self.assertNotIn('Incomplete document', html)
            source = next(item for item in navigations if item['source'])
            replay = next(item for item in navigations if not item['source'])
            self.assertGreater(source['elapsed'], 30)
            self.assertLess(source['elapsed'], 60)
            self.assertEqual(source['timeout'], 60000)
            self.assertEqual(replay['timeout'], 30000)

    def test_small_total_budget_still_times_out_without_an_archive(self):
        source_timeouts = []
        original = Page.goto

        def observe(page, target, *args, **kwargs):
            source_timeouts.append(kwargs.get('timeout'))
            return original(page, target, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            start = time.monotonic()
            with patch.object(Page, 'goto', observe), self.assertRaises(CaptureError) as caught:
                capture(self.request(directory, '/budget', timeout=3))
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertTrue(source_timeouts)
            self.assertLessEqual(source_timeouts[0], 3000)
            self.assertGreater(source_timeouts[0], 0)
            self.assertLess(time.monotonic() - start, 15, 'The 60s stage cap must not extend a 3s total budget')
            self.assertFalse(list(Path(directory).glob('*.html')))


if __name__ == '__main__':
    unittest.main()
