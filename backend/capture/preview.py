"""Bounded, non-executable first-screen derivatives of validated HTML archives."""
import hashlib
import os
from pathlib import Path
import stat

from .models import CaptureError

MAX_PREVIEW_BYTES = 4 * 1024 * 1024
PREVIEW_QUALITY = 82


def preview_details(content, *, width=None, height=None):
    """Validate the JPEG envelope and dimensions without adding an image decoder."""
    if not 4 <= len(content) <= MAX_PREVIEW_BYTES or content[:2] != b'\xff\xd8' or content[-2:] != b'\xff\xd9':
        raise CaptureError('invalid_preview', 'Preview is not a bounded JPEG image')
    offset, dimensions, scan = 2, None, False
    while offset < len(content) - 2:
        if content[offset] != 0xff:
            raise CaptureError('invalid_preview', 'Preview JPEG header is invalid')
        while offset < len(content) and content[offset] == 0xff:
            offset += 1
        if offset >= len(content):
            break
        marker = content[offset]
        offset += 1
        if marker in (0, 0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            raise CaptureError('invalid_preview', 'Preview JPEG header is invalid')
        if offset + 2 > len(content):
            break
        length = int.from_bytes(content[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(content) - 2:
            raise CaptureError('invalid_preview', 'Preview JPEG segment is invalid')
        if marker in (0xc0, 0xc1, 0xc2):
            if dimensions is not None or length < 8 or content[offset + 2] != 8:
                raise CaptureError('invalid_preview', 'Preview JPEG dimensions are invalid')
            dimensions = (int.from_bytes(content[offset + 5:offset + 7], 'big'),
                          int.from_bytes(content[offset + 3:offset + 5], 'big'))
        if marker == 0xda:
            scan = True
            break
        offset += length
    if not scan or dimensions is None or not all(0 < value <= 4096 for value in dimensions):
        raise CaptureError('invalid_preview', 'Preview JPEG dimensions are invalid')
    if (width is not None and dimensions[0] != width) or (height is not None and dimensions[1] != height):
        raise CaptureError('invalid_preview', 'Preview dimensions do not match the archive viewport')
    return {'sha256': hashlib.sha256(content).hexdigest(), 'byte_size': len(content),
            'width': dimensions[0], 'height': dimensions[1]}


def validate_preview(path, expected_sha256=None, expected_size=None, width=None, height=None):
    """Verify a bounded regular file before publishing or serving a JPEG derivative."""
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 4 <= info.st_size <= MAX_PREVIEW_BYTES:
        raise CaptureError('invalid_preview', 'Preview is not a bounded nonlinked regular file')
    details = preview_details(path.read_bytes(), width=width, height=height)
    if ((expected_sha256 is not None and details['sha256'] != expected_sha256)
            or (expected_size is not None and details['byte_size'] != expected_size)):
        raise CaptureError('invalid_preview', 'Preview integrity check failed')
    return details


def screenshot_preview(page, width, height, timeout_ms=10000):
    """Capture the already-loaded first viewport; never navigate or fetch a source URL."""
    content = page.screenshot(type='jpeg', quality=PREVIEW_QUALITY, full_page=False,
        scale='css', animations='disabled', caret='hide', timeout=timeout_ms)
    preview_details(content, width=width, height=height)
    return content


def write_preview(page, directory, width, height, timeout_ms=10000):
    content = screenshot_preview(page, width, height, timeout_ms)
    details = preview_details(content, width=width, height=height)
    target = Path(directory) / (details['sha256'] + '.jpg')
    if target.exists() or target.is_symlink():
        validate_preview(target, details['sha256'], details['byte_size'], width, height)
    else:
        created = False
        try:
            with target.open('xb') as destination:
                created = True
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError:
            if created:
                target.unlink(missing_ok=True)
            raise
    return {'path': str(target), **details}
