"""Bound bodies before Waitress spools them; large uploads need an owner-issued lease.

The lease is an opaque, one-use capability issued by a CSRF-protected API. Header
parsing never queries SQLite, trusts forwarding headers, or authenticates a cookie.
Only one pending or active large upload can reserve the data volume at a time.
"""
import copy
import secrets
import shutil
import threading
import time
from pathlib import Path

from waitress.channel import HTTPChannel
from waitress.parser import HTTPRequestParser
from waitress.server import BaseWSGIServer

from library.errors import ApiError

ANONYMOUS_LIMIT = 1024**2
UPLOAD_MIN_SECONDS = 120
UPLOAD_MAX_SECONDS = 3600
UPLOAD_BYTES_PER_SECOND = 64 * 1024
UPLOAD_LIMITS = {
    '/api/v1/imports/preview': 21 * 1024**2,
    '/api/v1/imports': 21 * 1024**2,
    '/api/v1/restores/validate': 16 * 1024**3,
}


class UploadAdmissions:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.pending = {}
        self.active = set()

    def issue(self, path, size):
        if not isinstance(path, str) or path not in UPLOAD_LIMITS or type(size) is not int or not 0 < size <= UPLOAD_LIMITS[path]:
            raise ApiError('UPLOAD_SIZE', '文件超过该上传入口的容量限制', 413)
        # Waitress, Django multipart and the durable restore source can coexist.
        if shutil.disk_usage(self.root).free < size * 3 + 32 * 1024**2:
            raise ApiError('INSUFFICIENT_DISK_SPACE', '磁盘空间不足，无法接收本次上传', 409)
        with self.lock:
            now = time.monotonic()
            self.pending = {token: row for token, row in self.pending.items() if row[2] > now}
            if self.pending or self.active:
                raise ApiError('UPLOAD_BUSY', '已有文件正在上传，请稍后重试', 409)
            token = secrets.token_urlsafe(32)
            self.pending[token] = (path, size, now + 120)
            return token

    def claim(self, token, method, path, declared):
        with self.lock:
            row = self.pending.get(token)
            if (row is None or row[2] <= time.monotonic() or method != 'POST'
                    or path != row[0] or declared > row[1]):
                return None
            del self.pending[token]
            self.active.add(token)
            return row[1]

    def release(self, token):
        with self.lock:
            self.active.discard(token)


_admissions = None


def upload_admissions():
    global _admissions
    if _admissions is None:
        from config.paths import DATA_ROOT
        _admissions = UploadAdmissions(DATA_ROOT)
    return _admissions


def install_body_gate(transport, admissions=None):
    """Install on every IPv4/IPv6 listener before starting the Waitress loop."""
    admissions = admissions or upload_admissions()

    class GatedParser(HTTPRequestParser):
        lease = None
        upload_deadline = None

        def parse_header(self, header_plus):
            super().parse_header(header_plus)
            self.adj = copy.copy(self.adj)
            limit = ANONYMOUS_LIMIT
            token = self.headers.get('X_LINKARIUM_UPLOAD', '')
            if token:
                allowed = admissions.claim(token, self.command, self.path, self.content_length)
                if allowed is not None:
                    self.lease = token
                    limit = allowed
                    self.upload_deadline = time.monotonic() + min(UPLOAD_MAX_SECONDS,
                        max(UPLOAD_MIN_SECONDS, 30 + allowed / UPLOAD_BYTES_PER_SECOND))
            # Waitress rejects at >= max_body, so add one to express an inclusive cap.
            self.adj.max_request_body_size = limit + 1
            if self.content_length > limit:
                self.expect_continue = False

        def close(self):
            try:
                super().close()
            finally:
                if self.lease is not None:
                    admissions.release(self.lease)
                    self.lease = None
                    self.upload_deadline = None

    class GatedChannel(HTTPChannel):
        parser_class = GatedParser

        def readable(self):
            pending = self.request
            if (pending is not None and not pending.completed and pending.upload_deadline is not None
                    and time.monotonic() >= pending.upload_deadline):
                # Absolute time, independent of the last byte received. A trickle
                # must not hold the sole disk reservation forever.
                self.handle_close()
                return False
            return super().readable()

        def close_pending_body(self):
            pending, self.request = self.request, None
            if pending is not None:
                pending.close()

        def handle_close(self):
            try:
                # Waitress normally closes completed parsers in service(); its
                # socket-close path leaves an incomplete parser unclosed.
                self.close_pending_body()
            finally:
                super().handle_close()

        def handle_read(self):
            super().handle_read()
            if not self.connected:
                # An orderly EOF sets connected=False without handle_close().
                self.handle_close()

        def cancel(self):
            self.close_pending_body()
            for pending in self.requests:
                pending.close()
            super().cancel()

    for server in transport.values():
        if isinstance(server, BaseWSGIServer):
            server.channel_class = GatedChannel
