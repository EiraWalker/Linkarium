"""Per-capture public DoH + pinned-IP proxy. HTTPS TLS remains end-to-end in Chromium.

Only explicit development uses this compatibility path. It does not replace production
network isolation, change system DNS/proxy settings, or accept configurable proxy/resolver URLs.
"""
import base64
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.client
import ipaddress
import json
import secrets
import select
import socket
from socketserver import ThreadingMixIn
import threading
import time
from urllib.parse import urlsplit

import httpx

from .diagnostics import bind_proxy_http_responses, error_evidence, host_only
from .models import CaptureError
from .network import PublicURLPolicy


class PublicDohResolver:
    ENDPOINT = 'https://1.1.1.1/dns-query'

    def __init__(self, client=None, deadline=None, proxy_url=None):
        self.proxy_url = proxy_url
        self.client = client or httpx.Client(trust_env=False, follow_redirects=False,
            proxy=proxy_url, timeout=httpx.Timeout(5.0), limits=httpx.Limits(max_connections=4))
        self.deadline = deadline or time.monotonic() + 120
        self.cache = {}
        self.lock = threading.RLock()
        self.queries = 0
        self.stop = threading.Event()

    def resolve(self, host, port=443):
        host = host.encode('idna').decode('ascii').lower().rstrip('.')
        with self.lock:
            if self.stop.is_set():
                raise CaptureError('timeout', 'Capture DNS resolver has stopped')
            cached = self.cache.get(host)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])
            if time.monotonic() >= self.deadline:
                raise CaptureError('timeout', 'Capture DNS time budget exhausted')
            self.queries += 1
            if self.queries > 128:
                raise CaptureError('dns_budget', 'Capture DNS query budget exhausted')
            try:
                with self.client.stream('GET', self.ENDPOINT, params={'name':host,'type':'A'},
                    headers={'Accept':'application/dns-json'}, timeout=min(5, max(0.1, self.deadline-time.monotonic()))) as response:
                    if response.status_code != 200:
                        raise CaptureError('dns_failed', 'Public DoH returned a non-success response')
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 65536:
                            raise CaptureError('dns_budget', 'Public DoH response exceeds size budget')
                        chunks.append(chunk)
                    data = json.loads(b''.join(chunks))
                question = data.get('Question', [{}])[0]
                if data.get('Status') != 0 or str(question.get('name','')).lower().rstrip('.') != host or question.get('type') != 1:
                    raise CaptureError('dns_failed', 'Public DoH question or status is invalid')
                answers = [answer for answer in data.get('Answer', []) if answer.get('type') == 1]
                if not answers:
                    raise CaptureError('dns_failed', 'Target has no public IPv4 DNS answer')
                if len(answers) > 32:
                    raise CaptureError('dns_budget', 'Public DoH answer count exceeds budget')
                addresses = [ipaddress.ip_address(answer['data']) for answer in answers]
                if any(address.version != 4 or not address.is_global or address.is_multicast for address in addresses):
                    raise CaptureError('unsafe_url', 'Public DoH returned a nonpublic or mixed DNS answer')
                ttl = max(0, min(30, min(int(answer.get('TTL', 0)) for answer in answers)))
                addresses = list(dict.fromkeys(addresses))
                self.cache[host] = (time.monotonic() + ttl, addresses)
                return list(addresses)
            except CaptureError:
                raise
            except httpx.ProxyError as exc:
                raise CaptureError('proxy_unavailable', 'Local proxy rejected DNS tunnel') from exc
            except httpx.ConnectError as exc:
                if self.proxy_url:
                    raise CaptureError('proxy_unavailable', 'Local proxy DNS connection failed') from exc
                raise CaptureError('dns_failed', 'Public DoH resolution failed') from exc
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                raise CaptureError('dns_failed', 'Public DoH resolution failed') from exc

    def close(self):
        self.stop.set()
        self.deadline = min(self.deadline,time.monotonic())
        self.client.close()


class ProxyServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    # The accept loop waits on one socket; the OS backlog bounds the remainder.
    # A connection burst must not become an unbounded collection of threads.
    request_queue_size = 32

    def __init__(self, owner):
        self.owner = owner
        self.threads = set()
        self.thread_lock = threading.Lock()
        self.handler_slots = threading.BoundedSemaphore(32)
        self.slot_wait_seconds = 0.0
        self.slot_wait_count = 0
        self.peak_active_handlers = 0
        super().__init__(('127.0.0.1', 0), ProxyHandler)

    def process_request(self, request, client_address):
        waited = False
        while True:
            if self.owner.stop.is_set() or self.owner.error is not None:
                self.shutdown_request(request)
                return
            remaining = self.owner.deadline - time.monotonic()
            if remaining <= 0:
                if self.owner.error is None:
                    self.owner.error = CaptureError('timeout', 'Capture proxy stopped or timed out')
                self.shutdown_request(request)
                return
            started = time.monotonic()
            acquired = self.handler_slots.acquire(timeout=min(0.1, remaining))
            waited_seconds = time.monotonic() - started
            if waited_seconds >= 0.001:
                with self.thread_lock:
                    self.slot_wait_seconds += waited_seconds
                    if not waited:
                        self.slot_wait_count += 1
                        waited = True
            if acquired:
                break
        if self.owner.stop.is_set():
            self.handler_slots.release()
            self.shutdown_request(request)
            return
        handler = threading.Thread(target=self.process_request_thread,args=(request,client_address),daemon=True)
        with self.thread_lock:
            self.threads.add(handler)
            self.peak_active_handlers = max(self.peak_active_handlers, len(self.threads))
        try:
            handler.start()
        except Exception:
            with self.thread_lock:
                self.threads.discard(handler)
            self.handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        current = threading.current_thread()
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.thread_lock:
                self.threads.discard(current)
            self.handler_slots.release()

    def handle_error(self, request, client_address):
        if not self.owner.stop.is_set():
            super().handle_error(request,client_address)


class BoundedHeaderReader:
    """Bound status/header lines before either HTTP parser accumulates them."""
    def __init__(self, stream, owner, maximum, count_network_bytes=False):
        self.stream, self.owner, self.maximum = stream, owner, maximum
        self.count_network_bytes = count_network_bytes
        self.size = 0

    def readline(self, size=-1):
        available = self.maximum-self.size+1
        line = self.stream.readline(available if size < 0 else min(size,available))
        self.size += len(line)
        if self.count_network_bytes:
            self.owner.budget(len(line))
        if self.size > self.maximum:
            raise CaptureError('byte_budget' if self.count_network_bytes else 'request_budget',
                               'Capture HTTP headers exceed byte budget')
        return line

    def __getattr__(self, name):
        return getattr(self.stream,name)


class PinnedPublicProxy:
    def __init__(self, resolver, max_bytes, deadline, deny_hosts=(), connector=None):
        self.resolver = resolver
        self.policy = PublicURLPolicy(deny_hosts=deny_hosts, resolver=resolver)
        self.max_bytes = max_bytes
        self.deadline = deadline
        self.connector = connector or socket.create_connection
        self.username = 'capture'
        self.password = secrets.token_urlsafe(32)
        self.authentication = 'Basic ' + base64.b64encode((self.username + ':' + self.password).encode()).decode()
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.sockets = set()
        self.error = None
        self.bytes = 0
        self.connections = 0
        self.http_response_errors = []
        self.http_response_error_count = 0
        self.record_http_diagnostics = bind_proxy_http_responses()
        self.server = ProxyServer(self)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':0.1}, daemon=True)

    def start(self):
        self.thread.start()
        return self

    @property
    def configuration(self):
        return {'server':f'http://127.0.0.1:{self.server.server_port}',
                'username':self.username,'password':self.password,'bypass':'<-loopback>'}

    def budget(self, count=0):
        with self.lock:
            if self.stop.is_set() or time.monotonic() >= self.deadline:
                raise CaptureError('timeout', 'Capture proxy stopped or timed out')
            self.bytes += count
            if self.bytes > self.max_bytes:
                self.error = CaptureError('byte_budget', 'Capture network byte budget exceeded')
                raise self.error

    def connect(self, url):
        addresses = self.policy.resolve(url)
        target = urlsplit(url)
        port = target.port or (443 if target.scheme == 'https' else 80)
        if any(address.version != 4 for address in addresses):
            raise CaptureError('ipv6_unsupported', 'Public DoH capture currently supports IPv4 targets only')
        self.budget()
        # Only validated literal addresses reach connect(). No hostname is delegated to
        # an OS resolver, and no DIRECT/system-DNS fallback is attempted.
        last_error = None
        for address in addresses:
            self.budget()
            with self.lock:
                self.connections += 1
                if self.connections > 2048:
                    raise CaptureError('request_budget', 'Capture proxy connection budget exceeded')
            try:
                stream = self.connector((str(address), port), timeout=min(8, max(0.1, self.deadline-time.monotonic())))
                stream.settimeout(10)
                self.track(stream)
                return stream
            except OSError as exc:
                last_error = exc
                self.budget()
        raise CaptureError('connect_failed', 'Validated public TCP endpoint could not be reached') from last_error

    def track(self, stream):
        with self.lock:
            if self.stop.is_set():
                stream.close()
                raise CaptureError('timeout', 'Capture proxy has stopped')
            self.sockets.add(stream)

    def release(self, stream):
        with self.lock:
            self.sockets.discard(stream)
        try:
            stream.close()
        except OSError:
            pass

    def record_http_response(self, url, status, origin, phase, *, error=None,
                             response_sent=True, upstream_status=None):
        if status < 400 and error is None:
            return
        # Never expose these internal facts through HTTP response headers/body.
        entry = {'host': host_only(url), 'origin': origin, 'phase': phase,
                 'http_status': status, 'response_sent': response_sent}
        if upstream_status is not None:
            entry['upstream_http_status'] = upstream_status
        if error is not None:
            entry['errors'] = error_evidence(error)
        with self.lock:
            self.http_response_error_count += 1
            self.http_response_errors.append(entry)
            self.http_response_errors = self.http_response_errors[-40:]

    def close(self):
        try:
            self.stop.set()
            self.resolver.close()
            with self.lock:
                streams = list(self.sockets)
            for stream in streams:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.release(stream)
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            with self.server.thread_lock:
                handlers = list(self.server.threads)
            wait_deadline = time.monotonic()+8
            for handler in handlers:
                handler.join(timeout=max(0,wait_deadline-time.monotonic()))
            if self.thread.is_alive() or any(handler.is_alive() for handler in handlers):
                raise CaptureError('proxy_stop_unconfirmed', 'Capture proxy threads did not exit within their shutdown budget')
        finally:
            # Only the closing thread writes the trace. Preserve evidence even if
            # shutdown fails, without a late diagnostic event replacing its stage.
            with self.lock:
                items = list(self.http_response_errors)
                summary = {'total': self.http_response_error_count,
                           'dropped': self.http_response_error_count-len(items), 'items': items}
            self.record_http_diagnostics(summary)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self.server.owner.track(self.connection)
        self.rfile = BoundedHeaderReader(self.rfile,self.server.owner,16384)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except CaptureError as exc:
            self.server.owner.error = exc
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except OSError:
            pass
        finally:
            self.server.owner.release(self.connection)

    def log_message(self, *args):
        pass

    def authorized(self):
        if not hmac.compare_digest(self.headers.get('Proxy-Authorization', ''), self.server.owner.authentication):
            self.send_response_only(407)
            self.send_header('Proxy-Authenticate','Basic realm="bookmark-capture"')
            self.send_header('Content-Length','0')
            self.send_header('Connection','close')
            self.end_headers()
            self.close_connection = True
            return False
        if sum(len(name)+len(value)+4 for name,value in self.headers.items()) > 16384:
            raise CaptureError('request_budget', 'Capture proxy request headers exceed budget')
        self.server.owner.budget(len(self.requestline) + sum(len(name)+len(value)+4 for name,value in self.headers.items()))
        return True

    def do_CONNECT(self):
        upstream = None
        try:
            if not self.authorized():
                return
            # Parsing through the shared URL policy rejects credentials, invalid/alternate
            # IP spellings, restricted ports, localhost/private/mixed DNS and denied hosts.
            upstream = self.server.owner.connect('https://' + self.path + '/')
            self.send_response_only(200, 'Connection established')
            self.end_headers()
            self.wfile.flush()
            last_activity = time.monotonic()
            while not self.server.owner.stop.is_set():
                self.server.owner.budget()
                if time.monotonic()-last_activity > 15:
                    break
                ready, _, _ = select.select([self.connection, upstream], [], [], 0.5)
                for stream in ready:
                    data = stream.recv(65536)
                    if not data:
                        return
                    self.server.owner.budget(len(data))
                    (upstream if stream is self.connection else self.connection).sendall(data)
                    last_activity = time.monotonic()
        except CaptureError as exc:
            if exc.code in ('byte_budget','timeout','request_budget','proxy_unavailable'):
                self.server.owner.error = exc
            try:
                self.send_error(502, 'Public capture tunnel refused')
            except OSError:
                pass
        except (OSError, ValueError):
            pass
        finally:
            self.close_connection = True
            if upstream:
                self.server.owner.release(upstream)

    def forward(self):
        upstream = None
        phase = 'request_validation'
        upstream_status = None
        try:
            if not self.authorized():
                return
            target = urlsplit(self.path)
            if target.scheme != 'http' or self.headers.get('Upgrade') or self.headers.get('Transfer-Encoding'):
                raise CaptureError('unsafe_url', 'Only bounded ordinary HTTP requests may be forwarded')
            length = int(self.headers.get('Content-Length','0'))
            if not 0 <= length <= 1024*1024:
                raise CaptureError('request_budget', 'Capture request body exceeds budget')
            body = self.rfile.read(length) if length else None
            self.server.owner.budget(length)
            phase = 'connect'
            upstream = self.server.owner.connect(self.path)
            connection = http.client.HTTPConnection(str(target.hostname), target.port or 80, timeout=10)
            connection.sock = upstream
            def response_factory(*args,**kwargs):
                response = http.client.HTTPResponse(*args,**kwargs)
                response.fp = BoundedHeaderReader(response.fp,self.server.owner,65536,True)
                return response
            connection.response_class = response_factory
            headers = {name:value for name,value in self.headers.items() if name.lower() not in
                ('proxy-authorization','proxy-connection','connection','host')}
            headers['Host'] = target.netloc
            headers['Connection'] = 'close'
            path = target.path or '/'
            if target.query:
                path += '?' + target.query
            phase = 'request_send'
            connection.request(self.command, path, body=body, headers=headers)
            phase = 'response_headers'
            response = connection.getresponse()
            upstream_status = response.status
            phase = 'response_body'
            declared = int(response.getheader('Content-Length','0'))
            if declared > 16*1024*1024:
                raise CaptureError('byte_budget', 'Capture response exceeds byte budget')
            chunks, size = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                self.server.owner.budget(len(chunk))
                if size > 16*1024*1024:
                    raise CaptureError('byte_budget', 'Capture response exceeds byte budget')
                chunks.append(chunk)
            phase = 'response_forward'
            self.send_response_only(response.status)
            for name,value in response.getheaders():
                if name.lower() not in ('content-length','transfer-encoding','connection','keep-alive','proxy-authenticate'):
                    self.send_header(name,value)
            self.send_header('Content-Length',str(size))
            self.send_header('Connection','close')
            self.end_headers()
            if self.command != 'HEAD':
                for chunk in chunks:
                    self.wfile.write(chunk)
            self.server.owner.record_http_response(self.path, response.status, 'upstream', 'response')
        except (CaptureError, OSError, ValueError, http.client.HTTPException) as exc:
            if isinstance(exc,CaptureError) and exc.code in ('byte_budget','timeout','request_budget','proxy_unavailable'):
                self.server.owner.error = exc
            response_sent = False
            try:
                self.send_error(502, 'Public capture request refused')
                response_sent = True
            except OSError:
                pass
            self.server.owner.record_http_response(self.path, 502, 'local_proxy', phase,
                error=exc, response_sent=response_sent, upstream_status=upstream_status)
        finally:
            self.close_connection = True
            if upstream:
                self.server.owner.release(upstream)

    do_GET = forward
    do_HEAD = forward
    do_POST = forward
    do_PUT = forward
    do_PATCH = forward
    do_DELETE = forward
    do_OPTIONS = forward
