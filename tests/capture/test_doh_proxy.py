import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx

from capture import CaptureError, CaptureRequest
from capture.doh_proxy import PinnedPublicProxy, PublicDohResolver, BoundedHeaderReader
from capture.network import PublicURLPolicy, SystemResolver


class StaticResolver:
    def __init__(self, addresses=('93.184.216.34',)):
        self.addresses = [ipaddress.ip_address(address) for address in addresses]
        self.calls = []
    def resolve(self, host, port):
        self.calls.append((host,port))
        return self.addresses
    def close(self):
        pass


class DoHResolverTests(unittest.TestCase):
    def resolver(self, answers, status=0, question='example.com.'):
        requested = []
        def response(request):
            requested.append(request)
            return httpx.Response(200,json={'Status':status,'Question':[{'name':question,'type':1}],
                'Answer':[{'name':'example.com.','type':1,'TTL':10,'data':address} for address in answers]})
        client = httpx.Client(transport=httpx.MockTransport(response),trust_env=False)
        return PublicDohResolver(client=client), requested

    def test_fixed_https_literal_endpoint_and_public_answers(self):
        resolver, requests = self.resolver(['93.184.216.34'])
        try:
            self.assertEqual([str(address) for address in resolver.resolve('example.com')],['93.184.216.34'])
            self.assertEqual(requests[0].url.host,'1.1.1.1')
            self.assertEqual(requests[0].url.scheme,'https')
            self.assertEqual(requests[0].headers['accept'],'application/dns-json')
            self.assertEqual(requests[0].url.params['name'],'example.com')
        finally:
            resolver.close()

    def test_mixed_private_answer_is_rejected_without_fallback(self):
        for addresses in (['93.184.216.34','127.0.0.1'],['198.18.0.1'],['169.254.169.254']):
            resolver,_ = self.resolver(addresses)
            try:
                with self.subTest(addresses=addresses),self.assertRaises(CaptureError) as caught:
                    resolver.resolve('example.com')
                self.assertEqual(caught.exception.code,'unsafe_url')
            finally:
                resolver.close()

    def test_wrong_dns_question_is_rejected(self):
        resolver,_ = self.resolver(['93.184.216.34'],question='other.example.')
        try:
            with self.assertRaises(CaptureError):
                resolver.resolve('example.com')
        finally:
            resolver.close()

    def test_excessive_dns_answers_are_rejected(self):
        resolver,_ = self.resolver(['93.184.216.34']*33)
        try:
            with self.assertRaises(CaptureError) as caught:
                resolver.resolve('example.com')
            self.assertEqual(caught.exception.code,'dns_budget')
        finally:
            resolver.close()


class PinnedProxyTests(unittest.TestCase):
    def test_system_dns_rebind_cannot_change_the_actual_connection(self):
        stream = MagicMock()
        connector = MagicMock(return_value=stream)
        resolver = SystemResolver(time.monotonic() + 10)
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
        proxy = PinnedPublicProxy(resolver, 1024 * 1024, time.monotonic() + 10, connector=connector).start()
        try:
            with patch('capture.network.socket.getaddrinfo', side_effect=[public, private]) as lookup:
                proxy.policy.validate('http://rebind.example/')
                proxy.connect('http://rebind.example/')
                proxy.connect('http://rebind.example/image')
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual([call.args[0] for call in connector.call_args_list],
                             [('93.184.216.34', 80), ('93.184.216.34', 80)])
        finally:
            proxy.close()

    def test_system_dns_mixed_private_answers_never_connect(self):
        connector = MagicMock()
        resolver = SystemResolver(time.monotonic() + 10)
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))
                   for address in ('93.184.216.34', '127.0.0.1')]
        proxy = PinnedPublicProxy(resolver, 1024 * 1024, time.monotonic() + 10, connector=connector).start()
        try:
            with patch('capture.network.socket.getaddrinfo', return_value=answers), self.assertRaises(CaptureError):
                proxy.connect('https://rebind.example/')
            connector.assert_not_called()
        finally:
            proxy.close()

    def test_development_system_dns_capture_uses_pinned_proxy(self):
        from capture.runner import capture
        with tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1, 'https://example.com/', directory, development=True)
            def inspect_proxy(current, **arguments):
                self.assertIsInstance(arguments['proxy'].resolver, SystemResolver)
                self.assertTrue(arguments['proxy'].thread.is_alive())
                return 'rendered'
            with patch('capture.runner._capture', side_effect=inspect_proxy), patch.dict(os.environ, {'BOOKMARK_PUBLIC_DEPLOYMENT': '0'}):
                self.assertEqual(capture(request), 'rendered')

    def test_cancelled_connector_does_not_try_remaining_public_addresses(self):
        calls = []
        def connector(address,timeout):
            calls.append(address)
            proxy.stop.set()
            raise OSError('cancelled first TCP attempt')
        proxy = PinnedPublicProxy(StaticResolver(('93.184.216.34','1.1.1.1','9.9.9.9')),
                                  1024,time.monotonic()+10,connector=connector).start()
        try:
            with self.assertRaises(CaptureError) as caught:
                proxy.connect('https://example.com/')
            self.assertEqual(caught.exception.code,'timeout')
            self.assertEqual(calls,[('93.184.216.34',443)])
        finally:
            proxy.close()

    def test_http_response_headers_are_charged_before_forwarding(self):
        upstream, remote = socket.socketpair()
        def origin():
            try:
                request = b''
                while b'\r\n\r\n' not in request:
                    request += remote.recv(4096)
                remote.sendall(b'HTTP/1.1 200 OK\r\nX-Large: '+b'x'*60000+b'\r\nContent-Length: 1\r\n\r\nx')
            finally:
                remote.close()
        thread = threading.Thread(target=origin,daemon=True)
        thread.start()
        proxy = PinnedPublicProxy(StaticResolver(),1024,time.monotonic()+10,
                                  connector=lambda *args,**kwargs:upstream).start()
        try:
            client = http.client.HTTPConnection('127.0.0.1',proxy.server.server_port,timeout=3)
            client.request('GET','http://example.com/',headers={'Proxy-Authorization':proxy.authentication})
            response = client.getresponse()
            self.assertEqual(response.status,502)
            response.read()
            client.close()
            self.assertEqual(proxy.error.code,'byte_budget')
        finally:
            proxy.close()
            thread.join(timeout=2)

    def test_request_header_reader_rejects_before_unbounded_accumulation(self):
        import io
        reader = BoundedHeaderReader(io.BytesIO(b'X-Large: '+b'x'*20000+b'\r\n'),None,16384)
        with self.assertRaises(CaptureError) as caught:
            reader.readline(65537)
        self.assertEqual(caught.exception.code,'request_budget')
        self.assertEqual(reader.stream.tell(),16385)

    def test_deployment_deny_hosts_include_configured_subdomains_and_idna(self):
        resolver = StaticResolver()
        policy = PublicURLPolicy(deny_hosts=('.Example.COM.', '例子.测试', '[2606:4700:4700::1111]'),resolver=resolver)
        for url in ('https://example.com/', 'https://child.example.com/', 'https://例子.测试/',
                    'https://[2606:4700:4700::1111]/'):
            with self.subTest(url=url), self.assertRaises(CaptureError):
                policy.validate(url)
        self.assertEqual(policy.validate('https://example.com.evil.test/'), 'https://example.com.evil.test/')
        self.assertEqual(policy.validate('https://notexample.com/'), 'https://notexample.com/')

    def test_tcp_connector_receives_checked_literal_ip_and_never_hostname(self):
        resolver = StaticResolver()
        stream = MagicMock()
        connect = MagicMock(return_value=stream)
        proxy = PinnedPublicProxy(resolver,1024*1024,time.monotonic()+10,connector=connect).start()
        try:
            self.assertIs(proxy.connect('https://example.com/path'),stream)
            self.assertEqual(connect.call_args.args[0],('93.184.216.34',443))
            self.assertEqual(resolver.calls,[('example.com',443)])
            self.assertEqual(proxy.configuration['bypass'],'<-loopback>')
            self.assertEqual(proxy.server.server_address[0],'127.0.0.1')
        finally:
            proxy.close()
        self.assertFalse(proxy.thread.is_alive())

    def test_private_ports_and_credentials_never_reach_connector(self):
        connector = MagicMock()
        resolver = StaticResolver()
        proxy = PinnedPublicProxy(resolver,1024*1024,time.monotonic()+10,connector=connector).start()
        try:
            for url in ('https://127.0.0.1/','https://[::1]/','http://169.254.169.254/',
                        'https://example.com:8443/','https://user:password@example.com/'):
                with self.subTest(url=url),self.assertRaises(CaptureError):
                    proxy.connect(url)
            connector.assert_not_called()
        finally:
            proxy.close()

    def test_proxy_authentication_precedes_public_connect(self):
        connector = MagicMock()
        proxy = PinnedPublicProxy(StaticResolver(),1024*1024,time.monotonic()+10,connector=connector).start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1',proxy.server.server_port,timeout=3)
            connection.request('GET','http://example.com/')
            response = connection.getresponse()
            self.assertEqual(response.status,407)
            response.read()
            connection.close()
            connector.assert_not_called()
        finally:
            proxy.close()
        self.assertFalse(proxy.thread.is_alive())

    def test_combined_bidirectional_budget_fails_closed(self):
        proxy = PinnedPublicProxy(StaticResolver(),1024,time.monotonic()+10).start()
        try:
            proxy.budget(600)
            with self.assertRaises(CaptureError) as caught:
                proxy.budget(500)
            self.assertEqual(caught.exception.code,'byte_budget')
        finally:
            proxy.close()

    def test_cancel_closes_active_tunnel_and_confirms_handler_exit(self):
        upstream, remote = socket.socketpair()
        proxy = PinnedPublicProxy(StaticResolver(),1024*1024,time.monotonic()+20,
            connector=lambda *args,**kwargs:upstream).start()
        client = socket.create_connection(('127.0.0.1',proxy.server.server_port),timeout=3)
        try:
            client.sendall(('CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n'
                            'Proxy-Authorization: '+proxy.authentication+'\r\n\r\n').encode())
            response = client.recv(4096)
            self.assertIn(b'200 Connection established',response)
            client.sendall(b'tunnel bytes')
            self.assertEqual(remote.recv(4096),b'tunnel bytes')
            start = time.monotonic()
            proxy.close()
            self.assertLess(time.monotonic()-start,3)
            self.assertFalse(proxy.thread.is_alive())
            self.assertEqual(proxy.server.threads,set())
            self.assertEqual(proxy.sockets,set())
            self.assertEqual(client.recv(1),b'')
        finally:
            client.close()
            remote.close()
            if proxy.thread.is_alive():
                proxy.close()

    @unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1','Opt-in real Chromium integration')
    def test_real_system_dns_capture_keeps_public_resolution_pinned(self):
        from capture.runner import capture
        root = Path(__file__).resolve().parents[2]
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(root / 'tools' / 'browsers'))
        hosts, connections = [], []
        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                hosts.append(self.headers.get('Host'))
                content = b'<!doctype html><html><head><title>Pinned system DNS</title></head><body><h1>Public origin</h1><img src="/image.svg"></body></html>'
                mime = 'text/html'
                if self.path == '/image.svg':
                    content = b'<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30"><rect width="30" height="30" fill="blue"/></svg>'
                    mime = 'image/svg+xml'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *_arguments):
                pass
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        thread = threading.Thread(target=origin.serve_forever, daemon=True)
        thread.start()
        real_lookup, real_connect = socket.getaddrinfo, socket.create_connection
        resolutions = []
        def lookup(host, port, *arguments, **keywords):
            if host != 'rebind.example.test':
                return real_lookup(host, port, *arguments, **keywords)
            resolutions.append(host)
            address = '93.184.216.34' if len(resolutions) == 1 else '127.0.0.1'
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]
        def connect(address, timeout):
            connections.append(address)
            self.assertEqual(address, ('93.184.216.34', 80))
            return real_connect(('127.0.0.1', origin.server_port), timeout=timeout)
        try:
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(str(uuid4()), 'test', 1, 'http://rebind.example.test/', directory,
                                         development=True, dns_mode='system')
                with patch('capture.network.socket.getaddrinfo', side_effect=lookup), \
                        patch('capture.doh_proxy.socket.create_connection', side_effect=connect):
                    result = capture(request)
                self.assertIn('Public origin', Path(result.artifact_path).read_text(encoding='utf-8'))
                self.assertIn('development-system-dns-pinned-ip-proxy', result.warnings)
            self.assertEqual(resolutions, ['rebind.example.test'])
            self.assertGreaterEqual(len(connections), 2)
            self.assertTrue(all(host == 'rebind.example.test' for host in hosts))
        finally:
            origin.shutdown()
            origin.server_close()
            thread.join(timeout=2)

    @unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1','Opt-in real Chromium integration')
    def test_browser_private_redirect_cannot_bypass_proxy(self):
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
        root = Path(__file__).resolve().parents[2]
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(root/'tools'/'browsers'))
        private_hits, origin_hosts, connector_addresses = [], [], []
        class PrivateHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                private_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'PRIVATE DATA')
            def log_message(self,*args):
                pass
        private = ThreadingHTTPServer(('127.0.0.1',0),PrivateHandler)
        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                origin_hosts.append((self.headers.get('Host'),self.headers.get('Proxy-Authorization')))
                self.send_response(302)
                self.send_header('Location',f'http://127.0.0.1:{private.server_port}/private')
                self.send_header('Content-Length','0')
                self.end_headers()
            def log_message(self,*args):
                pass
        origin = ThreadingHTTPServer(('127.0.0.1',0),OriginHandler)
        threads = [threading.Thread(target=server.serve_forever,daemon=True) for server in (private,origin)]
        for thread in threads:
            thread.start()
        def fixture_connector(address,timeout):
            # Test transport substitution, never a production/public request option.
            connector_addresses.append(address)
            self.assertEqual(address,('93.184.216.34',80))
            return socket.create_connection(('127.0.0.1',origin.server_port),timeout=timeout)
        proxy = PinnedPublicProxy(StaticResolver(),1024*1024,time.monotonic()+30,
                                  connector=fixture_connector).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(chromium_sandbox=True,proxy=proxy.configuration,
                    args=['--proxy-bypass-list=<-loopback>','--disable-background-networking','--disable-quic'])
                context = browser.new_context(service_workers='block')
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(
                    ('http://public.example.test/','http://127.0.0.1:')) else route.abort())
                page = context.new_page()
                try:
                    page.goto('http://public.example.test/',wait_until='domcontentloaded',timeout=10000)
                except PlaywrightError:
                    pass
                page.wait_for_timeout(200)
                browser.close()
            self.assertTrue(connector_addresses)
            self.assertTrue(origin_hosts)
            self.assertTrue(all(host == 'public.example.test' and secret is None for host,secret in origin_hosts))
            self.assertEqual(private_hits,[])
        finally:
            proxy.close()
            for server in (origin,private):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_doh_is_not_available_to_production_or_fixture_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            for options in ({'development':False},{'development':True,'allow_test_hosts':('127.0.0.1',)}):
                with self.subTest(options=options),self.assertRaises(CaptureError):
                    CaptureRequest(str(uuid4()),'test',1,'https://example.com/',directory,
                        dns_mode='public-doh',**options).validate()
