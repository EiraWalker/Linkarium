"""Strong WAF response markers must not become successful, unstyled archives."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from capture.diagnostics import CaptureTrace
from capture.models import CaptureError, CaptureRequest
from capture.runner import capture


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class ChallengeResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.counts = {}

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                status, mime, marker = 200, 'text/html', None
                body = '<!doctype html><html><body><h1>Readable main page</h1></body></html>'
                if path == '/aws-document':
                    status, marker = 202, ('x-amzn-waf-action', 'challenge')
                elif path == '/cf-document':
                    status, marker = 403, ('cf-mitigated', 'challenge')
                elif path == '/forbidden':
                    status = 403
                elif path == '/aws-style.css':
                    status, marker, body = 202, ('x-amzn-waf-action', 'challenge'), ''
                elif path == '/captcha-style.css':
                    status, marker, body = 405, ('x-amzn-waf-action', 'captcha'), ''
                elif path == '/image.png':
                    status, marker, body = 202, ('x-amzn-waf-action', 'challenge'), ''
                elif path == '/recover.css':
                    cls.counts[path] = cls.counts.get(path, 0) + 1
                    if cls.counts[path] == 1:
                        status, marker, body = 202, ('x-amzn-waf-action', 'challenge'), ''
                    else:
                        mime, body = 'text/css', 'body{background:rgb(210,220,230)}'
                elif path == '/recover-document' and 'ready=1' not in self.path:
                    status, marker = 202, ('x-amzn-waf-action', 'challenge')
                    body += '<script>setTimeout(()=>location.replace("/recover-document?ready=1"),50)</script>'
                elif path in ('/style-page', '/captcha-page'):
                    resource = '/aws-style.css?secret=DO_NOT_LOG' if path == '/style-page' else '/captcha-style.css'
                    body = f'<html><head><link rel="stylesheet" href="{resource}"></head><body>Readable but unstyled page</body></html>'
                elif path == '/image-page':
                    body += '<img width="100" height="60" src="/image.png">'
                elif path == '/frame-page':
                    body += '<iframe src="/aws-document"></iframe>'
                elif path == '/recover-page':
                    body = '''<html><head><link id="css" rel="stylesheet" href="/recover.css"></head>
                      <body>Recovered page<script>setTimeout(()=>{document.querySelector('#css').remove();
                      const style=document.createElement('link');style.rel='stylesheet';style.href='/recover.css';
                      document.head.append(style)},150)</script></body></html>'''
                data = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                if marker:
                    self.send_header(*marker)
                self.end_headers()
                self.wfile.write(data)

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def run_case(self, path, error=None):
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(attempt_uuid=str(uuid.uuid4()), generation='test', library_epoch=1,
                url=f'http://127.0.0.1:{self.server.server_port}{path}', output_dir=directory,
                development=True, allow_test_hosts=('127.0.0.1',))
            trace = Path(directory) / 'diagnostics.json'
            if error:
                with self.assertRaises(CaptureError) as caught, CaptureTrace(trace, request):
                    capture(request)
                self.assertEqual(caught.exception.code, error)
                self.assertFalse(list(Path(directory).glob('*.html')))
                evidence = json.loads(trace.read_text('utf-8'))
                self.assertNotIn('DO_NOT_LOG', trace.read_text('utf-8'))
                return evidence
            with CaptureTrace(trace, request):
                result = capture(request)
            self.assertTrue(Path(result.artifact_path).is_file())
            return result

    def test_main_document_waf_and_cloudflare_are_challenges(self):
        for path in ('/aws-document', '/cf-document'):
            with self.subTest(path=path):
                self.run_case(path, error='challenge')

    def test_stylesheet_challenge_is_not_accepted_as_empty_css(self):
        for path in ('/style-page', '/captcha-page'):
            with self.subTest(path=path):
                evidence = self.run_case(path, error='challenge')
                events = [event for event in evidence['events'] if event['stage'] == 'resource_challenge']
                self.assertTrue(events)
                self.assertEqual(events[0]['provider'], 'aws-waf')
                self.assertEqual(events[0]['resource_type'], 'stylesheet')
                self.assertEqual(set(events[0]), {'stage', 'elapsed_seconds', 'provider', 'resource_type', 'host', 'http_status'})

    def test_ordinary_403_keeps_access_denied_error(self):
        self.run_case('/forbidden', error='access_denied')

    def test_source_image_challenge_stays_a_warning(self):
        result = self.run_case('/image-page')
        self.assertIn('source-broken-images:1', result.warnings)
        self.assertIn('resource-challenge:aws-waf:image', result.warnings)

    def test_child_frame_challenge_does_not_reject_main_page(self):
        result = self.run_case('/frame-page')
        self.assertIn('resource-challenge:aws-waf:document', result.warnings)

    def test_later_successful_stylesheet_clears_challenge(self):
        self.counts.pop('/recover.css', None)
        self.run_case('/recover-page')
        self.assertGreaterEqual(self.counts['/recover.css'], 2)

    def test_later_successful_main_navigation_clears_challenge(self):
        self.run_case('/recover-document')
