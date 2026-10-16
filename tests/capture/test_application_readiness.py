"""Real async application updates must finish before a static first-screen freeze."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

import html5lib

from capture.diagnostics import CaptureTrace
from capture.models import CaptureError, CaptureRequest
from capture.runner import capture


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class ApplicationReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release_stalled_requests = threading.Event()
        cls.stalled_request_started = threading.Event()

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                mime = 'text/html'
                if self.path in ('/app.js', '/late-app.js'):
                    time.sleep(12 if self.path == '/late-app.js' else 2.5)
                    mime = 'text/javascript'
                    body = 'document.querySelector("main").innerHTML="<h1>Initialized application</h1>";'
                elif self.path == '/picture.svg':
                    time.sleep(2.5)
                    mime = 'image/svg+xml'
                    body = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="blue"/></svg>'
                elif self.path in ('/async', '/late-async'):
                    script = '/late-app.js' if self.path == '/late-async' else '/app.js'
                    body = f'<html><head><script async src="{script}"></script></head><body><main>Loading application</main></body></html>'
                elif self.path == '/image':
                    body = '''<html><body><main><h1>Real article</h1></main><script>
                    setTimeout(() => {const img = new Image(80,80); img.id='late-picture';
                    img.src='/picture.svg';document.querySelector('main').append(img);}, 700);
                    </script></body></html>'''
                elif self.path == '/data':
                    time.sleep(2.5)
                    mime = 'application/json'
                    body = '{"title":"Loaded application data"}'
                elif self.path == '/tick':
                    mime = 'application/json'
                    body = '{"ok":true}'
                elif self.path == '/stalled-data':
                    cls.stalled_request_started.set()
                    cls.release_stalled_requests.wait(20)
                    mime = 'application/json'
                    body = '{"ok":true}'
                elif self.path == '/poll':
                    body = '''<html><body><h1>Readable polling page</h1><script>
                    fetch('/tick');setInterval(()=>fetch('/tick'),100);
                    </script></body></html>'''
                elif self.path == '/stalled':
                    body = '''<html><body><h1>Readable stalled page</h1><script>
                    fetch('/stalled-data');
                    </script></body></html>'''
                elif self.path == '/hidden':
                    body = '''<html><body><h1>Readable stable page</h1>
                    <div id="hidden-widget" style="visibility:hidden;width:80px;height:80px">0</div>
                    <script>let count=0;setInterval(()=>{
                    document.querySelector('#hidden-widget').textContent=String(++count);},50);
                    </script></body></html>'''
                elif self.path == '/offscreen':
                    body = '''<html><body><h1>Readable stable page</h1><script>
                    setInterval(()=>{const node=document.createElement('div');
                    node.style.cssText='position:absolute;top:2000px;width:80px;height:80px';
                    node.textContent='Offscreen update';document.body.append(node);},50);
                    </script></body></html>'''
                elif self.path == '/replace-image':
                    initial = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">'
                               '<rect width="80" height="80" fill="green"/></svg>')
                    body = ('<html><body><h1>Real article</h1><img id="replaced-picture" width="80" height="80" '
                            'src="data:image/svg+xml;base64,' + base64.b64encode(initial.encode()).decode() + '">'
                            '<script>setTimeout(()=>{document.querySelector("#replaced-picture").src="/picture.svg";},900);'
                            '</script></body></html>')
                else:
                    body = '''<html><body><main>Loading application</main><script>
                    fetch('/data').then(r=>r.json()).then(data=>{
                    document.querySelector('main').textContent=data.title;});
                    </script></body></html>'''
                data = body.encode()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.release_stalled_requests.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def archive(self, path):
        html, result, _ = self.archive_with_evidence(path)
        return html, result

    def archive_with_evidence(self, path):
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}{path}', directory,
                development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=30)
            trace = Path(directory) / 'trace.json'
            with CaptureTrace(trace, request):
                result = capture(request)
            return Path(result.artifact_path).read_text('utf8'), result, json.loads(trace.read_text('utf8'))

    def test_slow_async_script_is_not_frozen_as_initial_placeholder(self):
        html, _ = self.archive('/async')
        self.assertIn('Initialized application', html)
        self.assertNotIn('Loading application', html)

    def test_unfinished_app_bundle_gets_one_bounded_window_before_freeze(self):
        html, _, evidence = self.archive_with_evidence('/late-async')
        self.assertIn('Initialized application', html)
        self.assertNotIn('Loading application', html)
        windows = [row for row in evidence['events'] if row['stage'] == 'application_script_wait']
        self.assertEqual(len(windows), 1)
        ready = next(row for row in evidence['events'] if row['stage'] == 'application_ready')
        self.assertLess(ready['observation_seconds'], 20)

    def test_spa_image_added_after_first_readiness_check_is_fully_archived(self):
        html, result = self.archive('/image')
        self.assertIn('id="late-picture"', html)
        self.assertIn('data-capture-source-image="loaded"', html)
        self.assertFalse(any(w.startswith('source-broken-images:') for w in result.warnings))

    def test_same_origin_data_request_finishes_before_freezing_placeholder(self):
        html, _ = self.archive('/fetch')
        self.assertIn('Loaded application data', html)
        self.assertNotIn('Loading application', html)

    def test_hidden_continuous_mutation_does_not_hold_readable_page(self):
        html, _, evidence = self.archive_with_evidence('/hidden')
        self.assertIn('Readable stable page', html)
        self.assertFalse(any(row['stage'] == 'readiness_limit' for row in evidence['events']))
        readiness = next(row for row in evidence['events'] if row['stage'] == 'application_ready')
        self.assertLess(readiness['observation_seconds'], 4)

    def test_offscreen_body_insertions_do_not_hold_readable_page(self):
        html, _, evidence = self.archive_with_evidence('/offscreen')
        self.assertIn('Readable stable page', html)
        self.assertFalse(any(row['stage'] == 'readiness_limit' for row in evidence['events']))
        readiness = next(row for row in evidence['events'] if row['stage'] == 'application_ready')
        self.assertLess(readiness['observation_seconds'], 4)

    def test_continuous_same_origin_polling_stops_at_phase_limit(self):
        started = time.monotonic()
        html, _, evidence = self.archive_with_evidence('/poll')
        self.assertIn('Readable polling page', html)
        events = evidence['events']
        begin = next(row for row in events if row['stage'] == 'readiness')
        limit = next(row for row in events if row['stage'] == 'readiness_limit')
        self.assertGreaterEqual(limit['elapsed_seconds'] - begin['elapsed_seconds'], 9)
        self.assertLess(limit['elapsed_seconds'] - begin['elapsed_seconds'], 12)
        self.assertFalse(any(row['stage'] == 'application_script_wait' for row in events))

    def test_stalled_request_cannot_extend_global_capture_deadline(self):
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}/stalled', directory,
                development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=2)
            with self.assertRaises(CaptureError) as caught:
                capture(request)
            self.assertTrue(self.stalled_request_started.is_set())
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertFalse(list(Path(directory).glob('*.html')))
        self.assertLess(time.monotonic() - started, 7)

    def test_existing_image_changed_to_slow_resource_is_rechecked(self):
        html, result = self.archive('/replace-image')
        document = html5lib.parse(html, namespaceHTMLElements=False)
        image = next(node for node in document.iter('img') if node.get('id') == 'replaced-picture')
        self.assertEqual(image.get('data-capture-source-image'), 'loaded')
        self.assertTrue(image.get('src', '').startswith('data:image/svg+xml;base64,'))
        resource = base64.b64decode(image.get('src').split(',', 1)[1])
        self.assertIn(b'fill="blue"', resource)
        self.assertNotIn(b'fill="green"', resource)
        self.assertFalse(any(w.startswith('source-broken-images:') for w in result.warnings))


if __name__ == '__main__':
    unittest.main()
