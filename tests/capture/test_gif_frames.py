"""Large animations become their actual first frame without relaxing budgets."""
import base64
import ipaddress
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from playwright.sync_api import sync_playwright
import httpx

from capture.doh_proxy import PinnedPublicProxy
from capture.gif_frame import FirstGifFrame, InvalidGif, MAX_FRAME_BYTES, fetch_first_frame
from capture.models import CaptureError, CaptureRequest
from capture.runner import _capture
from capture.network import PublicURLPolicy


BLUE = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\xff\xff\x00\x00'
        b'\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b')


class FirstGifFrameTests(unittest.TestCase):
    def test_arbitrary_chunk_boundaries_keep_real_complete_first_frame(self):
        for size in (1, 2, 13, 31, 65536):
            parser = FirstGifFrame()
            data = BLUE[:-1] + b'\x21\xfe\xff' + b'x' * 255 + b'\x00\x3b'
            result = None
            for offset in range(0, len(data), size):
                result = parser.feed(data[offset:offset+size])
                if result is not None:
                    break
            self.assertEqual(result, BLUE)

    def test_truncated_image_cannot_be_mistaken_for_a_complete_frame(self):
        for length in range(len(BLUE) - 2):
            self.assertIsNone(FirstGifFrame().feed(BLUE[:length]))

    def test_non_gif_and_empty_first_image_are_not_synthesized(self):
        with self.assertRaises(InvalidGif):
            FirstGifFrame().feed(b'<html>not gif</html>')
        with self.assertRaises(InvalidGif):
            FirstGifFrame().feed(BLUE[:-5] + b'\x00\x3b')

    def test_first_frame_still_obeys_original_resource_limit(self):
        parser = FirstGifFrame()
        parser.feed(BLUE[:19] + b'\x21\xfe')
        chunk = b'\xff' + b'x' * 255
        with self.assertRaises(CaptureError) as caught:
            for _ in range(MAX_FRAME_BYTES // 256 + 1):
                parser.feed(chunk)
        self.assertEqual(caught.exception.code, 'byte_budget')

    def test_image_descriptor_cannot_bypass_logical_screen_pixel_limit(self):
        data = bytearray(BLUE)
        data[24:26] = (65535).to_bytes(2, 'little')
        data[26:28] = (65535).to_bytes(2, 'little')
        with self.assertRaises(InvalidGif):
            FirstGifFrame().feed(data)


class Resolver:
    def resolve(self, _host, _port):
        return [ipaddress.ip_address('93.184.216.34')]

    def close(self):
        pass


class GifRangeTests(unittest.TestCase):
    def run_ranges(self, change=None):
        data = BLUE[:19] + b'\x21\xfe' + (b'\xff' + b'x' * 255) * 800 + b'\x00' + BLUE[19:]
        requests = []
        proxy = SimpleNamespace(policy=PublicURLPolicy(resolver=Resolver()), budget=lambda: None,
                                deadline=time.monotonic() + 5, error=None)
        charged = []

        def respond(request):
            requests.append(request)
            start, end = map(int, request.headers['range'][6:].split('-'))
            end = min(end, len(data) - 1)
            headers = {'Content-Type': 'image/gif', 'ETag': '"stable"',
                       'Content-Range': f'bytes {start}-{end}/{len(data)}'}
            if change:
                change(headers, len(requests))
            return httpx.Response(206, content=data[start:end+1], headers=headers)

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result = fetch_first_frame('http://example.com/ranges.gif', {}, proxy,
                                       lambda: charged.append(True), client=client)
        return result, requests, charged, data

    def test_continuation_reassembles_one_stable_gif_and_charges_each_request(self):
        result, requests, charged, data = self.run_ranges()
        self.assertEqual(result[0], data)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].headers['if-range'], '"stable"')
        self.assertEqual(requests[1].headers['range'], 'bytes=131072-524287')
        self.assertEqual(len(charged), 1)

    def test_changed_validator_cannot_mix_two_versions(self):
        result, _, _, _ = self.run_ranges(lambda headers, count: headers.update(ETag='"changed"') if count == 2 else None)
        self.assertIsNone(result)

    def test_continuation_without_a_validator_is_not_attempted(self):
        result, requests, _, _ = self.run_ranges(lambda headers, _count: headers.pop('ETag'))
        self.assertIsNone(result)
        self.assertEqual(len(requests), 1)

    def test_wrong_range_offset_or_encoded_segments_are_rejected(self):
        for changes in ({'Content-Range': 'bytes 12-131083/204838'}, {'Content-Encoding': 'gzip'},
                        {'Content-Range': 'bytes 0-131071/12'}):
            with self.subTest(changes=changes):
                result, requests, _, _ = self.run_ranges(lambda headers, _count: headers.update(changes))
                self.assertIsNone(result)
                self.assertEqual(len(requests), 1)


class GifNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                cls.received.append((self.path, self.headers.get('Range')))
                if self.path in ('/private.gif', '/file.gif'):
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1/should-not-connect' if self.path == '/private.gif'
                                     else 'file:///should-not-read.gif')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/truncated.gif':
                    body, mime, status = BLUE[:-3], 'image/gif', 200
                elif self.path.endswith('.gif'):
                    # The normal animation exceeds 16 MiB; its real first frame
                    # fits in a small range. The proxy itself remains bounded.
                    body = BLUE[:-1] + (b'\x21\xfe\xff' + b'x' * 255 + b'\x00') * 68000 + b'\x3b'
                    mime, status = 'image/gif', 200
                    if self.headers.get('Range'):
                        body, status = body[:512], 206
                else:
                    body = b'<html><body><h1>Readable animation</h1><img width="100" height="100" src="/large.gif"></body></html>'
                    mime, status = 'text/html', 200
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                if status == 206:
                    self.send_header('Content-Range', 'bytes 0-511/17612035')
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
        cls.thread.join(2)

    def proxy(self, max_bytes=64*1024*1024):
        def connect(address, timeout):
            self.assertEqual(address, ('93.184.216.34', 80))
            return socket.create_connection(('127.0.0.1', self.server.server_port), timeout=timeout)
        return PinnedPublicProxy(Resolver(), max_bytes, time.monotonic() + 40, connector=connect).start()

    def test_proxy_stream_returns_only_complete_first_frame(self):
        proxy = self.proxy()
        try:
            result = fetch_first_frame('http://example.com/large.gif', {}, proxy, lambda: None)
            self.assertEqual(result[0], BLUE)
            self.assertLess(proxy.bytes, 4096)
        finally:
            proxy.close()

    def test_redirect_to_private_address_is_rejected_before_connection(self):
        proxy = self.proxy()
        try:
            with self.assertRaises(CaptureError) as caught:
                fetch_first_frame('http://example.com/private.gif', {}, proxy, lambda: None)
            self.assertEqual(caught.exception.code, 'unsafe_url')
            self.assertEqual(proxy.connections, 1)
        finally:
            proxy.close()

    def test_truncated_response_does_not_return_a_fake_static_image(self):
        proxy = self.proxy()
        try:
            self.assertIsNone(fetch_first_frame('http://example.com/truncated.gif', {}, proxy, lambda: None))
        finally:
            proxy.close()

    def test_non_http_redirect_is_rejected_before_reading_destination(self):
        proxy = self.proxy()
        try:
            with self.assertRaises(CaptureError) as caught:
                fetch_first_frame('http://example.com/file.gif', {}, proxy, lambda: None)
            self.assertEqual(caught.exception.code, 'unsafe_url')
            self.assertEqual(proxy.connections, 1)
        finally:
            proxy.close()

    def test_network_byte_limit_remains_enforced(self):
        proxy = self.proxy(max_bytes=128)
        try:
            with self.assertRaises(CaptureError) as caught:
                fetch_first_frame('http://example.com/large.gif', {}, proxy, lambda: None)
            self.assertEqual(caught.exception.code, 'byte_budget')
        finally:
            proxy.close()

    def test_expired_original_deadline_cannot_start_an_image_request(self):
        proxy = self.proxy()
        try:
            proxy.deadline = time.monotonic() - 1
            with self.assertRaises(CaptureError) as caught:
                fetch_first_frame('http://example.com/large.gif', {}, proxy, lambda: None)
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertEqual(proxy.connections, 0)
        finally:
            proxy.close()

    @unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
    def test_large_animation_archives_and_decodes_to_its_actual_blue_pixel(self):
        proxy = self.proxy()
        try:
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(str(uuid4()), 'test', 1, 'http://example.com/', directory, development=True)
                result = _capture(request, proxy=proxy)
                html = Path(result.artifact_path).read_text('utf-8')
                self.assertIn('Readable animation', html)
                self.assertIn('animated-image-first-frame', result.warnings)
                encoded = re.search(r'data:image/gif;base64,([A-Za-z0-9+/=]+)', html).group(1)
                self.assertEqual(base64.b64decode(encoded), BLUE)
                self.assertLess(proxy.bytes, 1024 * 1024)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page()
                    pixel = page.evaluate('''async data => {const image=new Image();image.src=data;await image.decode();
                        const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
                        const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);
                        return [...ctx.getImageData(0,0,1,1).data];}''', 'data:image/gif;base64,' + encoded)
                    browser.close()
                self.assertEqual(pixel, [0, 0, 255, 255])
        finally:
            proxy.close()
