"""Client-side document changes get bounded retries and fresh safety checks."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from capture.diagnostics import CaptureTrace
from capture.models import CaptureError, CaptureRequest
from capture.runner import _LateNavigation, capture


class LateNavigationBudgetTests(unittest.TestCase):
    def test_restart_shares_original_deadline_and_cumulative_budgets(self):
        calls, sentinel = [], object()

        def attempt(request, **kwargs):
            calls.append((request, kwargs))
            if len(calls) == 1:
                kwargs['budget'].update(bytes=1234, requests=37)
                raise _LateNavigation('https://second.example/final')
            return sentinel

        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1, 'https://first.example/', directory, development=True)
            with patch('capture.runner._capture', side_effect=attempt), patch('capture.proxy_config.request_proxy', return_value=None):
                self.assertIs(capture(request), sentinel)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0].url, 'https://second.example/final')
        self.assertEqual(calls[0][1]['deadline'], calls[1][1]['deadline'])
        self.assertIs(calls[0][1]['budget'], calls[1][1]['budget'])
        self.assertEqual(calls[1][1]['budget'], {'bytes': 1234, 'requests': 37, 'error': None})
        self.assertIs(calls[0][1]['proxy'], calls[1][1]['proxy'])

    def test_late_navigation_cannot_restart_indefinitely(self):
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1, 'https://first.example/', directory, development=True)
            with patch('capture.runner._capture', side_effect=_LateNavigation('https://second.example/')) as attempt, \
                    patch('capture.proxy_config.request_proxy', return_value=None), self.assertRaises(CaptureError) as caught:
                capture(request)
            self.assertEqual(attempt.call_count, 2)
            self.assertEqual(caught.exception.code, 'navigation_failed')


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class NavigationReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                target = urlsplit(self.path)
                query = parse_qs(target.query)
                status, marker, mime = 200, None, 'text/html'
                body = '<!doctype html><html><head><title>Final readable page</title></head><body><h1>Final readable content</h1></body></html>'
                if target.path == '/slow.svg':
                    time.sleep(0.8)
                    mime = 'image/svg+xml'
                    body = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="blue"/></svg>'
                elif target.path == '/redirect':
                    destination = query.get('to', ['/final?secret=DO_NOT_LOG'])[0]
                    body = self.redirect(destination)
                elif target.path == '/same-url':
                    body = '''<!doctype html><html><body><h1>Final readable content</h1>
                      <script>if (!sessionStorage.getItem('reloaded')) {
                        sessionStorage.setItem('reloaded','1');
                        document.write('<img src="/slow.svg" width="80" height="80">');
                        setTimeout(()=>location.reload(),100);
                      }</script></body></html>'''
                elif target.path == '/loop':
                    number = int(query.get('n', ['0'])[0])
                    body = self.redirect(f'/loop?n={number+1}', delay=600)
                elif target.path == '/history':
                    body = body.replace('</body>', '<script>setTimeout(()=>history.replaceState(null,"","/renamed#section"),1800)</script></body>')
                elif target.path in ('/late', '/late403'):
                    destination = '/final' if target.path == '/late' else '/403'
                    body = body.replace('</body>', '<script>setTimeout(()=>location.replace(' + json.dumps(destination) + '),1800)</script></body>')
                elif target.path == '/404':
                    status = 404
                elif target.path == '/403':
                    status = 403
                elif target.path == '/waf':
                    status, marker = 202, ('x-amzn-waf-action', 'challenge')
                elif target.path == '/challenge-title':
                    body = '<html><head><title>Verify you are human</title></head><body>Verification required</body></html>'
                data = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                if marker:
                    self.send_header(*marker)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            @staticmethod
            def redirect(destination, delay=100):
                return ('<!doctype html><html><body><h1>Temporary page</h1>'
                        '<img src="/slow.svg" width="80" height="80">'
                        '<script>setTimeout(()=>location.replace(' + json.dumps(destination) + '),' + str(delay) + ')</script>'
                        '</body></html>')

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def run_capture(self, path, error=None, timeout=30):
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}{path}', directory,
                development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=timeout)
            trace = Path(directory) / 'trace.json'
            if error:
                with self.assertRaises(CaptureError) as caught, CaptureTrace(trace, request):
                    capture(request)
                self.assertEqual(caught.exception.code, error)
                self.assertFalse(list(Path(directory).glob('*.html')))
                result = None
            else:
                with CaptureTrace(trace, request):
                    result = capture(request)
                html = Path(result.artifact_path).read_text('utf-8')
                self.assertIn('Final readable content', html)
                self.assertNotIn('Temporary page', html)
            self.assertNotIn('DO_NOT_LOG', trace.read_text('utf-8'))
            return result, json.loads(trace.read_text('utf-8'))

    def test_context_destroyed_by_client_redirect_recovers_final_document(self):
        result, evidence = self.run_capture('/redirect')
        self.assertTrue(result.final_url.endswith('/final?secret=DO_NOT_LOG'))
        retries = [event for event in evidence['events'] if event['stage'] == 'navigation_retry']
        self.assertTrue(retries)
        self.assertIn(retries[0]['reason'], ('execution_context_destroyed', 'document_changed'))

    def test_same_url_reload_recovers_without_reusing_old_document(self):
        _, evidence = self.run_capture('/same-url')
        self.assertTrue(any(event['stage'] == 'navigation_retry' for event in evidence['events']))

    def test_same_document_history_during_archiving_does_not_invalidate_snapshot(self):
        real_run = subprocess.run

        def archive_after_history(*args, **kwargs):
            if '-o' in args[0]:
                time.sleep(1.2)
            return real_run(*args, **kwargs)

        with patch('capture.runner.subprocess.run', archive_after_history):
            _, evidence = self.run_capture('/history')
        commits = [event for event in evidence['events'] if event['stage'] == 'document_navigation' and event['phase'] == 'commit']
        self.assertEqual(len(commits), 1)

    def test_real_navigation_during_archiving_restarts_at_final_document(self):
        real_run = subprocess.run

        def archive_after_redirect(*args, **kwargs):
            if '-o' in args[0]:
                time.sleep(1.2)
            return real_run(*args, **kwargs)

        with patch('capture.runner.subprocess.run', archive_after_redirect):
            result, evidence = self.run_capture('/late')
        self.assertTrue(result.final_url.endswith('/final'))
        retries = [event for event in evidence['events'] if event.get('reason') == 'document_changed_after_freeze']
        self.assertEqual(len(retries), 1)

    def test_late_navigation_still_rejects_final_access_denial(self):
        real_run = subprocess.run

        def archive_after_redirect(*args, **kwargs):
            if '-o' in args[0]:
                time.sleep(1.2)
            return real_run(*args, **kwargs)

        with patch('capture.runner.subprocess.run', archive_after_redirect):
            self.run_capture('/late403', error='access_denied')

    def test_redirect_destination_http_error_is_not_published(self):
        for status, code in (('403', 'access_denied'), ('404', 'not_found')):
            with self.subTest(status=status):
                self.run_capture('/redirect?to=/' + status, error=code)

    def test_redirect_destination_waf_and_title_are_checked(self):
        for destination in ('waf', 'challenge-title'):
            with self.subTest(destination=destination):
                self.run_capture('/redirect?to=/' + destination, error='challenge')

    def test_redirect_private_address_remains_blocked(self):
        self.run_capture('/redirect?to=http://127.0.0.2/private', error='unsafe_url')

    def test_repeated_navigation_stops_after_three_readiness_attempts(self):
        started = time.monotonic()
        _, evidence = self.run_capture('/loop', error='navigation_failed')
        self.assertLess(time.monotonic()-started, 20)
        self.assertEqual(len([event for event in evidence['events'] if event['stage'] == 'readiness']), 3)
        self.assertEqual(len([event for event in evidence['events'] if event['stage'] == 'navigation_retry']), 2)

    def test_navigation_retry_uses_original_deadline(self):
        started = time.monotonic()
        self.run_capture('/redirect', error='timeout', timeout=1)
        self.assertLess(time.monotonic()-started, 6)

    def test_style_and_node_budgets_are_not_reported_as_network_errors(self):
        original_read = Path.read_text
        for message in ('DOM exceeds capture node budget', 'DOM exceeds capture style budget'):
            def freeze(path, *args, **kwargs):
                if path.name == 'freeze.js':
                    return '() => {throw new Error(' + json.dumps(message) + ')}'
                return original_read(path, *args, **kwargs)

            with self.subTest(message=message), patch.object(Path, 'read_text', freeze):
                _, evidence = self.run_capture('/final', error='dom_budget')
                self.assertFalse(any(event['stage'] == 'navigation_retry' for event in evidence['events']))
