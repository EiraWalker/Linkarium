"""Early URL/DNS validation. Production additionally requires kernel egress isolation."""
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

from .models import CaptureError


class SystemResolver:
    """Resolve once per capture and retain the exact addresses used by its proxy."""
    def __init__(self, deadline):
        self.deadline = deadline
        self.cache = {}
        self.lock = threading.RLock()
        self.stopped = False

    def resolve(self, host, port=443):
        with self.lock:
            if self.stopped or time.monotonic() >= self.deadline:
                raise CaptureError('timeout', 'Capture DNS time budget exhausted')
            key = (host, port)
            if key not in self.cache:
                if len(self.cache) >= 128:
                    raise CaptureError('dns_budget', 'Capture DNS query budget exhausted')
                answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                addresses = list(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in answers))
                if len(addresses) > 32:
                    raise CaptureError('dns_budget', 'Capture DNS answer count exceeds budget')
                self.cache[key] = addresses
            return list(self.cache[key])

    def close(self):
        with self.lock:
            self.stopped = True


class PublicURLPolicy:
    def __init__(self, allow_test_hosts=(), deny_hosts=(), resolver=None):
        self.allow_test_hosts = set(allow_test_hosts)
        self.deny_hosts = set()
        self.deny_host_suffixes = set()
        for configured_host in deny_hosts:
            # Match Django ALLOWED_HOSTS conventions: a leading dot includes the
            # base domain and subdomains, while an ordinary entry is exact.
            suffix = configured_host.startswith('.')
            host = configured_host[1:] if suffix else configured_host
            host = host.removeprefix('[').removesuffix(']').encode('idna').decode('ascii').lower().rstrip('.')
            if not host or host == '*':
                raise CaptureError('invalid_request', 'Capture deny hosts must be explicit host names')
            (self.deny_host_suffixes if suffix else self.deny_hosts).add(host)
        self.resolver = resolver

    def validate(self, url: str) -> str:
        self.resolve(url)
        return url

    def resolve(self, url: str) -> list:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except (ValueError, UnicodeError) as exc:
            raise CaptureError("unsafe_url", "Malformed target URL") from exc
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            raise CaptureError("unsafe_url", "Only public HTTP/HTTPS targets without credentials are allowed")
        if host in self.deny_hosts or any(host == suffix or host.endswith('.' + suffix)
                                         for suffix in self.deny_host_suffixes):
            raise CaptureError("unsafe_url", "Application and deployment hosts cannot be captured")
        if host in self.allow_test_hosts:
            # This exception is constructed by the test harness, never exposed through the product API.
            return self.resolver.resolve(host, port) if self.resolver else [
                ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        if port not in (80, 443) or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise CaptureError("unsafe_url", "Target host or port is not public")
        # Reject shortened/integer/octal address spellings interpreted as IPs by browsers.
        if host.replace(".", "").isdigit() or host.startswith("0x"):
            try:
                ipaddress.ip_address(host)
            except ValueError as exc:
                raise CaptureError("unsafe_url", "Noncanonical IP spelling") from exc
        try:
            literal = ipaddress.ip_address(host)
            addresses = [literal]
        except ValueError:
            try:
                addresses = self.resolver.resolve(host, port) if self.resolver else [
                    ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
            except OSError as exc:
                raise CaptureError("dns_failed", "Target DNS lookup failed") from exc
        if not addresses:
            raise CaptureError("dns_failed", "Target has no address")
        for address in addresses:
            effective = getattr(address, "ipv4_mapped", None) or address
            if not effective.is_global or effective.is_multicast:
                raise CaptureError("unsafe_url", "Target DNS includes a nonpublic address")
        # Production IPv6 is blocked at the launcher until its containment is separately verified.
        return addresses
