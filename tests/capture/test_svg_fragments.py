"""SVG paint references survive Monolith making local fragments absolute."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from capture.models import CaptureRequest
from capture.rewrite import DocumentRewriter, Resource
from capture.runner import capture, REPLAY_CSP


class SVGFragmentRewriterTests(unittest.TestCase):
    def test_absolute_same_document_fragments_are_preserved_in_css_and_svg(self):
        base = 'https://example.com/course?language=zh#old'
        rewriter = DocumentRewriter({}, [], 1024 * 1024)
        markup = '<style>.star{fill:url("https://example.com/course?language=zh#gradient")!important}</style>' \
            '<svg><defs><linearGradient id="gradient"></linearGradient></defs>' \
            '<use href="https://example.com/course?language=zh#shape"></use></svg>'
        result = rewriter.html(markup, base)
        self.assertIn('url("#gradient")', result)
        self.assertIn('href="#shape"', result)
        self.assertNotIn('data:application/octet-stream;base64,', result)

    def test_other_document_or_query_is_not_treated_as_a_local_fragment(self):
        external = 'https://example.com/icons.svg'
        changed_query = 'https://example.com/course?language=en'
        resources = {url: Resource(b'<svg></svg>', 'image/svg+xml', url)
            for url in (external, changed_query)}
        rewriter = DocumentRewriter(resources, [], 1024 * 1024)
        for value in (external + '#shape', changed_query + '#shape'):
            with self.subTest(value=value):
                self.assertTrue(rewriter.data_url(value, 'https://example.com/course?language=zh')
                    .startswith('data:image/svg+xml;base64,'))
        self.assertEqual(rewriter.data_url('https://unfetched.example/icons.svg#shape',
            'https://example.com/course?language=zh'), 'data:application/octet-stream;base64,')


SVG_FIXTURE = b'''<!doctype html><html><head><style>html,body{margin:0;background:white}
#gradient-box{fill:url(#paint)!important}#clip-box{clip-path:url(#clip)!important}
#mask-box{mask:url(#mask)!important}</style></head><body><svg width="240" height="80" viewBox="0 0 240 80">
<defs><linearGradient id="paint"><stop offset="0" stop-color="#00ff00"/>
<stop offset="1" stop-color="#0000ff"/></linearGradient>
<clipPath id="clip"><circle cx="120" cy="40" r="24"/></clipPath>
<mask id="mask"><rect width="240" height="80" fill="white"/><circle cx="200" cy="40" r="20" fill="black"/></mask></defs>
<rect id="gradient-box" width="80" height="80"/>
<rect id="clip-box" x="80" width="80" height="80" fill="#0000ff"/>
<rect id="mask-box" x="160" width="80" height="80" fill="#ff0000"/>
</svg></body></html>'''


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class SVGFragmentCaptureTests(unittest.TestCase):
    def test_gradient_clip_and_mask_pixels_survive_actual_freeze_and_monolith(self):
        from playwright.sync_api import Page, sync_playwright
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(SVG_FIXTURE)))
                self.end_headers()
                self.wfile.write(SVG_FIXTURE)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_evaluate = Page.evaluate
        source = []
        clip = {'x': 0, 'y': 0, 'width': 240, 'height': 80}
        def evaluate(page, expression, arg=None):
            if isinstance(expression, str) and 'const serializeRoot = root =>' in expression:
                source.append(page.screenshot(clip=clip, scale='css'))
            return original_evaluate(page, expression, arg)

        try:
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(str(uuid4()), 'test', 1,
                    f'http://127.0.0.1:{server.server_port}/', directory,
                    development=True, allow_test_hosts=('127.0.0.1',))
                with patch.object(Page, 'evaluate', evaluate):
                    result = capture(request)
                self.assertEqual(len(source), 1)
                content = Path(result.artifact_path).read_text(encoding='utf-8')
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    page = browser.new_page(viewport={'width': 1366, 'height': 768})
                    def replay(route):
                        if route.request.url == 'https://snapshot.invalid/':
                            route.fulfill(status=200, body=content, headers={
                                'Content-Type': 'text/html; charset=utf-8', 'Content-Security-Policy': REPLAY_CSP})
                        else:
                            route.abort()
                    page.route('**/*', replay)
                    page.goto('https://snapshot.invalid/', wait_until='load')
                    after = page.screenshot(clip=clip, scale='css')
                    trusted = browser.new_page()
                    difference = trusted.evaluate('''async ([before,after])=>{
                      const load=async data=>{const img=new Image();img.src='data:image/png;base64,'+data;await img.decode();
                        const c=document.createElement('canvas');c.width=img.width;c.height=img.height;
                        const ctx=c.getContext('2d');ctx.drawImage(img,0,0);return ctx.getImageData(0,0,c.width,c.height).data;};
                      const a=await load(before),b=await load(after);let changed=0;
                      for(let i=0;i<a.length;i+=4)if(Math.max(...[0,1,2].map(c=>Math.abs(a[i+c]-b[i+c])))>8)changed++;
                      return changed/(a.length/4);
                    }''', [base64.b64encode(source[0]).decode(), base64.b64encode(after).decode()])
                    browser.close()
                self.assertLess(difference, 0.005, f'SVG changed-pixel ratio: {difference}')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
