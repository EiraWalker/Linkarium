"""Exercise destination failover through a loopback-only HTTP proxy fixture."""
import ipaddress
import socket
import socketserver
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx

from capture.doh_proxy import PinnedPublicProxy
from capture.models import CaptureError
from capture.proxy_config import HttpProxyConnector


class FixtureResolver:
    def __init__(self, addresses=('1.1.1.1', '8.8.8.8')):
        self.addresses = [ipaddress.ip_address(value) for value in addresses]

    def resolve(self, host, port=443):
        return self.addresses

    def close(self):
        pass


@contextmanager
def upstream_proxy(response_for):
    received = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            header = bytearray()
            while not header.endswith(b'\r\n\r\n') and len(header) < 16384:
                chunk = self.request.recv(1)
                if not chunk:
                    return
                header.extend(chunk)
            line = bytes(header).split(b'\r\n', 1)[0].decode('ascii')
            received.append(line)
            self.request.sendall(response_for(line))

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever,
            kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_address[1]}', received
        finally:
            server.shutdown()
            thread.join(timeout=3)


@contextmanager
def loopback_connections_only():
    original = socket.create_connection
    contacted = []

    def connect(address, *args, **kwargs):
        # Enforce rather than merely observe that failed proxy attempts never
        # fall back to opening the public destination directly.
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError('Unexpected direct destination connection')
        contacted.append(address)
        return original(address, *args, **kwargs)

    with patch('capture.proxy_config.socket.create_connection', side_effect=connect):
        yield contacted


class ProxyTargetFallbackTests(unittest.TestCase):
    def test_rejected_target_socket_is_closed_before_next_attempt(self):
        rejected, accepted = MagicMock(), MagicMock()
        rejected.recv.side_effect = [bytes([value]) for value in b'HTTP/1.1 502 Unavailable\r\n\r\n']
        accepted.recv.side_effect = [bytes([value]) for value in b'HTTP/1.1 200 Connected\r\n\r\n']
        attempts = []

        def connect(address, **kwargs):
            attempts.append(address)
            if len(attempts) == 1:
                return rejected
            rejected.close.assert_called_once()
            return accepted

        with patch('capture.proxy_config.socket.create_connection', side_effect=connect):
            proxy = PinnedPublicProxy(FixtureResolver(), 1024 * 1024,
                time.monotonic() + 10, connector=HttpProxyConnector('http://127.0.0.1:7897')).start()
            try:
                self.assertIs(proxy.connect('https://example.com/'), accepted)
                self.assertEqual(attempts, [('127.0.0.1', 7897)] * 2)
                rejected.close.assert_called_once()
            finally:
                proxy.close()

    def test_target_gateway_errors_try_next_checked_public_ip(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                def response(line):
                    return (f'HTTP/1.1 {status} Destination unavailable\r\n\r\n'.encode()
                        if '1.1.1.1:' in line else b'HTTP/1.1 200 Connected\r\n\r\nX')
                with upstream_proxy(response) as (endpoint, received), loopback_connections_only() as contacted:
                    proxy = PinnedPublicProxy(FixtureResolver(), 1024 * 1024,
                        time.monotonic() + 10, connector=HttpProxyConnector(endpoint)).start()
                    try:
                        stream = proxy.connect('https://example.com/')
                        self.assertEqual(stream.recv(1), b'X')
                        proxy.release(stream)
                        self.assertIsNone(proxy.error)
                        self.assertEqual(received, [
                            'CONNECT 1.1.1.1:443 HTTP/1.1',
                            'CONNECT 8.8.8.8:443 HTTP/1.1',
                        ])
                        self.assertEqual(len(contacted), 2)
                    finally:
                        proxy.close()
                    self.assertFalse(proxy.sockets)

    def assert_optional_request_failure(self, scheme):
        with upstream_proxy(lambda line: b'HTTP/1.1 502 Destination unavailable\r\n\r\n') as (endpoint, received), loopback_connections_only():
            proxy = PinnedPublicProxy(FixtureResolver(), 1024 * 1024,
                time.monotonic() + 15, connector=HttpProxyConnector(endpoint)).start()
            try:
                config = proxy.configuration
                with httpx.Client(proxy=httpx.Proxy(config['server'],
                        auth=(config['username'], config['password'])),
                        timeout=5, trust_env=False) as client:
                    if scheme == 'https':
                        with self.assertRaises(httpx.ProxyError):
                            client.get('https://example.com/optional-image.png')
                    else:
                        self.assertEqual(client.get('http://example.com/optional-image.png').status_code, 502)
                self.assertIsNone(proxy.error)
                port = 443 if scheme == 'https' else 80
                self.assertEqual(received, [
                    f'CONNECT 1.1.1.1:{port} HTTP/1.1',
                    f'CONNECT 8.8.8.8:{port} HTTP/1.1',
                ])
            finally:
                proxy.close()
            self.assertFalse(proxy.sockets)

    def test_all_http_targets_failed_does_not_poison_other_page_requests(self):
        self.assert_optional_request_failure('http')

    def test_all_https_targets_failed_does_not_poison_other_page_requests(self):
        self.assert_optional_request_failure('https')

    def test_proxy_authentication_or_invalid_handshake_does_not_try_other_targets(self):
        for response in (b'HTTP/1.1 407 Authentication required\r\n\r\n', b'INVALID 200\r\n\r\n'):
            with self.subTest(response=response):
                with upstream_proxy(lambda line: response) as (endpoint, received), loopback_connections_only() as contacted:
                    proxy = PinnedPublicProxy(FixtureResolver(), 1024 * 1024,
                        time.monotonic() + 10, connector=HttpProxyConnector(endpoint)).start()
                    try:
                        with self.assertRaises(CaptureError) as caught:
                            proxy.connect('https://example.com/')
                        self.assertEqual(caught.exception.code, 'proxy_unavailable')
                        self.assertEqual(len(received), 1)
                        self.assertEqual(len(contacted), 1)
                    finally:
                        proxy.close()

    def test_mixed_public_private_answers_never_reach_upstream(self):
        with upstream_proxy(lambda line: b'HTTP/1.1 200 Connected\r\n\r\n') as (endpoint, received), loopback_connections_only() as contacted:
            proxy = PinnedPublicProxy(FixtureResolver(('1.1.1.1', '127.0.0.1')), 1024 * 1024,
                time.monotonic() + 10, connector=HttpProxyConnector(endpoint)).start()
            try:
                with self.assertRaises(CaptureError) as caught:
                    proxy.connect('https://example.com/')
                self.assertEqual(caught.exception.code, 'unsafe_url')
                self.assertFalse(received)
                self.assertFalse(contacted)
            finally:
                proxy.close()


if __name__ == '__main__':
    unittest.main()
