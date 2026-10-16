"""A scripted source page must not gain noscript fallback content in its archive."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

import html5lib

from capture.models import CaptureRequest
from capture.runner import capture


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class NoscriptFallbackTests(unittest.TestCase):
    def test_scripted_document_does_not_activate_unfetched_noscript_images(self):
        paths = []
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                paths.append(self.path)
                if self.path == '/shown.svg':
                    body = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="green"/></svg>'
                    mime, status = 'image/svg+xml', 200
                elif self.path == '/':
                    body = b'''<!doctype html><html><head>
                        <noscript><style>body{display:none}</style></noscript></head>
                        <body><h1>Scripted source content</h1><img src="/shown.svg">
                        <noscript><img width="200" height="150" src="/unrequested-fallback.jpg">
                        <p>Fallback must stay inactive</p></noscript></body></html>'''
                    mime, status = 'text/html', 200
                else:
                    body, mime, status = b'not found', 'text/plain', 404
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                result = capture(CaptureRequest(attempt_uuid=str(uuid4()),generation='test',library_epoch=1,
                    url=f'http://127.0.0.1:{server.server_port}/',output_dir=directory,development=True,
                    allow_test_hosts=('127.0.0.1',)))
                tree = html5lib.parse(Path(result.artifact_path).read_text(encoding='utf8'),namespaceHTMLElements=False)
                self.assertEqual(len(list(tree.iter('img'))),1)
                self.assertNotIn('Fallback must stay inactive', ''.join(tree.itertext()))
                self.assertIn('Scripted source content', ''.join(tree.itertext()))
                self.assertFalse(any('broken-images' in value for value in result.warnings))
            self.assertNotIn('/unrequested-fallback.jpg', paths)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
