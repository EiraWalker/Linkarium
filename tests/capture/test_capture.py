import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from capture import CaptureError, CaptureRequest, capture
from capture.network import PublicURLPolicy
from capture.rewrite import DocumentRewriter, Resource
from capture.preview import validate_preview

FIXTURE = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dynamic fixture</title><link rel="stylesheet" href="/style.css"></head>
<body><h1 id="title">Loading</h1><div class="layout"><div class="visual" id="visual"></div>
<canvas id="canvas" width="200" height="100"></canvas><div id="shadow"></div></div>
<input id="input" value="before"><select id="select"><option>before</option><option>chosen</option></select>
<iframe src="/frame" width="260" height="80"></iframe>
<script>setTimeout(()=>{
title.textContent=innerWidth<600?'Mobile dynamic content':'Desktop dynamic content';
input.value='current edited value';select.selectedIndex=1;
const drawing=canvas.getContext('2d');drawing.fillStyle='#2683ff';drawing.fillRect(0,0,200,100);
drawing.fillStyle='#fff';drawing.font='20px sans-serif';drawing.fillText('Canvas state',12,54);
const root=shadow.attachShadow({mode:'closed'});root.innerHTML='<style>:host{display:block}p{color:#126329;font-size:20px}</style><p>Shadow dynamic text</p>';
const style=document.createElement('style');document.head.appendChild(style);style.sheet.insertRule('#visual::before{content:"CSSOM dynamic pseudo";color:#8f3074}');
},100);</script></body></html>'''
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="#e2bd67"/><circle cx="80" cy="45" r="30" fill="#6b4584"/></svg>'


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            body, mime = FIXTURE.encode(), 'text/html'
        elif self.path == '/style.css':
            body = b'body{font-family:Arial;margin:24px;background:#f6f7f9;color:#161c27}.layout{display:flex;gap:20px}.visual{background:url("/asset.svg");width:160px;height:90px}@media(max-width:600px){.layout{display:block}.visual{margin-bottom:12px}}'
            mime = 'text/css'
        elif self.path == '/asset.svg':
            body, mime = SVG, 'image/svg+xml'
        elif self.path == '/frame':
            body, mime = b'<html><body style="background:#def4ed">Frame initial<script>document.body.firstChild.textContent="Frame dynamic text"</script></body></html>', 'text/html'
        else:
            body, mime = b'', 'text/plain'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def fixture_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class NetworkPolicyTests(unittest.TestCase):
    def test_private_malformed_and_alternate_ip_targets_are_rejected(self):
        policy = PublicURLPolicy()
        for url in ('http://127.0.0.1/', 'http://[::1]/', 'http://2130706433/', 'http://127.1/',
                    'http://10.0.0.2/', 'http://169.254.169.254/latest/', 'file:///etc/passwd',
                    'https://example.com:8443/', 'http://user:password@example.com/'):
            with self.subTest(url=url), self.assertRaises(CaptureError):
                policy.validate(url)

    def test_mixed_dns_is_rejected(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))]
        with patch('socket.getaddrinfo', return_value=answers), self.assertRaises(CaptureError):
            PublicURLPolicy().validate('https://example.com/')

    def test_production_direct_capture_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1, 'https://example.com/', directory)
            with patch.dict(os.environ, {'BOOKMARK_CAPTURE_ISOLATED': ''}), self.assertRaises(CaptureError) as caught:
                capture(request)
            self.assertEqual(caught.exception.code, 'isolation_required')


class RewriterTests(unittest.TestCase):
    def test_nested_css_urls_and_html_execution_are_removed(self):
        resources = {'https://example.com/img.svg': Resource(SVG, 'image/svg+xml', 'https://example.com/img.svg')}
        rewriter = DocumentRewriter(resources, [], 1024*1024)
        html = rewriter.html('<style>@media(min-width:1px){div{background:url("img.svg")}}</style>'
            '<script>alert(1)</script><div onclick="alert(1)">Text</div><a href="javascript:alert(1)">Link</a>'
            '<form action="/api"><input value="x"></form><meta http-equiv="refresh" content="0;url=/api">', 'https://example.com/')
        self.assertIn('data:image/svg+xml;base64,', html)
        self.assertNotIn('<script', html)
        self.assertNotIn('onclick=', html)
        self.assertNotIn('javascript:', html)
        self.assertNotIn('http-equiv=', html)
        self.assertNotIn('action=', html)


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Opt-in real Chromium/Monolith integration')
class BrowserIntegrationTests(unittest.TestCase):
    def test_dynamic_desktop_mobile_html_and_metadata(self):
        with fixture_server() as url, tempfile.TemporaryDirectory() as directory:
            for variant in ('desktop', 'mobile'):
                request = CaptureRequest(str(uuid4()), 'test', 1, url, directory, variant=variant,
                    development=True, allow_test_hosts=('127.0.0.1',))
                result = capture(request)
                html = Path(result.artifact_path).read_text('utf-8')
                self.assertEqual(result.monolith_version, 'monolith 2.10.1')
                self.assertIn(f'{variant.title()} dynamic content', html)
                self.assertIn('current edited value', html)
                self.assertIn('CSSOM dynamic pseudo', html)
                self.assertIn('Shadow dynamic text', html)
                self.assertIn('Frame dynamic text', html)
                self.assertIn('data:image/png;base64,', html)
                self.assertIn('shadowrootmode="open"', html)
                self.assertNotIn('<script', html)
                self.assertEqual(result.viewport_width, 1366 if variant == 'desktop' else 390)
                self.assertGreater(result.byte_size, 500)
                details = validate_preview(result.preview_path, result.preview_sha256,
                    result.preview_byte_size, result.viewport_width, result.viewport_height)
                self.assertGreater(details['byte_size'], 1000)
                self.assertEqual(Path(result.preview_path).name, details['sha256'] + '.jpg')
                # Decode the produced image, not just its header, and confirm it
                # is one CSS viewport for this device, rather than a full page.
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        encoded = base64.b64encode(Path(result.preview_path).read_bytes()).decode()
                        dimensions = page.evaluate('''async encoded => {
                            const image = new Image(); image.src = 'data:image/jpeg;base64,' + encoded;
                            await image.decode(); return [image.naturalWidth, image.naturalHeight];
                        }''', encoded)
                        self.assertEqual(dimensions, [result.viewport_width, result.viewport_height])
                    finally:
                        browser.close()
            metadata = capture(CaptureRequest(str(uuid4()), 'test', 1, url, directory, mode='metadata',
                development=True, allow_test_hosts=('127.0.0.1',)))
            self.assertIsNone(metadata.artifact_path)
            self.assertIsNone(metadata.preview_path)
            self.assertIn('Desktop dynamic content', metadata.text_excerpt)


if __name__ == '__main__':
    unittest.main()
