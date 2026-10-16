"""Playback streams are optional; static resources and aggregate budgets are not."""
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from capture.diagnostics import CaptureTrace, mime_only
from capture.doh_proxy import PinnedPublicProxy
from capture.models import CaptureError, CaptureRequest
from capture.runner import _capture, capture


LARGE_BYTES = 23_605_375
POSTER = b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="blue"/></svg>'


class MediaDiagnosticTests(unittest.TestCase):
    def test_mime_diagnostics_drop_parameters_and_non_mime_header_content(self):
        self.assertEqual(mime_only('VIDEO/MP4; secret=DO_NOT_LOG'), 'video/mp4')
        self.assertEqual(mime_only('https://example.com/?secret=DO_NOT_LOG'), '')
        self.assertEqual(mime_only('video/mp4\r\nX-Secret: DO_NOT_LOG'), '')


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class MediaResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                mime, advertised = 'text/html', None
                if path == '/poster.svg':
                    body, mime = POSTER, 'image/svg+xml'
                elif path == '/large':
                    # Headers alone reproduce the old rejection without sending
                    # a real 23 MB movie over the loopback fixture.
                    body, mime, advertised = b'incomplete media', 'video/mp4; token=DO_NOT_LOG', LARGE_BYTES
                elif path == '/budget-media':
                    body, mime = b'x' * 8192, 'video/mp4'
                else:
                    markup = {
                        '/media': '<video width="160" height="90" preload="auto" poster="/poster.svg" src="/large?secret=DO_NOT_LOG"></video>',
                        '/media-no-poster': '<video width="160" height="90" preload="auto" src="/large"></video>',
                        '/image': '<img width="160" height="90" src="/large">',
                        '/script': '<script src="/large"></script>',
                        '/network-budget': '<video width="160" height="90" preload="auto" src="/budget-media"></video>',
                        '/frame': '''<video id="frame" width="160" height="90" muted autoplay></video>
                          <script>const canvas=document.createElement('canvas');canvas.width=160;canvas.height=90;
                          const ctx=canvas.getContext('2d');ctx.fillStyle='blue';ctx.fillRect(0,0,160,90);
                          const video=document.querySelector('video');video.srcObject=canvas.captureStream(10);
                          const timer=setInterval(()=>ctx.fillRect(0,0,160,90),50);video.play().catch(()=>{});</script>''',
                    }.get(path, '')
                    body = ('<!doctype html><html><body><h1>Readable media page</h1>' + markup + '</body></html>').encode()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(advertised or len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, directory, path, public=False):
        host = 'example.com' if public else f'127.0.0.1:{self.server.server_port}'
        return CaptureRequest(str(uuid4()), 'test', 1, f'http://{host}{path}', directory,
                              development=True, allow_test_hosts=() if public else ('127.0.0.1',))

    def test_oversized_playback_keeps_poster_and_readable_html(self):
        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory, '/media')
            trace = Path(directory) / 'trace.json'
            with CaptureTrace(trace, request):
                result = capture(request)
            html = Path(result.artifact_path).read_text('utf-8')
            self.assertIn('Readable media page', html)
            self.assertIn('data:image/svg+xml;base64,', html)
            self.assertNotIn('<video', html)
            self.assertIn('media-stream-not-archived', result.warnings)
            self.assertIn('media-poster:VIDEO', result.warnings)
            evidence = json.loads(trace.read_text('utf-8'))
            skipped = [event for event in evidence['events'] if event['stage'] == 'media_not_archived']
            self.assertTrue(skipped)
            self.assertEqual(skipped[0]['advertised_bytes'], LARGE_BYTES)
            self.assertEqual(skipped[0]['mime_type'], 'video/mp4')
            self.assertEqual(skipped[0]['host'], '127.0.0.1')
            self.assertNotIn('DO_NOT_LOG', trace.read_text('utf-8'))
            self.assertLess(result.byte_size, 100_000)

    def test_missing_media_poster_uses_local_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            result = capture(self.request(directory, '/media-no-poster'))
            self.assertIn('media-placeholder:VIDEO', result.warnings)
            self.assertNotIn('<video', Path(result.artifact_path).read_text('utf-8'))

    def test_already_readable_current_frame_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            result = capture(self.request(directory, '/frame'))
            html = Path(result.artifact_path).read_text('utf-8')
            self.assertIn('data-capture-media="frame"', html)
            self.assertIn('data:image/png;base64,', html)
            self.assertNotIn('media-placeholder:VIDEO', result.warnings)

    def test_video_mime_does_not_exempt_main_images_or_scripts(self):
        for path in ('/image', '/script'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                request = self.request(directory, path)
                trace = Path(directory) / 'trace.json'
                with self.assertRaises(CaptureError) as caught, CaptureTrace(trace, request):
                    capture(request)
                self.assertEqual(caught.exception.code, 'byte_budget')
                evidence = json.loads(trace.read_text('utf-8'))
                events = [event for event in evidence['events'] if event['stage'] == 'resource_limit']
                self.assertEqual(events[0]['resource_type'], path[1:])
                self.assertEqual(events[0]['mime_type'], 'video/mp4')
                self.assertFalse(list(Path(directory).glob('*.html')))

    def proxy(self, max_bytes=64*1024*1024):
        class Resolver:
            def resolve(self, host, port):
                return [ipaddress.ip_address('93.184.216.34')]

            def close(self):
                pass

        def connector(address, timeout):
            self.assertEqual(address, ('93.184.216.34', 80))
            return socket.create_connection(('127.0.0.1', self.server.server_port), timeout=timeout)

        return PinnedPublicProxy(Resolver(), max_bytes, time.monotonic()+30, connector=connector).start()

    def test_playback_still_consumes_aggregate_network_budget(self):
        proxy = self.proxy(max_bytes=4096)
        try:
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(CaptureError) as caught:
                _capture(self.request(directory, '/network-budget', public=True), proxy=proxy)
            self.assertEqual(caught.exception.code, 'byte_budget')
            self.assertEqual(proxy.error.code, 'byte_budget')
        finally:
            proxy.close()

    def test_late_network_budget_failure_cannot_publish_html(self):
        proxy = self.proxy()
        real_run = subprocess.run

        def archive_then_exhaust(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if '-o' in args[0]:
                proxy.error = CaptureError('byte_budget', 'Capture network byte budget exceeded')
            return result

        try:
            with tempfile.TemporaryDirectory() as directory:
                with patch('capture.runner.subprocess.run', archive_then_exhaust), self.assertRaises(CaptureError) as caught:
                    _capture(self.request(directory, '/', public=True), proxy=proxy)
                self.assertEqual(caught.exception.code, 'byte_budget')
                self.assertFalse(list(Path(directory).glob('*.html')))
        finally:
            proxy.close()
