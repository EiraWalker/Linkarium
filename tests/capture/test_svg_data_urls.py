"""SVG data URL payloads survive Monolith's URL query parsing unchanged."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote, unquote_to_bytes
from uuid import uuid4

import html5lib

from capture.models import CaptureRequest
from capture.rewrite import DocumentRewriter
from capture.runner import capture


XML = '<?xml version="1.0" encoding="UTF-8"?>'
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="#00aa55"/></svg>'
VIEW_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40" viewBox="0 0 80 40">' \
    '<view id="green" viewBox="0 0 40 40"/><rect width="40" height="40" fill="#00aa55"/>' \
    '<rect x="40" width="40" height="40" fill="#0033cc"/></svg>'


def svg_uri(markup, fragment=''):
    # Real sites leave '?' in their XML declaration unescaped. Chromium accepts
    # it as image data, while Monolith otherwise drops everything after it.
    return 'data:image/svg+xml;charset=utf-8,' + quote(markup, safe='?') + fragment


def decoded_svg(uri):
    content, _, fragment = uri.partition('#')
    header, payload = content.split(',', 1)
    return (base64.b64decode(payload) if ';base64' in header else unquote_to_bytes(payload)), fragment


class SVGDataURLRewriterTests(unittest.TestCase):
    def test_xml_question_marks_are_encoded_without_losing_mime_data_or_fragment(self):
        original = svg_uri(XML + VIEW_SVG, '#green?view=1')
        rewritten = DocumentRewriter({}, [], 1024 * 1024).data_url(original, 'https://example.invalid/')
        self.assertIn('%3C%3Fxml', rewritten)
        self.assertTrue(rewritten.startswith('data:image/svg+xml;charset=utf-8,'))
        self.assertTrue(rewritten.endswith('#green?view=1'))
        self.assertIn('%2300aa55', rewritten)
        self.assertEqual(decoded_svg(rewritten), decoded_svg(original))
        self.assertEqual(DocumentRewriter({}, [], 1024 * 1024).data_url(rewritten, ''), rewritten)

    def test_regular_base64_and_non_svg_data_urls_are_unchanged(self):
        values = [svg_uri(SVG, '#green'), 'data:image/svg+xml;base64,' + base64.b64encode((XML + SVG).encode()).decode(),
            'data:text/plain;charset=utf-8,question?mark#fragment']
        for value in values:
            with self.subTest(value=value[:80]):
                self.assertEqual(DocumentRewriter({}, [], 1024 * 1024).data_url(value, ''), value)


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class SVGDataURLCaptureTests(unittest.TestCase):
    def test_actual_monolith_keeps_xml_declared_svg_bytes(self):
        source = XML + SVG
        value = DocumentRewriter({}, [], 1024 * 1024).data_url(svg_uri(source), '')
        markup = '<!doctype html><html><body><img src="' + html.escape(value, quote=True) + '"></body></html>'
        binary = Path(__file__).resolve().parents[2] / 'tools/monolith/monolith.exe'
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([str(binary), '-j', '-M', '-d', 'capture.invalid', '-t', '1',
                '-b', 'https://example.invalid/', '-o', 'archive.html', '-'], input=markup.encode(),
                capture_output=True, timeout=10, check=True, cwd=directory)
            tree = html5lib.parse((Path(directory) / 'archive.html').read_text(encoding='utf8'), namespaceHTMLElements=False)
        self.assertEqual(decoded_svg(next(tree.iter('img')).get('src'))[0], source.encode())

    def test_xml_regular_and_fragment_svgs_keep_source_pixels_through_capture(self):
        from playwright.sync_api import Page
        values = [svg_uri(XML + SVG), svg_uri(SVG), svg_uri(XML + VIEW_SVG, '#green')]
        markup = '<!doctype html><html><head><style>html,body{margin:0}body{display:flex}' \
            'img{display:block;width:80px;height:40px}</style></head><body>' + ''.join(
                '<img src="' + html.escape(value, quote=True) + '">' for value in values) + '</body></html>'
        body = markup.encode()

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_evaluate, original_goto = Page.evaluate, Page.goto
        screenshots = {}
        clip = {'x': 0, 'y': 0, 'width': 240, 'height': 40}

        def evaluate(page, expression, *args, **kwargs):
            if isinstance(expression, str) and 'const serializeRoot = root =>' in expression:
                self.assertTrue(original_evaluate(page, '() => Array.from(document.images).every(e=>e.complete&&e.naturalWidth>0)'))
                screenshots['source'] = page.screenshot(clip=clip, scale='css')
            return original_evaluate(page, expression, *args, **kwargs)

        def goto(page, target, *args, **kwargs):
            result = original_goto(page, target, *args, **kwargs)
            if target.startswith('http://snapshot.invalid/'):
                screenshots['replay'] = page.screenshot(clip=clip, scale='css')
            return result

        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(Page, 'evaluate', evaluate), patch.object(Page, 'goto', goto):
                result = capture(CaptureRequest(str(uuid4()), 'test', 1,
                    f'http://127.0.0.1:{server.server_port}/', directory,
                    development=True, allow_test_hosts=('127.0.0.1',)))
                tree = html5lib.parse(Path(result.artifact_path).read_text(encoding='utf8'), namespaceHTMLElements=False)
                images = list(tree.iter('img'))
                self.assertEqual(len(images), 3)
                self.assertEqual([decoded_svg(e.get('src')) for e in images], [decoded_svg(value) for value in values])
            self.assertEqual(screenshots['source'], screenshots['replay'], 'Archive SVG pixels must match the live source exactly')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
