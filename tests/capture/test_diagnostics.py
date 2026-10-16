import json
from pathlib import Path
import tempfile
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from uuid import uuid4
from unittest.mock import patch

from capture.diagnostics import CaptureTrace, development_trace_path, event
from capture.models import CaptureError, CaptureRequest


class CaptureDiagnosticTests(unittest.TestCase):
    def request(self, directory):
        return CaptureRequest(str(uuid4()), 'test', 1,
            'https://name:SECRET@example.com/private/SECRET?key=SECRET#SECRET', str(directory), development=True)

    def test_exception_chain_records_network_cause_without_secret_text_or_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'trace.json'
            with self.assertRaises(CaptureError):
                with CaptureTrace(path, self.request(directory)):
                    event('navigation')
                    try:
                        raise RuntimeError('net::ERR_CONNECTION_RESET https://name:SECRET@host/path?token=SECRET Cookie: SECRET')
                    except RuntimeError as cause:
                        raise CaptureError('browser_failed', 'SECRET') from cause
            text = path.read_text('utf-8')
            body = json.loads(text)
            self.assertNotIn('SECRET', text)
            self.assertNotIn('/private/', text)
            self.assertEqual(body['host'], 'example.com')
            self.assertEqual(body['state'], 'failed')
            self.assertEqual(body['stage'], 'navigation')
            self.assertEqual(body['errors'][1]['network_codes'], ['net::ERR_CONNECTION_RESET'])

    def test_stage_is_flushed_before_completion_and_writes_do_not_fail_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'trace.json'
            with CaptureTrace(path, self.request(directory)):
                event('rewrite', html_bytes=123)
                self.assertEqual(json.loads(path.read_text())['stage'], 'rewrite')
                with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
                    event('archive')
            self.assertEqual(json.loads(path.read_text())['state'], 'succeeded')

    def test_only_development_spool_gets_automatic_persistent_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory)
            self.assertIsNone(development_trace_path(request))
            request.output_dir = str(Path(directory)/'work'/'capture'/request.attempt_uuid)
            expected = Path(directory)/'diagnostics'/'capture'/(request.attempt_uuid+'.json')
            self.assertEqual(development_trace_path(request), expected)
            request.development = False
            self.assertIsNone(development_trace_path(request))

    @unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium')
    def test_real_child_http_failure_persists_after_attempt_spool_cleanup(self):
        from capture.development import run_development_capture
        import shutil

        class Denied(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Denied)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                request = self.request(directory)
                spool = Path(directory)/'work'/'capture'/request.attempt_uuid
                request.output_dir = str(spool)
                request.url = f'http://127.0.0.1:{server.server_port}/?token=SECRET'
                request.allow_test_hosts = ('127.0.0.1',)
                with self.assertRaises(CaptureError) as error:
                    run_development_capture(request, lambda: True)
                self.assertEqual(error.exception.code, 'access_denied')
                self.assertTrue(spool.resolve().is_relative_to(Path(directory).resolve()))
                shutil.rmtree(spool)
                trace = development_trace_path(request)
                text = trace.read_text('utf-8')
                self.assertNotIn('SECRET', text)
                body = json.loads(text)
                self.assertEqual(body['state'], 'failed')
                self.assertEqual(body['events'][-1]['http_status'], 403)
                self.assertEqual(body['errors'][0]['reason'], 'http_access_denied')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
