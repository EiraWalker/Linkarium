"""Logical HTML objects; disk encoding never escapes this boundary.

Canonical paths and hashes always describe the uncompressed bytes. This module
has no Django dependency and can also be used by offline maintenance tools.
"""
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import tempfile
import zlib

from capture.models import MAX_CAPTURE_BYTES

_BLOCK_SIZE = 64 * 1024


class ArtifactStorageError(ValueError):
    """An object has an unsafe path, invalid encoding or invalid content."""


def _safe_path(path):
    path = Path(path)
    if ".." in path.parts:
        raise ArtifactStorageError("Artifact path contains parent traversal")
    path = path.absolute()
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ArtifactStorageError("Artifact paths cannot contain links or reparse points")
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise ArtifactStorageError("Artifact parent is not a directory")
    return path


def validate_artifact_path(canonical_path):
    """Validate the logical name and both representations, even if absent."""
    canonical = Path(canonical_path)
    if re.fullmatch(r"[0-9a-f]{64}\.html", canonical.name) is None:
        raise ArtifactStorageError("Invalid canonical artifact filename")
    canonical = _safe_path(canonical)
    for path in (canonical, Path(str(canonical) + ".gz")):
        _safe_path(path)
        if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
            raise ArtifactStorageError("Artifact representation is not a regular file")
    return canonical


def _open_regular(path):
    path = _safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactStorageError("Artifact source is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        _safe_path(path)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, path.lstat()):
            raise ArtifactStorageError("Artifact changed while opening")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def storage_path(canonical_path):
    """Return the existing representation, preferring legacy raw HTML."""
    canonical = validate_artifact_path(canonical_path)
    if canonical.is_file():
        return canonical
    compressed = Path(str(canonical) + ".gz")
    if compressed.is_file():
        return compressed
    raise FileNotFoundError(canonical)


class _ArtifactReader(io.RawIOBase):
    # Deliberately no name, seek or tell: HTTP must never infer compressed size.
    def __init__(self, physical, digest, expected_size):
        super().__init__()
        self._file = _open_regular(physical)
        self._stream = gzip.GzipFile(fileobj=self._file, mode="rb") if physical.suffix == ".gz" else self._file
        self._digest = digest
        self._expected_size = expected_size
        self._limit = MAX_CAPTURE_BYTES if expected_size is None else expected_size
        self._hash = hashlib.sha256()
        self._size = 0
        self._verified = False
        self._pending = b""

    def readable(self):
        return True

    def read(self, size=-1):
        self._checkClosed()
        if size == 0 or self._verified:
            return b""
        pieces = []
        remaining = size
        try:
            while remaining is None or remaining != 0:
                length = min(_BLOCK_SIZE, self._limit - self._size)
                if remaining is not None and remaining > 0:
                    length = min(length, remaining)
                block = self._pending + self._stream.read(length - len(self._pending))
                # Do not release the final block until EOF also verifies gzip's
                # trailer and our identity. One pending byte keeps read(n)
                # bounded without making an early close drain the whole file.
                self._pending = self._stream.read(1)
                self._size += len(block)
                if self._size + len(self._pending) > self._limit:
                    raise ArtifactStorageError("Artifact exceeds its logical byte budget")
                self._hash.update(block)
                if not self._pending:
                    if ((self._expected_size is not None and self._size != self._expected_size)
                            or self._hash.hexdigest() != self._digest):
                        raise ArtifactStorageError("Artifact content does not match its identity")
                    self._verified = True
                pieces.append(block)
                if self._verified:
                    break
                if remaining is not None and remaining > 0:
                    remaining -= len(block)
            return b"".join(pieces)
        except (OSError, EOFError, zlib.error) as error:
            self.close()
            raise ArtifactStorageError("Artifact encoding is corrupt or truncated") from error
        except BaseException:
            self.close()
            raise

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def __iter__(self):
        return self

    def __next__(self):
        data = self.read(_BLOCK_SIZE)
        if not data:
            raise StopIteration
        return data

    def close(self):
        try:
            if hasattr(self, "_stream"):
                self._stream.close()
        finally:
            if hasattr(self, "_file"):
                self._file.close()
            super().close()


def open_artifact(canonical_path, *, expected_size=None):
    """Open bounded logical HTML, verifying EOF before returning the final bytes.

    Early close releases both handles without draining an abandoned response.
    Consumers needing trust before use must finish reading before using bytes.
    """
    if expected_size is not None and (type(expected_size) is not int or not 0 <= expected_size <= MAX_CAPTURE_BYTES):
        raise ArtifactStorageError("Invalid expected artifact size")
    return _ArtifactReader(storage_path(canonical_path), Path(canonical_path).stem, expected_size)


def read_artifact(canonical_path, *, expected_size=None):
    """Read logical HTML, never its on-disk encoding."""
    with open_artifact(canonical_path, expected_size=expected_size) as stream:
        return stream.read()


def inspect_source(source_path):
    """Return the original SHA-256 and byte size of a bounded, regular source."""
    return _copy_source(source_path)


def read_capture_source(source_path):
    """Read bounded raw capture input before it has a content-addressed name."""
    output = io.BytesIO()
    _copy_source(source_path, output)
    return output.getvalue()


def _copy_source(source_path, output=None, digest=None):
    checksum = hashlib.sha256()
    size = 0
    with _open_regular(source_path) as source:
        while block := source.read(_BLOCK_SIZE):
            size += len(block)
            if size > MAX_CAPTURE_BYTES:
                raise ArtifactStorageError("Artifact exceeds its logical byte budget")
            checksum.update(block)
            if output is not None:
                output.write(block)
    if digest is not None and checksum.hexdigest() != digest:
        raise ArtifactStorageError("Source does not match the canonical SHA-256")
    return checksum.hexdigest(), size


def _fsync_directory(directory):
    # Windows does not expose fsync-able directory descriptors. Its file data
    # is still flushed before replace; POSIX additionally persists the entry.
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_artifact(source_path, canonical_path):
    """Atomically publish verified HTML, using gzip only when strictly smaller.

    Existing valid representations are reused, not rewritten. The source and
    existing objects are never removed, including after failed publication.
    """
    canonical = validate_artifact_path(canonical_path)
    _safe_path(source_path)
    # Existence is not proof of durability: an earlier failed call may have
    # created any ancestor. With no durable root marker, sync the full finite
    # parent chain (leaf to filesystem root) on both publication and reuse.
    directories = canonical.parents
    canonical.parent.mkdir(parents=True, exist_ok=True)
    validate_artifact_path(canonical)
    fd, name = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=canonical.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            with gzip.GzipFile(filename="", mode="wb", compresslevel=6, mtime=0, fileobj=output) as compressed:
                _, size = _copy_source(source_path, compressed, canonical.stem)
            compressed_size = output.tell()
            try:
                existing = storage_path(canonical)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                with open_artifact(canonical, expected_size=size) as stream:
                    for _ in stream:
                        pass
                # A prior replace may have succeeded but directory fsync failed.
                # Retrying must establish durability before returning success.
                for directory in directories:
                    _fsync_directory(directory)
                return existing
            destination = Path(str(canonical) + ".gz")
            if compressed_size >= size:
                output.seek(0)
                output.truncate()
                _copy_source(source_path, output, canonical.stem)
                destination = canonical
            output.flush()
            os.fsync(output.fileno())
        validate_artifact_path(canonical)
        os.replace(temporary, destination)
        for directory in directories:
            _fsync_directory(directory)
        return destination
    finally:
        _safe_path(temporary).unlink(missing_ok=True)


def delete_artifact(canonical_path):
    """Remove both representations without following links; missing is a no-op."""
    canonical = validate_artifact_path(canonical_path)
    for path in (canonical, Path(str(canonical) + ".gz")):
        _safe_path(path).unlink(missing_ok=True)


def artifact_mtime(canonical_path):
    """Newest representation's mtime, or None if absent (GC grace period)."""
    canonical = validate_artifact_path(canonical_path)
    times = []
    for path in (canonical, Path(str(canonical) + ".gz")):
        try:
            times.append(path.stat().st_mtime)
        except FileNotFoundError:
            pass
    return max(times, default=None)


def iter_artifacts(artifact_root):
    """Yield canonical identities once, without following linked directories.

    Ignore unknown names, staging files and unsafe entries; only two-hex shards
    containing their matching content-addressed filenames belong to this store.
    """
    root = _safe_path(artifact_root)
    if not root.exists():
        return
    for shard in root.iterdir():
        if re.fullmatch(r"[0-9a-f]{2}", shard.name) is None:
            continue
        try:
            if not _safe_path(shard).is_dir():
                continue
        except ArtifactStorageError:
            continue
        seen = set()
        for physical in shard.iterdir():
            match = re.fullmatch(r"([0-9a-f]{64})\.html(?:\.gz)?", physical.name)
            if match is None or not match[1].startswith(shard.name):
                continue
            canonical = shard / f"{match[1]}.html"
            if canonical in seen:
                continue
            seen.add(canonical)
            try:
                yield validate_artifact_path(canonical)
            except ArtifactStorageError:
                continue
