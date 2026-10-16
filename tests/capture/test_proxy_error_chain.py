"""Proxy failures retain their actionable code through DNS, HTTP and Chromium."""
import os
import socket
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx

from capture.development import run_development_capture
from capture.doh_proxy import PinnedPublicProxy, PublicDohResolver
from capture.models import CaptureError, CaptureRequest
from capture.proxy_config import HttpProxyConnector


@contextmanager
def unavailable_proxy():
    # Reserve the port without listening, so another process cannot claim it.
    # Every connection in these tests remains on loopback; no live proxy is used.
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        yield f'http://127.0.0.1:{reserved.getsockname()[1]}'


class LiteralOnlyResolver:
    def resolve(self, host, port=443):
        raise AssertionError('Literal destination must not perform a DNS query')

    def close(self):
        pass


class ProxyErrorChainTests(unittest.TestCase):
    def test_closed_upstream_during_dns_reports_proxy_unavailable(self):
        with unavailable_proxy() as endpoint:
            resolver = PublicDohResolver(proxy_url=endpoint)
            try:
                with self.assertRaises(CaptureError) as caught:
                    resolver.resolve('example.com')
                self.assertEqual(caught.exception.code, 'proxy_unavailable')
            finally:
                resolver.close()

    def test_dns_service_failure_is_not_misreported_as_proxy_failure(self):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(503, request=request)), trust_env=False)
        resolver = PublicDohResolver(client=client, proxy_url='http://127.0.0.1:7897')
        try:
            with self.assertRaises(CaptureError) as caught:
                resolver.resolve('example.com')
            self.assertEqual(caught.exception.code, 'dns_failed')
        finally:
            resolver.close()

    def assert_proxy_forward_error(self, scheme):
        with unavailable_proxy() as endpoint:
            proxy = PinnedPublicProxy(LiteralOnlyResolver(), 1024 * 1024,
                time.monotonic() + 15, connector=HttpProxyConnector(endpoint)).start()
            try:
                config = proxy.configuration
                with httpx.Client(proxy=httpx.Proxy(config['server'],
                        auth=(config['username'], config['password'])),
                        trust_env=False, timeout=10) as client:
                    if scheme == 'https':
                        with self.assertRaises(httpx.ProxyError):
                            client.get('https://1.1.1.1/')
                    else:
                        response = client.get('http://1.1.1.1/')
                        self.assertEqual(response.status_code, 502)
                self.assertIsInstance(proxy.error, CaptureError)
                self.assertEqual(proxy.error.code, 'proxy_unavailable')
            finally:
                proxy.close()
            self.assertFalse(proxy.thread.is_alive())
            self.assertFalse(proxy.sockets)

    def test_http_forwarding_preserves_upstream_proxy_error(self):
        self.assert_proxy_forward_error('http')

    def test_connect_tunnel_preserves_upstream_proxy_error(self):
        self.assert_proxy_forward_error('https')


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1',
                     'Requires installed Chromium')
class ProxyBrowserErrorChainTests(unittest.TestCase):
    def assert_cli_proxy_error(self, scheme):
        with unavailable_proxy() as endpoint, tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(attempt_uuid=str(uuid4()), generation='test',
                library_epoch=1, url=f'{scheme}://1.1.1.1/', output_dir=directory,
                development=True, proxy_url=endpoint, timeout_seconds=20)
            with self.assertRaises(CaptureError) as caught:
                run_development_capture(request, lambda: True)
            self.assertEqual(caught.exception.code, 'proxy_unavailable')
            self.assertFalse(list(Path(directory).glob('*.html')))

    def test_http_navigation_returns_proxy_error_through_child_protocol(self):
        self.assert_cli_proxy_error('http')

    def test_https_navigation_returns_proxy_error_through_child_protocol(self):
        self.assert_cli_proxy_error('https')


if __name__ == '__main__':
    unittest.main()
