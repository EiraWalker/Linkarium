import ipaddress
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from capture.models import CaptureError
from capture.proxy_config import HttpProxyConnector, detect_clash, load_config, save_config, validate_proxy


class ProxyConfigTests(unittest.TestCase):
    def test_only_explicit_loopback_http_endpoints_are_accepted(self):
        self.assertEqual(validate_proxy('http://127.0.0.1:7897/'), 'http://127.0.0.1:7897')
        for value in ('http://example.com:7897', 'http://127.0.0.1:0', 'socks5://127.0.0.1:7898',
                      'http://user:secret@127.0.0.1:7897', 'http://127.0.0.1:7897/path', None):
            with self.subTest(value=value), self.assertRaises(CaptureError):
                validate_proxy(value)

    def test_clash_ports_only_and_profile_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            folder = base / 'io.github.clash-verge-rev.clash-verge-rev'
            folder.mkdir()
            (folder / 'clash-verge.yaml').write_text('secret: do-not-read\nmixed-port: 7897\nport: 7899\nproxies:\n  - port: 6666\n')
            self.assertEqual(detect_clash(base), 'http://127.0.0.1:7897')
            self.assertEqual(load_config(base)['mode'], 'direct')
            save_config(base, {'mode': 'manual', 'proxy_url': 'http://127.0.0.1:7899'})
            self.assertEqual(load_config(base)['proxy_url'], 'http://127.0.0.1:7899')
            save_config(base, {'mode': 'direct', 'proxy_url': 'ignored'})
            self.assertEqual(load_config(base), {'mode': 'direct', 'proxy_url': ''})

    def test_connector_never_delegates_hostname_or_private_ip(self):
        connector = HttpProxyConnector('http://127.0.0.1:7897')
        with patch('capture.proxy_config.socket.create_connection') as connect:
            for address in (('127.0.0.1', 80), ('198.18.0.1', 443), ('169.254.169.254', 80), ('1.1.1.1', 22)):
                with self.subTest(address=address), self.assertRaises(CaptureError):
                    connector(address, 1)
            connect.assert_not_called()

    def test_http_connect_pins_literal_and_preserves_first_payload_byte(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        received = []
        def serve():
            connection, _ = server.accept()
            with connection:
                data = b''
                while not data.endswith(b'\r\n\r\n'):
                    data += connection.recv(1)
                received.append(data)
                connection.sendall(b'HTTP/1.1 200 Connection established\r\n\r\nX')
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            connector = HttpProxyConnector(f'http://127.0.0.1:{server.getsockname()[1]}')
            with connector(('1.1.1.1', 443), 2) as stream:
                self.assertEqual(stream.recv(1), b'X')
            self.assertTrue(received[0].startswith(b'CONNECT 1.1.1.1:443 HTTP/1.1'))
        finally:
            server.close()
            thread.join(timeout=3)

    def test_closed_proxy_has_explicit_failure_without_direct_fallback(self):
        with patch('capture.proxy_config.socket.create_connection', side_effect=OSError('secret')) as connect:
            with self.assertRaises(CaptureError) as caught:
                HttpProxyConnector('http://127.0.0.1:7897')(('1.1.1.1', 443), 1)
            self.assertEqual(caught.exception.code, 'proxy_unavailable')
            self.assertNotIn('secret', str(caught.exception))
            connect.assert_called_once()
