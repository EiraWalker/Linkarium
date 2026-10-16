"""Local capture network preferences; never reads Clash subscription or node secrets."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .models import CaptureError


def validate_proxy(value):
    try:
        if not isinstance(value, str) or any(c.isspace() for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        host = ipaddress.ip_address(parsed.hostname or '')
        if (parsed.scheme != 'http' or not host.is_loopback or not parsed.port or
                parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError()
        name = f'[{host}]' if host.version == 6 else str(host)
        return f'http://{name}:{parsed.port}'
    except (ValueError, TypeError):
        raise CaptureError('proxy_config', 'Use a local HTTP proxy address without credentials') from None


def detect_clash(appdata=None):
    base = Path(appdata or os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    for folder in ('io.github.clash-verge-rev.clash-verge-rev', 'io.github.clash-verge.clash-verge'):
        for name in ('clash-verge.yaml', 'config.yaml'):
            try:
                # Only top-level scalar ports; never parse/output subscriptions, nodes or controller secrets.
                path = base / folder / name
                if path.stat().st_size > 8 * 1024 * 1024:
                    continue
                content = path.read_text(encoding='utf-8-sig')
                for key in ('mixed-port', 'port'):
                    found = re.search(r'^' + key + r':\s*[\'"]?(\d+)[\'"]?\s*(?:#.*)?$', content, re.M)
                    if found and 0 < int(found[1]) <= 65535:
                        return f'http://127.0.0.1:{int(found[1])}'
            except (OSError, UnicodeError):
                continue
    return None


def load_config(data_root):
    path = Path(data_root) / 'runtime' / 'capture-network.json'
    try:
        if path.stat().st_size > 4096:
            raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
        return validate_config(data)
    except FileNotFoundError:
        return {'mode': 'direct', 'proxy_url': ''}
    except (OSError, ValueError, TypeError):
        raise CaptureError('proxy_config', 'Capture proxy settings could not be read') from None


def validate_config(data):
    if not isinstance(data, dict) or data.get('mode') not in ('direct', 'clash', 'manual'):
        raise CaptureError('proxy_config', 'Invalid capture proxy mode')
    return {'mode': data['mode'], 'proxy_url': validate_proxy(data.get('proxy_url')) if data['mode'] == 'manual' else ''}


def save_config(data_root, data):
    config = validate_config(data)
    path = Path(data_root) / 'runtime' / 'capture-network.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.capture-network-{uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps(config), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return config


def status(data_root):
    config = load_config(data_root)
    detected = detect_clash()
    active = detected if config['mode'] == 'clash' else config['proxy_url'] or None
    return {**config, 'detected_proxy_url': detected, 'active_proxy_url': active}


def configured_proxy(data_root):
    config = load_config(data_root)
    if config['mode'] == 'clash':
        detected = detect_clash()
        if not detected:
            raise CaptureError('proxy_config', 'Clash Verge local HTTP proxy was not found')
        return detected
    return config['proxy_url']


def request_proxy(request):
    if request.proxy_url:
        return validate_proxy(request.proxy_url)
    # Standard development child requests pick up changes without restarting the worker.
    output = Path(request.output_dir).resolve()
    if request.development and not request.allow_test_hosts and output.name == request.attempt_uuid and output.parent.name == 'capture' and output.parent.parent.name == 'work':
        return configured_proxy(output.parent.parent.parent)
    return ''


class TargetProxyError(OSError):
    """The local proxy is reachable, but this public destination did not connect."""


class HttpProxyConnector:
    """Tunnel validated literal destinations through a trusted local HTTP proxy."""
    def __init__(self, proxy_url):
        self.proxy = urlsplit(validate_proxy(proxy_url))

    def __call__(self, address, timeout):
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or address[1] not in (80, 443):
            raise CaptureError('unsafe_url', 'Only validated public endpoints may use the proxy')
        stream = None
        deadline = time.monotonic() + timeout
        try:
            stream = socket.create_connection((self.proxy.hostname, self.proxy.port), timeout=timeout)
            authority = f'[{ip}]:{address[1]}' if ip.version == 6 else f'{ip}:{address[1]}'
            stream.sendall(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('ascii'))
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(header) >= 16384:
                    raise OSError('Proxy handshake exceeded budget')
                stream.settimeout(remaining)
                chunk = stream.recv(1)  # Do not consume the first byte of the tunnel payload.
                if not chunk:
                    raise OSError('Proxy closed its handshake')
                header.extend(chunk)
            parts = header.split(b'\r\n', 1)[0].split()
            if len(parts) < 2 or parts[0] not in (b'HTTP/1.0', b'HTTP/1.1'):
                raise OSError('Proxy returned an invalid handshake')
            if parts[1] in (b'502', b'503', b'504'):
                # Retry only other already-validated addresses via the same proxy.
                # This is not evidence that the local proxy itself is unavailable.
                raise TargetProxyError('Proxy could not connect to the public destination')
            if parts[1] != b'200':
                raise OSError('Proxy rejected tunnel')
            return stream
        except TargetProxyError:
            if stream:
                stream.close()
            raise
        except OSError:
            if stream:
                stream.close()
            raise CaptureError('proxy_unavailable', 'Local proxy connection failed') from None
