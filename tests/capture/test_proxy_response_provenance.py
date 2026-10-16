"""Local-only HTTP fixtures for proxy-generated versus upstream errors."""
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from capture.diagnostics import CaptureTrace, event
from capture.doh_proxy import PinnedPublicProxy
from capture.models import CaptureError, CaptureRequest


class StaticResolver:
    def resolve(self, host, port):
        return [ipaddress.ip_address('93.184.216.34')]

    def close(self):
        pass


class OriginConnector:
    """A socket pair substitutes transport after public-address validation."""
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.threads = []

    def __call__(self, address, timeout):
        payload = next(self.payloads)
        if isinstance(payload, Exception):
            raise payload
        upstream, remote = socket.socketpair()

        def respond():
            try:
                remote.settimeout(3)
                request = b''
                while b'\r\n\r\n' not in request:
                    chunk = remote.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                remote.sendall(payload)
            finally:
                remote.close()

        thread = threading.Thread(target=respond, daemon=True)
        self.threads.append(thread)
        thread.start()
        return upstream

    def join(self):
        for thread in self.threads:
            thread.join(timeout=3)


def response(status, body=b'origin response'):
    return (f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n'
            'Content-Type: text/plain\r\nX-Origin-Fixture: preserved\r\n\r\n').encode() + body


class ProxyResponseProvenanceTests(unittest.TestCase):
    def exercise(self, payloads, close_failure=False):
        connector = OriginConnector(payloads)
        replies = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.json'
            request = CaptureRequest(str(uuid4()), 'test', 1, 'http://example.com/private?token=QUERY_SECRET', directory)
            with CaptureTrace(path, request):
                event('navigation')
                proxy = PinnedPublicProxy(StaticResolver(), 1024 * 1024, time.monotonic() + 20,
                                          connector=connector).start()
                try:
                    for _ in payloads:
                        client = http.client.HTTPConnection('127.0.0.1', proxy.server.server_port, timeout=3)
                        try:
                            client.request('GET', 'http://example.com/private?token=QUERY_SECRET', headers={
                                'Proxy-Authorization': proxy.authentication, 'Cookie': 'session=COOKIE_SECRET',
                            })
                            reply = client.getresponse()
                            replies.append({'status': reply.status, 'headers': dict(reply.getheaders()), 'body': reply.read()})
                        finally:
                            client.close()
                    proxy_error = proxy.error
                finally:
                    if close_failure:
                        with patch.object(proxy.thread, 'is_alive', return_value=True):
                            with self.assertRaises(CaptureError) as caught:
                                proxy.close()
                        self.assertEqual(caught.exception.code, 'proxy_stop_unconfirmed')
                    else:
                        proxy.close()
                    connector.join()
            raw_trace = path.read_text(encoding='utf8')
            trace = json.loads(raw_trace)
        self.assertLess(len(raw_trace.encode('utf8')), 128 * 1024,
                        'Evidence must remain readable by the bounded task diagnostics collector')
        self.assertEqual(trace['stage'], 'navigation', 'Proxy evidence must not replace the capture failure stage')
        for secret in ('QUERY_SECRET', 'COOKIE_SECRET', 'EXCEPTION_SECRET', '/private', proxy.password):
            self.assertNotIn(secret, raw_trace)
        for reply in replies:
            self.assertFalse(any(name.lower().startswith('x-capture') for name in reply['headers']))
            self.assertNotIn(b'local_proxy', reply['body'])
            self.assertNotIn(b'connect_failed', reply['body'])
        return replies, trace, proxy_error

    def evidence(self, trace):
        self.assertIn('proxy_http_responses', trace,
                      '502 provenance and the original connection error are currently discarded')
        return trace['proxy_http_responses']

    def test_connect_failure_preserves_original_stage_without_poisoning_later_requests(self):
        replies, trace, error = self.exercise([
            OSError('EXCEPTION_SECRET http://example.com/private?token=QUERY_SECRET'), response(200),
        ])
        self.assertEqual([reply['status'] for reply in replies], [502, 200])
        self.assertIsNone(error, 'An optional resource connection failure must not become a global fatal error')
        evidence = self.evidence(trace)
        self.assertEqual(evidence['total'], 1)
        entry = evidence['items'][0]
        self.assertEqual((entry['origin'], entry['phase'], entry['http_status']), ('local_proxy', 'connect', 502))
        self.assertEqual(entry['host'], 'example.com')
        self.assertEqual(entry['errors'][0]['code'], 'connect_failed')
        self.assertEqual(entry['errors'][1]['type'], 'OSError')

    def test_upstream_502_is_forwarded_unchanged_and_distinguished(self):
        replies, trace, error = self.exercise([response(502, b'actual upstream response')])
        self.assertEqual(replies[0]['status'], 502)
        self.assertEqual(replies[0]['body'], b'actual upstream response')
        self.assertEqual(replies[0]['headers']['X-Origin-Fixture'], 'preserved')
        self.assertIsNone(error)
        evidence = self.evidence(trace)
        self.assertEqual(evidence['total'], 1)
        entry = evidence['items'][0]
        self.assertEqual((entry['origin'], entry['phase'], entry['http_status']), ('upstream', 'response', 502))
        self.assertNotIn('errors', entry)

    def test_malformed_upstream_response_preserves_header_reading_phase(self):
        replies, trace, error = self.exercise([b'EXCEPTION_SECRET invalid HTTP response\r\n\r\n'])
        self.assertEqual(replies[0]['status'], 502)
        entry = self.evidence(trace)['items'][0]
        self.assertEqual((entry['origin'], entry['phase']), ('local_proxy', 'response_headers'))
        self.assertEqual(entry['errors'][0]['type'], 'BadStatusLine')
        self.assertIsNone(error)

    def test_error_only_limit_preserves_latest_errors_and_reports_truncation(self):
        payloads = [response(200)] * 45 + [response(502)] * 44 + [response(503)]
        _, trace, error = self.exercise(payloads)
        evidence = self.evidence(trace)
        self.assertEqual(evidence['total'], 45)
        self.assertEqual(evidence['dropped'], 5)
        self.assertEqual(len(evidence['items']), 40)
        self.assertEqual(evidence['items'][-1]['http_status'], 503)
        self.assertIsNone(error)

    def test_shutdown_error_still_flushes_response_provenance(self):
        _, trace, _ = self.exercise([OSError('EXCEPTION_SECRET')], close_failure=True)
        entry = self.evidence(trace)['items'][0]
        self.assertEqual(entry['errors'][0]['code'], 'connect_failed')

    def test_many_connection_errors_keep_a_bounded_diagnostic_file(self):
        _, trace, error = self.exercise([OSError('EXCEPTION_SECRET') for _ in range(45)])
        evidence = self.evidence(trace)
        self.assertEqual((evidence['total'], evidence['dropped'], len(evidence['items'])), (45, 5, 40))
        self.assertTrue(all(item['errors'][0]['code'] == 'connect_failed' for item in evidence['items']))
        self.assertIsNone(error)


if __name__ == '__main__':
    unittest.main()
