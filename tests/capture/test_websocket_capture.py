"""A rejected live socket must neither escape upstream nor deadlock capture."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from capture.development import run_development_capture
from capture.models import CaptureRequest


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class WebSocketCaptureTests(unittest.TestCase):
    def test_socket_is_intercepted_without_sync_callback_deadlock_or_upgrade(self):
        upgrades = []

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    upgrades.append(self.path)
                body = b'''<!doctype html><html><body><h1>Readable socket page</h1>
                  <script>setTimeout(()=>{
                    const socket=new WebSocket('ws://'+location.host+'/socket?secret=DO_NOT_LOG');
                    socket.onopen=()=>{document.querySelector('h1').textContent='Intercepted socket page';};
                  },50);</script></body></html>'''
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(str(uuid4()), 'test', 1, f'http://127.0.0.1:{server.server_port}/', directory,
                    development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=10)
                # Use the real process watchdog: the old callback deadlocks its
                # dispatcher and must fail this test instead of hanging pytest.
                result = run_development_capture(request, lambda: True)
                self.assertIn('blocked-request:unsupported_resource', result.warnings)
                self.assertIn('Intercepted socket page', Path(result.artifact_path).read_text('utf-8'))
                self.assertLess(time.monotonic()-started, 10)
                self.assertEqual(upgrades, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
