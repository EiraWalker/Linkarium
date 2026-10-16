"""A source video cannot start new downloads after its static state is frozen."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from capture.models import CaptureRequest
from capture.runner import capture


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class FrozenMediaNetworkTests(unittest.TestCase):
    def test_late_playback_is_blocked_but_archive_preserves_real_poster(self):
        downloads = []
        poster = b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="blue"/></svg>'
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/poster.svg':
                    body, mime = poster, 'image/svg+xml'
                elif self.path == '/late.mp4':
                    downloads.append(self.path)
                    body, mime = b'unneeded playback bytes', 'video/mp4'
                else:
                    body = b'''<html><body><h1>Readable media page</h1>
                      <video width="160" height="90" poster="/poster.svg" preload="auto"></video>
                      <script>setTimeout(()=>{const video=document.querySelector('video');
                        video.src='/late.mp4';video.load();},2000)</script></body></html>'''
                    mime = 'text/html'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = subprocess.run

        def archive_after_playback_timer(*args, **kwargs):
            if '-o' in args[0]:
                time.sleep(2)
            return original(*args, **kwargs)

        try:
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(str(uuid4()), 'test', 1,
                    f'http://127.0.0.1:{server.server_port}/', directory,
                    development=True, allow_test_hosts=('127.0.0.1',))
                with patch('capture.runner.subprocess.run', archive_after_playback_timer):
                    result = capture(request)
                html = Path(result.artifact_path).read_text('utf-8')
                self.assertIn('Readable media page', html)
                self.assertIn('data:image/svg+xml;base64,', html)
                self.assertIn('media-stream-stopped-after-freeze', result.warnings)
                self.assertFalse(downloads)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
