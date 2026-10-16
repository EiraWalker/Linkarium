"""Deliver one immutable archive without copying it through browser-control IPC.

This is a local, non-forwarding HTTP proxy for a single replay context. It is
never the proxy used to fetch source websites and cannot open outbound sockets.
"""
import secrets
import socket
import socketserver
import threading
import time

from .models import CaptureError


class ReplayDocument:
    def __init__(self, content: bytes, csp: str, deadline: float):
        if '\r' in csp or '\n' in csp:
            raise ValueError('Invalid replay policy header')
        self.content = content
        self.deadline = deadline
        self.url = 'http://snapshot.invalid/' + secrets.token_urlsafe(32)
        self._target = self.url.encode('ascii')
        self._headers = (
            'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n'
            f'Content-Length: {len(content)}\r\nContent-Security-Policy: {csp}\r\n'
            'Cache-Control: no-store\r\nConnection: close\r\n\r\n'
        ).encode('ascii')
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._active = None
        self._server = None
        self._thread = None

    def __enter__(self):
        if time.monotonic() >= self.deadline:
            raise CaptureError('timeout', 'Capture exceeded its time budget')
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                with owner._lock:
                    owner._active = self.request
                try:
                    owner._serve(self.request)
                except (OSError, TimeoutError):
                    pass  # Closed client, shutdown, or the shared deadline.
                finally:
                    with owner._lock:
                        owner._active = None

        self._server = socketserver.TCPServer(('127.0.0.1', 0), Handler, bind_and_activate=True)
        self.proxy_url = f'http://127.0.0.1:{self._server.server_address[1]}'
        try:
            self._thread = threading.Thread(target=self._server.serve_forever,
                kwargs={'poll_interval': 0.05}, name='capture-replay-document', daemon=True)
            self._thread.start()
        except BaseException:
            self._server.server_close()
            raise
        return self

    def _remaining(self, cap=None):
        seconds = self.deadline - time.monotonic()
        if self._stopping.is_set() or seconds <= 0:
            raise TimeoutError('Replay delivery deadline reached')
        return min(seconds, cap) if cap is not None else seconds

    def _allowed(self, header):
        lines = header.split(b'\r\n')
        if lines[0] not in (b'GET ' + self._target + b' HTTP/1.1',
                            b'GET ' + self._target + b' HTTP/1.0'):
            return False
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b':')
            if not separator or not name or name.strip() != name:
                return False
            name = name.lower()
            if name in headers:
                return False
            headers[name] = value.strip()
        return (headers.get(b'host', b'').lower() == b'snapshot.invalid'
                and headers.get(b'content-length', b'0') == b'0'
                and b'transfer-encoding' not in headers and b'upgrade' not in headers)

    def _write(self, connection, data):
        view = memoryview(data)
        for offset in range(0, len(view), 64 * 1024):
            connection.settimeout(self._remaining())
            connection.sendall(view[offset:offset + 64 * 1024])

    def _serve(self, connection):
        incoming = bytearray()
        while b'\r\n\r\n' not in incoming and len(incoming) < 8192:
            connection.settimeout(self._remaining(1.0))
            chunk = connection.recv(min(2048, 8192 - len(incoming)))
            if not chunk:
                return
            incoming.extend(chunk)
        header, separator, extra = bytes(incoming).partition(b'\r\n\r\n')
        if not separator or extra or not self._allowed(header):
            self._write(connection, b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            return
        self._write(connection, self._headers)
        self._write(connection, self.content)

    def __exit__(self, exc_type, _exc_value, _traceback):
        self._stopping.set()
        with self._lock:
            active = self._active
            if active:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    active.close()
                except OSError:
                    pass
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=1)
            if self._thread.is_alive() and exc_type is None:
                raise CaptureError('replay_failed', 'Replay document service did not stop')
