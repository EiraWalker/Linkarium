"""Read one real GIF image block through the capture's existing bounded proxy.

An animated GIF's first frame is a valid static GIF when followed by a trailer.
The remaining animation need not be downloaded, decoded or embedded in an HTML
snapshot. Unsupported/truncated files are left to the normal browser pipeline.
"""
from contextlib import nullcontext
import re
import time
from urllib.parse import urljoin

import httpx

from .models import CaptureError

MAX_FRAME_BYTES = 16 * 1024 * 1024


class InvalidGif(ValueError):
    pass


class FirstGifFrame:
    def __init__(self):
        self.data = bytearray()
        self.position = 0
        self.state = 'header'
        self.image = False
        self.image_data = 0

    def feed(self, chunk):
        if len(self.data) + len(chunk) > MAX_FRAME_BYTES:
            raise CaptureError('byte_budget', 'Resource exceeds per-resource byte budget')
        self.data.extend(chunk)
        while True:
            remaining = len(self.data) - self.position
            if self.state == 'header':
                if remaining < 13:
                    return None
                if self.data[:6] not in (b'GIF87a', b'GIF89a'):
                    raise InvalidGif('Not a GIF')
                width = int.from_bytes(self.data[6:8], 'little')
                height = int.from_bytes(self.data[8:10], 'little')
                if not width or not height or width * height > 16_000_000:
                    raise InvalidGif('Unsupported GIF dimensions')
                packed = self.data[10]
                size = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 128 else 0)
                if remaining < size:
                    return None
                self.position = size
                self.state = 'block'
            elif self.state == 'block':
                if not remaining:
                    return None
                marker = self.data[self.position]
                if marker == 0x21:
                    if remaining < 2:
                        return None
                    self.position += 2
                    self.state = 'subblocks'
                elif marker == 0x2C:
                    if remaining < 10:
                        return None
                    width = int.from_bytes(self.data[self.position + 5:self.position + 7], 'little')
                    height = int.from_bytes(self.data[self.position + 7:self.position + 9], 'little')
                    if not width or not height or width * height > 16_000_000:
                        raise InvalidGif('Unsupported GIF frame dimensions')
                    packed = self.data[self.position + 9]
                    size = 11 + (3 * 2 ** ((packed & 7) + 1) if packed & 128 else 0)
                    if remaining < size:
                        return None
                    if not 2 <= self.data[self.position + size - 1] <= 8:
                        raise InvalidGif('Invalid LZW code size')
                    self.position += size
                    self.image = True
                    self.state = 'subblocks'
                else:
                    raise InvalidGif('Missing first GIF image')
            else:
                if not remaining:
                    return None
                size = self.data[self.position]
                if remaining < size + 1:
                    return None
                self.position += size + 1
                if self.image:
                    self.image_data += size
                if size == 0:
                    if self.image:
                        if not self.image_data:
                            raise InvalidGif('Empty GIF image')
                        return bytes(self.data[:self.position]) + b'\x3b'
                    self.state = 'block'


def open_gif_client(proxy):
    config = proxy.configuration
    return httpx.Client(proxy=httpx.Proxy(config['server'], auth=(config['username'], config['password'])),
                        trust_env=False, follow_redirects=False, timeout=8)


def fetch_first_frame(url, headers, proxy, charge_redirect, client=None):
    """No direct fallback, ambient proxy, automatic redirect or unbounded body.

    A complete small range keeps the TLS connection reusable across a gallery.
    Further ranges are requested only if the first image block is incomplete.
    A server ignoring Range is streamed only until that complete image exists.
    """
    safe_headers = {name: value for name, value in headers.items()
                    if name.lower() in ('user-agent', 'accept', 'accept-language')}
    safe_headers['Accept-Encoding'] = 'identity'
    parser, start, end, redirects = FirstGifFrame(), 0, 128 * 1024 - 1, 0
    validator, validator_header, total_size = None, None, None
    with (nullcontext(client) if client is not None else open_gif_client(proxy)) as client:
        for _ in range(16):
            proxy.budget()
            proxy.policy.validate(url)
            remaining = proxy.deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureError('timeout', 'Capture exceeded its time budget')
            safe_headers['Range'] = f'bytes={start}-{end}'
            try:
                with client.stream('GET', url, headers=safe_headers, timeout=min(8, remaining)) as response:
                    if proxy.error:
                        raise proxy.error
                    if response.status_code in (301, 302, 303, 307, 308):
                        if redirects == 5 or not response.headers.get('location'):
                            return None
                        redirects += 1
                        url = urljoin(url, response.headers['location'])
                        parser, start, end = FirstGifFrame(), 0, 128 * 1024 - 1
                        validator, validator_header, total_size = None, None, None
                        safe_headers.pop('If-Range', None)
                        charge_redirect()
                        continue
                    if response.status_code not in (200, 206):
                        return None
                    if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'image/gif':
                        return None
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        return None
                    segment = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)',
                                           response.headers.get('content-range', '')) if response.status_code == 206 else None
                    if response.status_code == 206:
                        if (not segment or int(segment[1]) != start or not start <= int(segment[2]) <= end):
                            return None
                        if segment[3] != '*' and int(segment[2]) >= int(segment[3]):
                            return None
                        if start and (segment[3] != total_size or response.headers.get(validator_header) != validator):
                            return None
                        total_size = segment[3]
                    elif start:
                        return None
                    frame, received = None, 0
                    for chunk in response.iter_bytes(16384):
                        proxy.budget()
                        received += len(chunk)
                        if segment and received > int(segment[2]) - start + 1:
                            return None
                        if frame is None:
                            frame = parser.feed(chunk)
                        if frame is not None and segment is None:
                            break
                    if segment and received != int(segment[2]) - start + 1:
                        return None
                    if frame is not None:
                        return frame, {name: value for name, value in response.headers.items()
                            if name in ('access-control-allow-origin', 'access-control-allow-credentials',
                                        'cross-origin-resource-policy', 'vary')}
                    if not segment or received != int(segment[2]) - start + 1:
                        return None
                    start = int(segment[2]) + 1
                    if start >= MAX_FRAME_BYTES:
                        raise CaptureError('byte_budget', 'Resource exceeds per-resource byte budget')
                    if segment[3] != '*' and start >= int(segment[3]):
                        return None
                    end = min(MAX_FRAME_BYTES - 1, max(start, (end + 1) * 4 - 1))
                    if validator is None:
                        etag = response.headers.get('etag')
                        validator_header = 'etag' if etag and not etag.startswith('W/') else 'last-modified'
                        validator = response.headers.get(validator_header)
                        if not validator:
                            return None
                    safe_headers['If-Range'] = validator
                    charge_redirect()
            except InvalidGif:
                return None
            except httpx.HTTPError:
                if proxy.error:
                    raise proxy.error
                return None
    return None
