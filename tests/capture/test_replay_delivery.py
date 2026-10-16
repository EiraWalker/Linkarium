"""Replay delivery remains bounded, private, and independent of source proxies."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import sync_playwright, Page, Route

from capture.models import CaptureError, CaptureRequest
from capture.replay_document import ReplayDocument
from capture.rewrite import DocumentRewriter
from capture.runner import capture, IMAGE_DIAGNOSTICS, REPLAY_CSP


ROOT = Path(__file__).resolve().parents[2]
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="red"/></svg>'
HTML = ('<html><body><h1>Readable replay</h1><img width="20" height="20" src="data:image/svg+xml;base64,'
        + base64.b64encode(SVG).decode() + '"></body></html>')
IMAGE_ONLY_HTML = HTML.replace('<h1>Readable replay</h1>', '')


class ReplayDocumentTests(unittest.TestCase):
    def address(self, document):
        proxy = urlsplit(document.proxy_url)
        return proxy.hostname, proxy.port

    def request(self, document, request):
        with socket.create_connection(self.address(document), timeout=2) as connection:
            connection.sendall(request)
            parts = []
            while part := connection.recv(8192):
                parts.append(part)
            return b''.join(parts)

    def test_single_document_only_and_listener_closes(self):
        document = ReplayDocument(HTML.encode(), REPLAY_CSP, time.monotonic() + 5)
        with document:
            good = f'GET {document.url} HTTP/1.1\r\nHost: snapshot.invalid\r\n\r\n'.encode()
            response = self.request(document, good)
            self.assertTrue(response.startswith(b'HTTP/1.1 200'))
            self.assertEqual(response.split(b'\r\n\r\n', 1)[1], HTML.encode())
            self.assertIn(REPLAY_CSP.encode(), response)
            bad = [
                good.replace(document.url.encode(), (document.url + '?x=1').encode()),
                good.replace(b'Host: snapshot.invalid', b'Host: other.invalid'),
                good.replace(b'Host: snapshot.invalid', b'Host: snapshot.invalid\r\nHost: snapshot.invalid'),
                good.replace(b'GET ', b'POST ', 1),
                good.replace(b'GET ', b'CONNECT ', 1),
                good.replace(b'\r\n\r\n', b'\r\nUpgrade: websocket\r\n\r\n'),
                good.replace(b'\r\n\r\n', b'\r\nTransfer-Encoding: chunked\r\n\r\n'),
                good.replace(document.url.encode(), b'http://127.0.0.1/private'),
                good.replace(document.url.encode(), b'http://example.com/public'),
                good.replace(document.url.encode(), b'file:///private.txt'),
            ]
            for request in bad:
                with self.subTest(request=request.split(b'\r\n', 1)[0][:70]):
                    response = self.request(document, request)
                    self.assertTrue(response.startswith(b'HTTP/1.1 404'))
                    self.assertNotIn(b'Readable replay', response)
        with self.assertRaises(OSError):
            socket.create_connection(self.address(document), timeout=0.2)

    def test_slow_header_obeys_shared_deadline(self):
        document = ReplayDocument(HTML.encode(), REPLAY_CSP, time.monotonic() + 0.2)
        start = time.monotonic()
        with document:
            with socket.create_connection(self.address(document), timeout=1) as connection:
                connection.sendall(b'GET ')
                self.assertEqual(connection.recv(1), b'')
        self.assertLess(time.monotonic() - start, 1)
        with self.assertRaises(OSError):
            socket.create_connection(self.address(document), timeout=0.2)

    def test_slow_reader_does_not_truncate_archive_before_global_deadline(self):
        content = b'A' * (8 * 1024 * 1024)
        document = ReplayDocument(content, REPLAY_CSP, time.monotonic() + 5)
        sender, receiver = socket.socketpair()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
        receiver.settimeout(3)
        errors = []

        def send():
            try:
                document._serve(sender)
            except Exception as error:
                errors.append(error)
            finally:
                sender.close()

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        try:
            receiver.sendall(f'GET {document.url} HTTP/1.1\r\nHost: snapshot.invalid\r\n\r\n'.encode())
            time.sleep(1.3)  # Browser is temporarily busy decoding prior data.
            parts = []
            while part := receiver.recv(64 * 1024):
                parts.append(part)
            self.assertEqual(b''.join(parts).split(b'\r\n\r\n', 1)[1], content)
            self.assertFalse(errors)
        finally:
            receiver.close()
            thread.join(2)

    def test_exit_interrupts_client_that_stops_reading_body(self):
        document = ReplayDocument(b'A' * (8 * 1024 * 1024), REPLAY_CSP, time.monotonic() + 30)
        connection = None
        try:
            with document:
                connection = socket.create_connection(self.address(document), timeout=2)
                connection.sendall(f'GET {document.url} HTTP/1.1\r\nHost: snapshot.invalid\r\n\r\n'.encode())
                self.assertIn(b'HTTP/1.1 200', connection.recv(256))
                start = time.monotonic()
            self.assertLess(time.monotonic() - start, 1)
            with self.assertRaises(OSError):
                socket.create_connection(self.address(document), timeout=0.2)
        finally:
            if connection:
                connection.close()

    def test_start_failure_closes_bound_listener(self):
        document = ReplayDocument(HTML.encode(), REPLAY_CSP, time.monotonic() + 5)
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('startup failed')):
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                document.__enter__()
        with self.assertRaises(OSError):
            socket.create_connection(self.address(document), timeout=0.2)


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class ReplayDeliveryCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                cls.requests.append(self.path)
                body = (IMAGE_ONLY_HTML if self.path == '/image-only' else HTML).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools/browsers'))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def test_large_valid_archive_replays_within_existing_total_budget(self):
        # The final sanitizer can produce a large valid archive below 64 MiB.
        # Its inert payload should not consume the capture budget in IPC copying.
        final_html = HTML.replace('</body>', '<!--' + 'A' * (63 * 1024 * 1024) + '--></body>')
        original = DocumentRewriter.html

        def final_archive(rewriter, html, base_url, *args, **kwargs):
            if kwargs.get('restore_shadow'):
                return final_html
            return original(rewriter, html, base_url, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}/', directory,
                development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=12)
            with patch.object(DocumentRewriter, 'html', final_archive):
                result = capture(request)
            artifact = Path(result.artifact_path)
            self.assertEqual(artifact.stat().st_size, len(final_html.encode()))
            self.assertLess(result.byte_size, 64 * 1024 * 1024)
            self.assertFalse(any('broken' in warning for warning in result.warnings))

    def test_replay_context_overrides_source_proxy_and_retains_csp(self):
        self.requests.clear()
        source_proxy = f'http://127.0.0.1:{self.server.server_port}'
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, proxy={'server': source_proxy, 'bypass': '<-loopback>'})
            try:
                with browser.new_context() as source:
                    source.new_page().goto('http://source.invalid/', wait_until='load')
                    source_requests = list(self.requests)
                    self.assertTrue(source_requests)
                    # Even without routes, the document proxy cannot forward an
                    # extra request; CSP also blocks page script/network access.
                    hostile = HTML.replace('</body>', '<script>window.executed=true;fetch("http://127.0.0.1/private")</script></body>')
                    with ReplayDocument(hostile.encode(), REPLAY_CSP, time.monotonic() + 5) as document:
                        with browser.new_context(service_workers='block',
                                proxy={'server': document.proxy_url, 'bypass': '<-loopback>'}) as replay:
                            page = replay.new_page()
                            response = page.goto(document.url, wait_until='load', timeout=3000)
                            self.assertEqual(response.header_value('content-security-policy'), REPLAY_CSP)
                            self.assertEqual(page.locator('h1').inner_text(), 'Readable replay')
                            self.assertEqual(page.evaluate(IMAGE_DIAGNOSTICS)['new_visible_broken'], 0)
                            self.assertFalse(page.evaluate('window.executed === true'))
                            self.assertEqual(self.requests, source_requests)
                            denied = page.goto(source_proxy + '/private', wait_until='load', timeout=3000)
                            self.assertEqual(denied.status, 404)
                            self.assertEqual(self.requests, source_requests)
                    # Opening replay did not replace the original context proxy.
                    source.new_page().goto('http://source.invalid/again', wait_until='load')
                    self.assertIn('http://source.invalid/again', self.requests)
            finally:
                browser.close()

    def test_new_broken_first_screen_image_still_rejects_archive(self):
        original = DocumentRewriter.html

        def damage(rewriter, html, base_url, *args, **kwargs):
            if kwargs.get('restore_shadow'):
                return HTML.replace('data:image/svg+xml;base64,' + base64.b64encode(SVG).decode(), 'data:image/png;base64,AAAA')
            return original(rewriter, html, base_url, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}/', directory,
                development=True, allow_test_hosts=('127.0.0.1',))
            with patch.object(DocumentRewriter, 'html', damage), patch('capture.runner.write_preview') as preview:
                with self.assertRaises(CaptureError) as caught:
                    capture(request)
                preview.assert_not_called()
            self.assertEqual(caught.exception.code, 'replay_failed')
            self.assertFalse(list(Path(directory).glob('*.html')))

    def test_empty_http_error_replay_rejects_image_only_archive(self):
        # A genuine image-only source has no text baseline. An empty HTTP error
        # would otherwise look like zero broken images and pass both checks.
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}/image-only', directory,
                development=True, allow_test_hosts=('127.0.0.1',))
            with patch.object(ReplayDocument, '_allowed', return_value=False) as allowed:
                with self.assertRaises(CaptureError) as caught:
                    capture(request)
                self.assertTrue(allowed.called)
            self.assertEqual(caught.exception.code, 'replay_failed')
            self.assertFalse(list(Path(directory).glob('*.html')))

    def test_successful_image_only_archive_still_passes_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}/image-only', directory,
                development=True, allow_test_hosts=('127.0.0.1',))
            result = capture(request)
            self.assertEqual(result.text_excerpt, '')
            self.assertTrue(Path(result.artifact_path).is_file())
            self.assertIn('data:image/svg+xml', Path(result.artifact_path).read_text(encoding='utf-8'))
            self.assertFalse(any('broken' in warning for warning in result.warnings))

    def test_isolated_replay_branch_uses_no_loopback_document_service(self):
        # Source capture remains an ordinary safe development fixture. Changing
        # this flag only after final sanitation exercises replay selection; it
        # does not simulate or claim Linux kernel/network namespace acceptance.
        original_html, original_goto = DocumentRewriter.html, Page.goto
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1,
                f'http://127.0.0.1:{self.server.server_port}/', directory,
                development=True, allow_test_hosts=('127.0.0.1',))

            def final_html(rewriter, html, base_url, *args, **kwargs):
                result = original_html(rewriter, html, base_url, *args, **kwargs)
                if kwargs.get('restore_shadow'):
                    request.development = False
                return result

            def observe(page, target, *args, **kwargs):
                response = original_goto(page, target, *args, **kwargs)
                if target == 'https://snapshot.invalid/':
                    observed.append((response.status, response.header_value('content-security-policy'),
                        page.evaluate(IMAGE_DIAGNOSTICS)['new_visible_broken']))
                return response

            with patch.object(DocumentRewriter, 'html', final_html), patch.object(Page, 'goto', observe), \
                    patch('capture.runner.ReplayDocument', side_effect=AssertionError('isolated replay cannot open loopback')) as local:
                result = capture(request)
                local.assert_not_called()
            self.assertEqual(observed, [(200, REPLAY_CSP, 0)])
            self.assertIn('Readable replay', Path(result.artifact_path).read_text('utf8'))

    def test_isolated_replay_keeps_http_and_broken_image_gates(self):
        original_html, original_fulfill = DocumentRewriter.html, Route.fulfill
        for failure in ('http_error', 'broken_image'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(str(uuid4()), 'test', 1,
                    f'http://127.0.0.1:{self.server.server_port}/image-only', directory,
                    development=True, allow_test_hosts=('127.0.0.1',))

                def final_html(rewriter, html, base_url, *args, **kwargs):
                    result = original_html(rewriter, html, base_url, *args, **kwargs)
                    if kwargs.get('restore_shadow'):
                        request.development = False
                        if failure == 'broken_image':
                            return '<html><body><img width="20" height="20" src="data:image/png;base64,AAAA"></body></html>'
                    return result

                def fulfill(route, *args, **kwargs):
                    if route.request.url == 'https://snapshot.invalid/' and failure == 'http_error':
                        kwargs.update(status=404, body='')
                    return original_fulfill(route, *args, **kwargs)

                with patch.object(DocumentRewriter, 'html', final_html), patch.object(Route, 'fulfill', fulfill), \
                        patch('capture.runner.ReplayDocument', side_effect=AssertionError('isolated replay cannot open loopback')):
                    with self.assertRaises(CaptureError) as caught:
                        capture(request)
                self.assertEqual(caught.exception.code, 'replay_failed')
                self.assertFalse(list(Path(directory).glob('*.html')))
