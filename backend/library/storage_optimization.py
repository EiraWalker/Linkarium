"""Offline, group-at-a-time artifact maintenance; no Django or database access.

Only explicitly supplied data roots participate. Reports contain aggregate counts,
never artifact contents or URLs. Dry runs acquire supervisor locks, but do not
change artifact files. Callers must stop all writers before running this tool.
"""
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Literal, overload
import zlib

from capture.models import MAX_CAPTURE_BYTES as MAX_ARTIFACT_BYTES
from runtime.instance_lock import InstanceLock


DEFAULT_SAFETY_MARGIN_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class OptimizationError(ValueError):
    """A privacy-safe reason code, not a path or content-bearing OS message."""


@dataclass(frozen=True)
class _Entry:
    path: Path
    sha: str
    kind: str
    info: os.stat_result

    @property
    def inode(self):
        return self.info.st_dev, self.info.st_ino


def _allocated_bytes(path, info):
    if hasattr(info, "st_blocks"):
        return info.st_blocks * 512
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FileStandardInfo(ctypes.Structure):
            _fields_ = [("allocation", ctypes.c_longlong),
                        ("end", ctypes.c_longlong), ("links", wintypes.DWORD),
                        ("delete_pending", wintypes.BOOLEAN),
                        ("directory", wintypes.BOOLEAN)]

        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel.GetFileInformationByHandleEx
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        query.restype = wintypes.BOOL
        with path.open("rb") as stream:
            result = FileStandardInfo()
            if not query(msvcrt.get_osfhandle(stream.fileno()), 1,
                         ctypes.byref(result), ctypes.sizeof(result)):
                raise OptimizationError("allocation_query_failed")
            return result.allocation
    raise OptimizationError("allocation_query_unsupported")


@overload
def _safe_path(path: Path, *, missing_ok: Literal[False] = False) -> os.stat_result: ...


@overload
def _safe_path(path: Path, *, missing_ok: Literal[True]) -> os.stat_result | None: ...


def _safe_path(path: Path, *, missing_ok=False):
    """Check every ancestor before resolving or opening; reject all reparses."""
    if ".." in path.parts:
        raise OptimizationError("unsafe_path")
    info = None
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise OptimizationError("missing_path") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OptimizationError("unsafe_path")
    if info is None:
        raise OptimizationError("missing_path")
    return info


def _roots(data_roots):
    roots = sorted(Path(root).absolute() for root in data_roots)
    if not roots:
        raise OptimizationError("no_data_roots")
    root_inodes = set()
    for index, root in enumerate(roots):
        root_info = _safe_path(root)
        if not stat.S_ISDIR(root_info.st_mode):
            raise OptimizationError("invalid_data_root")
        root_inode = root_info.st_dev, root_info.st_ino
        if root_inode in root_inodes:
            raise OptimizationError("overlapping_roots")
        root_inodes.add(root_inode)
        for previous in roots[:index]:
            if root == previous or root in previous.parents or previous in root.parents:
                raise OptimizationError("overlapping_roots")
        runtime = root / "runtime"
        info = _safe_path(runtime, missing_ok=True)
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise OptimizationError("unsafe_path")
        info = _safe_path(runtime / "supervisor.lock", missing_ok=True)
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise OptimizationError("unsafe_path")
    lock_infos = [_safe_path(root / "runtime/supervisor.lock", missing_ok=True) for root in roots]
    lock_counts = Counter((info.st_dev, info.st_ino) for info in lock_infos if info is not None)
    if any(info is not None and info.st_nlink != lock_counts[info.st_dev, info.st_ino]
           for info in lock_infos):
        raise OptimizationError("unsafe_path")
    return roots


def _acquire_locks(roots, stack, report):
    owned = set()
    for root in roots:
        path = root / "runtime/supervisor.lock"
        info = _safe_path(path, missing_ok=True)
        if info is None or (info.st_dev, info.st_ino) not in owned:
            lock = InstanceLock(path)
            try:
                stack.enter_context(lock)
            except RuntimeError:
                raise OptimizationError("lock_conflict") from None
            # InstanceLock permits same-thread nesting for preparation. This
            # destructive offline operation deliberately refuses such nesting.
            if lock.file is None:
                raise OptimizationError("lock_conflict")
            handle_info = os.fstat(lock.file.fileno())
            info = _safe_path(path)
            if info is None or (info.st_dev, info.st_ino) != (handle_info.st_dev, handle_info.st_ino):
                raise OptimizationError("concurrent_change")
            owned.add((info.st_dev, info.st_ino))
            report["physical_locks"] += 1
        report["locked_data_roots"] += 1
    # Recheck all aliases while all physical lock handles remain held.
    _roots(roots)
    for root in roots:
        info = _safe_path(root / "runtime/supervisor.lock")
        if info is None or (info.st_dev, info.st_ino) not in owned:
            raise OptimizationError("concurrent_change")


def _directory(path):
    if not stat.S_ISDIR(_safe_path(path).st_mode):
        raise OptimizationError("unsafe_path")
    return sorted(path.iterdir())


def _scan(roots):
    entries = []
    for root in roots:
        for generation in _directory(root / "generations"):
            if not stat.S_ISDIR(_safe_path(generation).st_mode):
                raise OptimizationError("unsafe_path")
            artifacts = generation / "artifacts"
            if _safe_path(artifacts, missing_ok=True) is None:
                continue
            for shard in _directory(artifacts):
                _safe_path(shard)
                if not re.fullmatch("[0-9a-f]{2}", shard.name):
                    raise OptimizationError("noncanonical_artifact")
                for path in _directory(shard):
                    info = _safe_path(path)
                    match = re.fullmatch(r"([0-9a-f]{64})\.html(\.gz)?(\.optimize-(?:tmp|link))?", path.name)
                    if (not stat.S_ISREG(info.st_mode) or not match or
                            not match[1].startswith(shard.name)):
                        raise OptimizationError("noncanonical_artifact")
                    kind = "gzip" if match[2] else "raw"
                    entries.append(_Entry(path, match[1], "temp_" + kind if match[3] else kind, info))
    return entries


def _snapshot(entries):
    unique = {entry.inode: entry for entry in entries}
    live = [entry for entry in entries if not entry.kind.startswith("temp_")]
    return {
        "allocated_bytes": sum(_allocated_bytes(e.path, e.info) for e in unique.values()),
        "unique_inodes": len(unique),
        "stored_bytes": sum(e.info.st_size for e in unique.values()),
        "raw_files": sum(e.kind == "raw" for e in entries),
        "gzip_files": sum(e.kind == "gzip" for e in entries),
        "temporary_files": sum(e.kind.startswith("temp_") for e in entries),
        "logical_objects": len({e.sha for e in live}),
        "object_aliases": len({(e.path.parent, e.sha) for e in live}),
        "groups": len({(e.info.st_dev, e.sha) for e in live}),
    }


def _check_links(entries, extra_links=None):
    aliases = Counter(entry.inode for entry in entries)
    aliases.update(extra_links or {})
    for entry in entries:
        if entry.info.st_nlink != aliases[entry.inode]:
            raise OptimizationError("unauthorized_hardlink")


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_mode, info.st_uid, info.st_gid)


def _fresh(entry):
    info = _safe_path(entry.path)
    if info is None or _signature(info) != _signature(entry.info):
        raise OptimizationError("concurrent_change")
    return _Entry(entry.path, entry.sha, entry.kind, info)


@contextmanager
def _open_checked(entry):
    _fresh(entry)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(entry.path, flags)
    with os.fdopen(descriptor, "rb") as source:
        if _signature(os.fstat(source.fileno())) != _signature(entry.info):
            raise OptimizationError("concurrent_change")
        yield source
        if _signature(os.fstat(source.fileno())) != _signature(entry.info):
            raise OptimizationError("concurrent_change")
    _fresh(entry)


def _verify(entry, sink=None):
    digest = hashlib.sha256()
    size = 0
    if entry.kind == "raw" and entry.info.st_size > MAX_ARTIFACT_BYTES:
        raise OptimizationError("size_limit")
    try:
        with _open_checked(entry) as source, ExitStack() as stack:
            stream = stack.enter_context(gzip.GzipFile(fileobj=source)) if entry.kind == "gzip" else source
            while chunk := stream.read(min(CHUNK_BYTES, MAX_ARTIFACT_BYTES - size + 1)):
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise OptimizationError("size_limit")
                digest.update(chunk)
                if sink is not None:
                    sink.write(chunk)
    except (gzip.BadGzipFile, EOFError, zlib.error):
        raise OptimizationError("invalid_gzip") from None
    if digest.hexdigest() != entry.sha:
        raise OptimizationError("sha_mismatch")
    return size


def _sync_directory(directory):
    # POSIX directory fsync makes each publication/deletion durable. Windows
    # exposes no portable equivalent; reports explicitly disclose this limit.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _remove(path):
    _safe_path(path)
    path.unlink()
    _sync_directory(path.parent)


def _publish(source, target):
    _safe_path(source)
    _safe_path(target, missing_ok=True)
    if target.exists() and os.path.samefile(source, target):
        _sync_directory(target.parent)
        return
    stage = target.with_name(target.name + ".optimize-link")
    os.link(source, stage)
    os.replace(stage, target)
    _sync_directory(target.parent)


def _metadata(path, info):
    # Windows _commit requires a writable descriptor, acquired before applying
    # a potentially read-only source mode.
    with path.open("r+b" if os.name == "nt" else "rb") as stream:
        current = path.stat()
        if os.name != "nt" and (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
            os.chown(path, info.st_uid, info.st_gid)
        os.chmod(path, stat.S_IMODE(info.st_mode))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.fsync(stream.fileno())


def _space_check(path, size, alias_count, reserve, *, create=True):
    # Conservative one-object deflate allowance plus directory/link metadata.
    # Never use guessed compression ratios or projected future reclaimed space.
    required = reserve + alias_count * 65536
    if create:
        required += size + size // 8 + CHUNK_BYTES
    if shutil.disk_usage(path.parent).free < required:
        raise OptimizationError("insufficient_space")


def _rewrite_group(entries, size, reserve, report, *, restore_raw=False, dry_run=False,
                   extra_links=None):
    _check_links([_fresh(entry) for entry in entries], extra_links)
    target_kind = "raw" if restore_raw else "gzip"
    old = [e for e in entries if e.kind != target_kind]
    ready = [e for e in entries if e.kind == target_kind]
    if not old and len({e.inode for e in ready}) == 1:
        return "already_raw" if restore_raw else "already_compressed"
    metadata = {(stat.S_IMODE(e.info.st_mode), e.info.st_uid, e.info.st_gid) for e in entries}
    if len(metadata) != 1:
        raise OptimizationError("metadata_conflict")
    suffix = ".html" if restore_raw else ".html.gz"
    aliases = sorted({e.path.parent / (e.sha + suffix) for e in entries})
    report["space_checked_groups"] += 1
    _space_check(entries[0].path, size, len(aliases), reserve, create=not ready)
    _check_links([_fresh(entry) for entry in entries], extra_links)
    if dry_run:
        return "dry_run"
    temporary = None
    if ready:
        candidate = min(ready, key=lambda e: e.info.st_size)
        if _verify(candidate) != size:
            raise OptimizationError("size_mismatch")
        report["sha_verified_files"] += 1
        with candidate.path.open("r+b" if os.name == "nt" else "rb") as stream:
            os.fsync(stream.fileno())
    else:
        source = old[0]
        temporary = source.path.with_name(source.sha + suffix + ".optimize-tmp")
        _safe_path(temporary, missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as output:
            if restore_raw:
                _verify(source, output)
            else:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output,
                                   compresslevel=6, mtime=0) as compressor:
                    _verify(source, compressor)
            report["sha_verified_files"] += 1
            output.flush()
            os.fsync(output.fileno())
        candidate = _Entry(temporary, source.sha, target_kind, temporary.stat())
        if _verify(candidate) != size:
            raise OptimizationError("size_mismatch")
        report["sha_verified_files"] += 1
        _metadata(temporary, source.info)
        candidate = _Entry(temporary, source.sha, target_kind, temporary.stat())
    old_allocated = sum(_allocated_bytes(e.path, e.info)
                        for e in {e.inode: e for e in entries}.values())
    if not restore_raw and old and (candidate.info.st_size >= size or
                                   _allocated_bytes(candidate.path, candidate.info) >= old_allocated):
        if temporary is not None:
            _remove(temporary)
        return "no_space_gain"
    # Every authorized alias has a durable replacement before any old deletion.
    _check_links([_fresh(entry) for entry in entries])
    for target in aliases:
        _publish(candidate.path, target)
    if temporary is not None:
        _remove(temporary)
    # A pre-existing gzip may have been written at a different time. Prefer
    # the first canonical raw alias's timestamps, including during resumption.
    timestamp_source = next((e for e in entries if e.kind == "raw"), entries[0])
    _metadata(aliases[0], timestamp_source.info)
    published = [_Entry(path, candidate.sha, target_kind, _safe_path(path)) for path in aliases]
    if any(e.inode != candidate.inode for e in published):
        raise OptimizationError("concurrent_change")
    _check_links(published)
    if _verify(published[0]) != size:
        raise OptimizationError("size_mismatch")
    report["sha_verified_files"] += 1
    remaining = Counter(entry.inode for entry in old)
    for entry in old:
        current = _fresh(entry)
        if current.info.st_nlink != remaining[current.inode]:
            raise OptimizationError("unauthorized_hardlink")
        _remove(entry.path)
        remaining[current.inode] -= 1
    return None


def optimize_artifacts(data_roots, *, apply=False, restore_raw=False,
                       safety_margin_bytes=DEFAULT_SAFETY_MARGIN_BYTES):
    """Validate, compress, or restore explicitly authorized offline data trees.

    ``data_roots`` is an iterable of directories containing ``generations``.
    Every generation's canonical artifacts participate; no database or manifest
    is read or modified. Stop all writers, including external filesystem tools.
    Supervisor locks remain held through the final inventory. Lock files may be
    created even in a dry run; artifact staging/publication requires ``apply``.

    Compression/restore stages at most one logical SHA per filesystem at a time.
    Every inode's links must be accounted for within these roots (including this
    tool's reserved ``.optimize-tmp``/``.optimize-link`` recovery names). All live
    inputs are validated before the first artifact mutation. Errors stop further
    groups, returning ``status='failed'`` without removing unreplaced originals.

    Counts are aggregate-only. ``sha_verified_files`` counts successful complete
    SHA verification passes, including staged and published output checks;
    ``sha_verified_aliases`` counts validated input filenames. Allocated bytes
    count unique (device, inode) pairs, including abandoned staging, using OS
    allocation information rather than logical sizes. ``tree_allocated_bytes_reduction``
    is before minus after in this inventory, NOT physically reclaimed capacity;
    it can be negative on restore. Unknown future compression/reclamation stays
    ``None``. ``filesystems`` has one row per distinct device (OS-local ``st_dev``)
    among roots and scanned artifacts: ``free_before``, ``free_after`` and
    ``free_delta`` (after minus before), all in bytes. Samples are taken after
    locking/initial inventory and after final inventory, while locks remain held.
    Missing samples/deltas are ``None`` and measurement errors fail the operation.
    Free-space deltas include other filesystem activity; an open reader can retain
    unlinked allocation until it closes. Neither delta proves exclusive physical
    reclamation. Every group checks current actual free space, never these deltas
    or a prediction of future savings.

    Windows read-only inputs and recovery files fail with ``readonly_artifact``
    before any artifact write or cleanup, even in dry runs. The attribute is never
    relaxed on a shared inode. POSIX read-only modes retain their existing handling.
    Mode/uid/gid must agree before deduplication; otherwise fail closed. The first
    raw alias supplies timestamps, or the first gzip if no raw remains. Timestamp
    precision is filesystem-dependent. POSIX supports directory fsync; Windows
    reports ``directory_fsync=False`` and does not claim power-loss durability.
    """
    report = {
        "status": "ok", "apply": apply,
        "mode": "restore_raw" if restore_raw else "compress",
        "before": None, "after": None,
        "sha_verified_files": 0, "sha_verified_aliases": 0,
        "groups_changed": 0, "groups_skipped": 0, "skips": {},
        "failures": [], "failure_count": 0,
        "estimated_reclaim_bytes": None, "estimated_compression_ratio": None,
        "tree_allocated_bytes_reduction": 0,
        "filesystems": [],
        "directory_fsync": os.name != "nt",
        "temporaries_removed": 0,
        "safety_margin_bytes": safety_margin_bytes,
        "space_checked_groups": 0,
        "locked_data_roots": 0, "physical_locks": 0,
    }
    scanned = False
    roots = []
    filesystem_paths = {}
    with ExitStack() as locks:
        try:
            if not isinstance(safety_margin_bytes, int) or safety_margin_bytes < 0:
                raise OptimizationError("invalid_safety_margin")
            roots = _roots(data_roots)
            _acquire_locks(roots, locks, report)
            entries = _scan(roots)
            report["before"] = _snapshot(entries)
            report["after"] = report["before"]
            scanned = True
            filesystem_paths = {_safe_path(root).st_dev: root for root in roots}
            for entry in entries:
                filesystem_paths.setdefault(entry.info.st_dev, entry.path.parent)
            report["filesystems"] = [
                {"device": device, "free_before": None, "free_after": None, "free_delta": None}
                for device in sorted(filesystem_paths)
            ]
            for observation in report["filesystems"]:
                observation["free_before"] = shutil.disk_usage(
                    filesystem_paths[observation["device"]]).free
            _check_links(entries)
            # Windows cannot unlink read-only files, including our recovery
            # aliases. Reject the entire inventory before cleanup/publication;
            # changing this attribute would also change every shared hardlink.
            if os.name == "nt" and any(
                    entry.info.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
                    for entry in entries):
                raise OptimizationError("readonly_artifact")
            checked = set()
            sizes = {}
            live = [entry for entry in entries if not entry.kind.startswith("temp_")]
            temporary = [entry for entry in entries if entry.kind.startswith("temp_")]
            canonical_aliases = {(entry.path.parent, entry.sha) for entry in live}
            if any((entry.path.parent, entry.sha) not in canonical_aliases for entry in temporary):
                raise OptimizationError("orphan_temporary")
            for entry in live:
                identity = entry.inode, entry.sha, entry.kind
                if identity not in checked:
                    sizes[entry.info.st_dev, entry.sha] = _verify(entry)
                    checked.add(identity)
                    report["sha_verified_files"] += 1
                report["sha_verified_aliases"] += 1
            if apply:
                # These reserved staging names can be partial writes. Remove
                # only after verifying a canonical replacement AND all links.
                for entry in temporary:
                    _remove(entry.path)
                    report["temporaries_removed"] += 1
                # Cleanup changes link counts, never validated bytes/identity.
                entries = [_fresh(entry) for entry in live]
                _check_links(entries)
            extra_links = Counter(_fresh(entry).inode for entry in temporary) if not apply else None
            groups = defaultdict(list)
            for entry in entries:
                if not entry.kind.startswith("temp_"):
                    groups[entry.info.st_dev, entry.sha].append(entry)
            for key, group in sorted(groups.items()):
                skip = _rewrite_group(group, sizes[key], safety_margin_bytes, report,
                                      restore_raw=restore_raw, dry_run=not apply,
                                      extra_links=extra_links)
                if skip:
                    report["groups_skipped"] += 1
                    report["skips"][skip] = report["skips"].get(skip, 0) + 1
                else:
                    report["groups_changed"] += 1
        except OptimizationError as error:
            report["status"] = "failed"
            report["failures"].append({"code": str(error)})
            report["failure_count"] += 1
        except OSError:
            report["status"] = "failed"
            report["failures"].append({"code": "io_error"})
            report["failure_count"] += 1
        except KeyboardInterrupt:
            report["status"] = "failed"
            report["failures"].append({"code": "interrupted"})
            report["failure_count"] += 1
        finally:
            if scanned:
                try:
                    report["after"] = _snapshot(_scan(roots))
                    report["tree_allocated_bytes_reduction"] = (
                        report["before"]["allocated_bytes"] - report["after"]["allocated_bytes"])
                except (OSError, OptimizationError):
                    report["after"] = None
                    report["tree_allocated_bytes_reduction"] = None
                    report["status"] = "failed"
                    report["failures"].append({"code": "post_scan_failed"})
                    report["failure_count"] += 1
            for observation in report["filesystems"]:
                try:
                    observation["free_after"] = shutil.disk_usage(
                        filesystem_paths[observation["device"]]).free
                    if observation["free_before"] is not None:
                        observation["free_delta"] = observation["free_after"] - observation["free_before"]
                except (OSError, KeyboardInterrupt) as error:
                    report["status"] = "failed"
                    report["failures"].append({
                        "code": "interrupted" if isinstance(error, KeyboardInterrupt) else "io_error"})
                    report["failure_count"] += 1
    return report
