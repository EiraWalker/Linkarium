"""Offline sharing of receipt-proven completed Linkarium backups only."""
from collections import Counter, defaultdict
from contextlib import ExitStack
import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

_SPEC = importlib.util.spec_from_file_location('_sharing_storage_upgrade', Path(__file__).with_name('storage_upgrade.py'))
assert _SPEC is not None and _SPEC.loader is not None
su = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(su)


def _inode(info):
    return info.st_dev, info.st_ino


def _allocated(entries):
    return sum(info.st_blocks * 512 for info in {_inode(e['info']): e['info'] for e in entries}.values())


def _roots(active, cold):
    roots = []
    for value in (active, cold):
        path = Path(value)
        if not path.is_absolute() or str(path) != os.fspath(value) or '..' in path.parts:
            raise su.UpgradeError('ROOT_NONCANONICAL')
        roots.append(su.safe(path))
    if (roots[0] == roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents
            or _inode(roots[0].stat()) == _inode(roots[1].stat())):
        raise su.UpgradeError('ROOT_OVERLAP')
    if roots[0].stat().st_dev != roots[1].stat().st_dev:
        raise su.UpgradeError('ROOT_CROSS_DEVICE')
    return roots


def _scan(roots, operator_lock):
    files = {operator_lock: su.regular(operator_lock).stat()}
    directories = {}
    for root in roots:
        paths, dirs = su.walk(root)
        su.require_quiescent_sqlite(root)
        for path in paths + dirs:
            if path != root:
                su.relative_name(path.relative_to(root).as_posix())
        for path in paths:
            info = path.stat()
            if path.name.endswith(('.sqlite3-wal', '.sqlite3-journal', '.sqlite3-shm')) and info.st_size:
                raise su.UpgradeError('UNSAFE_SQLITE_SIDECAR')
            files[path] = info
        directories.update({path: path.stat() for path in dirs})
        directories.update({path: su.safe(path).stat() for path in root.parents})
    counts = Counter(_inode(info) for info in files.values())
    if any(info.st_nlink != counts[_inode(info)] for info in files.values()):
        raise su.UpgradeError('EXTERNAL_HARDLINK')
    return files, directories


def _signature(info):
    return (_inode(info), info.st_size, info.st_mode, info.st_uid, info.st_gid,
            info.st_mtime_ns, info.st_ctime_ns)


def _kernel_locked(path):
    import fcntl
    fd = os.open(su.regular(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            # A separate SH request conflicts only with an exclusive lock.
            # An EX probe also conflicts with SH and could let the borrowed
            # descriptor confirmation below silently convert the caller's lock.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise su.UpgradeError('LOCK_IDENTITY_CHANGED')
    finally:
        os.close(fd)


def _acquire(roots, operator_lock, stack, operator_lock_fd):
    import fcntl
    paths = [operator_lock, *(root / 'runtime/supervisor.lock' for root in roots)]
    infos = {path: su.regular(path).stat() for path in paths}
    counts = Counter(_inode(info) for info in infos.values())
    if any(info.st_nlink != counts[_inode(info)] for info in infos.values()):
        raise su.UpgradeError('EXTERNAL_HARDLINK')
    if infos[operator_lock].st_nlink != 1:
        raise su.UpgradeError('OPERATOR_LOCK_ALIAS')
    owned = set()
    for path, info in infos.items():
        if _inode(info) in owned:
            continue
        if path == operator_lock and operator_lock_fd is not None:
            if _signature(os.fstat(operator_lock_fd)) != _signature(info):
                raise su.UpgradeError('LOCK_IDENTITY_CHANGED')
            _kernel_locked(path)  # It must already be held, not newly acquired here.
            try:
                fcntl.flock(operator_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise su.UpgradeError('WRITER_OR_OPERATOR_LOCKED') from None
            # Never close or unlock the caller's open-file description.
        else:
            stack.enter_context(su.file_lock(path))
        if _signature(su.regular(path).stat()) != _signature(info):
            raise su.UpgradeError('LOCK_IDENTITY_CHANGED')
        _kernel_locked(path)
        owned.add(_inode(info))
    for path, info in infos.items():
        if _signature(su.regular(path).stat()) != _signature(info):
            raise su.UpgradeError('LOCK_IDENTITY_CHANGED')
    return infos


def _check(roots, operator_lock, files, directories, changed_inodes=()):
    current, dirs = _scan(roots, operator_lock)
    # Also bind explicit external evidence directories and root ancestors.
    for path in directories:
        if not any(path.is_relative_to(root) for root in roots):
            dirs[path] = su.safe(path).stat()
    if set(current) != set(files) or set(dirs) != set(directories):
        raise su.UpgradeError('INPUT_CHANGED')
    for path, before in files.items():
        left, right = _signature(before), _signature(current[path])
        # link/rename changes ctime, never bytes or other metadata. Only those
        # exact bound inodes are allowed that change; content is rehashed below.
        if _inode(before) in changed_inodes:
            left, right = left[:-1], right[:-1]
        if left != right:
            raise su.UpgradeError('INPUT_CHANGED')
    for path, before in directories.items():
        after = dirs[path]
        if (_inode(before), before.st_mode, before.st_uid, before.st_gid) != (
                _inode(after), after.st_mode, after.st_uid, after.st_gid):
            raise su.UpgradeError('INPUT_CHANGED')
    files.update(current)


def _hash_bound(entry, files):
    path = entry['path']
    before = files[path]
    if _signature(su.regular(path).stat()) != _signature(before):
        raise su.UpgradeError('INPUT_CHANGED')
    sha = su.digest(path)
    if _signature(su.regular(path).stat()) != _signature(before):
        raise su.UpgradeError('INPUT_CHANGED')
    if sha != entry['sha'] or os.listxattr(path, follow_symlinks=False):
        raise su.UpgradeError('BACKUP_READBACK_MISMATCH')


def _stage(target):
    return target.with_name('.' + target.name + '.completed-share-v1')


def _sync_file(entry, files):
    fd = os.open(su.regular(entry['path']), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if _signature(os.fstat(fd)) != _signature(files[entry['path']]):
            raise su.UpgradeError('INPUT_CHANGED')
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(source, target, roots, operator_lock, files, directories):
    _check(roots, operator_lock, files, directories)
    _hash_bound(source, files)
    _hash_bound(target, files)
    stage = _stage(target['path'])
    _sync_file(source, files)
    with ExitStack() as handles:
        parents = []
        for path in (source['path'].parent, target['path'].parent):
            fd = os.open(su.safe(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.callback(os.close, fd)
            if _inode(os.fstat(fd)) != _inode(directories[path]):
                raise su.UpgradeError('INPUT_CHANGED')
            parents.append(fd)
        if stage not in files:
            os.link(source['path'].name, stage.name, src_dir_fd=parents[0], dst_dir_fd=parents[1], follow_symlinks=False)
            files[stage] = files[source['path']]
            _check(roots, operator_lock, files, directories, {_inode(files[source['path']])})
        _hash_bound(source, files)
        _hash_bound(target, files)
        _check(roots, operator_lock, files, directories)
        changed = {_inode(files[source['path']]), _inode(files[target['path']])}
        if _inode(files[source['path']]) == _inode(files[target['path']]):
            # rename(a,b) is a no-op when a and b are already hardlink aliases.
            os.unlink(stage.name, dir_fd=parents[1])
            files.pop(stage)
        else:
            os.replace(stage.name, target['path'].name, src_dir_fd=parents[1], dst_dir_fd=parents[1])
            files[target['path']] = files.pop(stage)
        _check(roots, operator_lock, files, directories, changed)
        su.sync_directory(target['path'].parent)
        _hash_bound(target, files)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _metadata(info):
    return {'dev': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
            'mtime_ns': info.st_mtime_ns, 'nlink': info.st_nlink,
            'allocated_bytes': info.st_blocks * 512}


def _read_receipt(path):
    info = su.regular(path).stat()
    if (info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid() or info.st_size > 4 * 1024**2):
        raise su.UpgradeError('UNSAFE_SHARING_RECEIPT')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        if _signature(os.fstat(stream.fileno())) != _signature(info):
            raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
        content = stream.read()
    if _signature(su.regular(path).stat()) != _signature(info):
        raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
    return content


def _save_once(path, content):
    # Never overwrite/delete evidence, even when fsync fails. Complete matching
    # bytes may be re-fsynced on retry; partial or unrelated bytes fail closed.
    if not path.exists():
        fd = os.open(su.safe(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    if _read_receipt(path) != content:
        raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    for directory in (path.parent, *path.parent.parents):
        su.sync_directory(directory)
    if _read_receipt(path) != content:
        raise su.UpgradeError('RECEIPT_INPUT_CHANGED')


def _completion(intent):
    return {'ok': True, 'intent_sha256': _sha(_json(intent)),
            'completed_paths': len(intent['entries']), 'independent_backup_copies': False,
            'after': [entry['after'] for entry in intent['entries']]}


def _intent(roots, groups, receipt_dir, legacy_sha):
    legacy_sha = legacy_sha or _sha(b'')
    value = {'version': 1, 'roots': [
        {'path_sha256': _sha(str(root).encode()), 'dev': root.stat().st_dev, 'inode': root.stat().st_ino,
         'database_sha256': su.digest(root / 'state.sqlite3')} for root in roots],
        'legacy_manifest_sha256': legacy_sha, 'entries': []}
    for group in groups.values():
        after = {**_metadata(group[0]['info']), 'nlink': len(group)}
        for entry in group:
            index = 0 if entry['path'].is_relative_to(roots[0]) else 1
            value['entries'].append({'root_index': index,
                'relative_name_sha256': _sha(entry['path'].relative_to(roots[index]).as_posix().encode()),
                'sha256': entry['sha'], 'before': _metadata(entry['info']), 'after': after})
    if receipt_dir is not None:
        receipt_dir = su.safe(receipt_dir)
        info = receipt_dir.stat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.geteuid() or any(receipt_dir == r or receipt_dir.is_relative_to(r) for r in roots)
                or {p.name for p in receipt_dir.iterdir()} - {'intent.json', 'complete.json'}):
            raise su.UpgradeError('UNSAFE_SHARING_RECEIPT')
        for path in receipt_dir.iterdir():
            _read_receipt(path)  # Check even dangling symlinks, before any write.
        previous = receipt_dir / 'intent.json'
        if previous.exists():
            saved = json.loads(_read_receipt(previous))
            if (set(saved) != set(value) or saved['version'] != value['version']
                    or saved['roots'] != value['roots'] or saved['legacy_manifest_sha256'] != legacy_sha
                    or len(saved['entries']) != len(value['entries'])):
                raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
            for old, current in zip(saved['entries'], value['entries']):
                for key in ('root_index', 'relative_name_sha256', 'sha256', 'after'):
                    if old[key] != current[key]:
                        raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
                # Link counts include a legitimate abandoned stage, but the
                # complete inventory independently proves every alias.
                comparable = lambda item: {k: v for k, v in item.items() if k != 'nlink'}
                if comparable(current['before']) not in (comparable(old['before']), comparable(old['after'])):
                    raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
            value = saved
        elif (receipt_dir / 'complete.json').exists():
            raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
        if (receipt_dir / 'complete.json').exists() and _read_receipt(receipt_dir / 'complete.json') != _json(_completion(value)):
            raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
    return value


def _legacy(roots, entries, intent, expected_sha):
    path = su.safe(roots[1].parent / 'data-manifest.json')
    if not path.exists():
        if expected_sha is not None:
            raise su.UpgradeError('LEGACY_MANIFEST_MISSING')
        return None
    if expected_sha is None:
        raise su.UpgradeError('LEGACY_MANIFEST_ACK_REQUIRED')
    before = su.regular(path).stat()
    if su.digest(path) != expected_sha:
        raise su.UpgradeError('LEGACY_MANIFEST_HASH_MISMATCH')
    manifest = json.loads(path.read_bytes())
    originals = {e['relative_name_sha256']: e for e in intent['entries'] if e['root_index'] == 1}
    for entry in entries:
        if not entry['path'].is_relative_to(roots[1]):
            continue
        name = entry['path'].relative_to(roots[1]).as_posix()
        record = manifest.get('files', {}).get(name, {})
        original = originals[_sha(name.encode())]['before']
        if (record.get('kind') != 'independent_copy' or record.get('sha256') != entry['sha']
                or any(record.get(k) != original[k] for k in ('dev', 'inode', 'size', 'mode', 'uid', 'gid', 'mtime_ns'))):
            raise su.UpgradeError('LEGACY_MANIFEST_ENTRY_MISMATCH')
    if _signature(su.regular(path).stat()) != _signature(before):
        raise su.UpgradeError('LEGACY_MANIFEST_CHANGED')
    return path, before, expected_sha


def _legacy_unchanged(binding):
    if binding is not None:
        path, info, sha = binding
        if _signature(su.regular(path).stat()) != _signature(info) or su.digest(path) != sha:
            raise su.UpgradeError('LEGACY_MANIFEST_CHANGED')


def _completed(root):
    # Keep the original helper's classification, but do not normalize malformed
    # raw spellings before that helper examines its PurePosixPath representation.
    with su.database(root / 'state.sqlite3', immutable=True) as db:
        for kind, raw in db.execute("SELECT kind,result FROM runtime_maintenanceoperation WHERE state='succeeded' AND kind IN ('backup','restore')"):
            receipt = json.loads(raw)
            if kind == 'backup':
                path = receipt.get('package_path')
                if not isinstance(path, str) or str(su.PurePosixPath(path)) != path:
                    raise su.UpgradeError('BACKUP_COMPLETION_RECEIPT_INVALID')
    return su.completed_backups(root, immutable=True)


def _compatible(group):
    metadata = {(e['info'].st_mode, e['info'].st_uid, e['info'].st_gid, e['info'].st_mtime_ns) for e in group}
    if len(metadata) != 1 or any(os.listxattr(e['path'], follow_symlinks=False) for e in group):
        raise su.UpgradeError('BACKUP_METADATA_INCOMPATIBLE')


def share(active, cold, *, operator_lock, offline=False, apply=False, operator_lock_fd=None,
          receipt_dir=None, legacy_manifest_sha256=None):
    if offline is not True:
        raise su.UpgradeError('OFFLINE_REQUIRED')
    if sys.platform != 'linux':
        raise su.UpgradeError('LINUX_REQUIRED')
    if apply and receipt_dir is None:
        raise su.UpgradeError('RECEIPT_REQUIRED')
    if type(apply) is not bool or (legacy_manifest_sha256 is not None
            and not su.re.fullmatch('[0-9a-f]{64}', legacy_manifest_sha256)):
        raise su.UpgradeError('ARGUMENT_INVALID')
    for value in (operator_lock, receipt_dir):
        if value is not None and (not Path(value).is_absolute() or str(Path(value)) != os.fspath(value)
                                  or '..' in Path(value).parts):
            raise su.UpgradeError('ARGUMENT_INVALID')
    roots = _roots(active, cold)
    operator_lock = su.safe(operator_lock)
    if operator_lock != roots[0].parent / 'ops/v012-operator.lock':
        raise su.UpgradeError('OPERATOR_LOCK_PATH_INVALID')
    with ExitStack() as locks:
        lock_infos = _acquire(roots, operator_lock, locks, operator_lock_fd)
        files, directories = _scan(roots, operator_lock)
        for path, info in lock_infos.items():
            if _signature(files[path]) != _signature(info):
                raise su.UpgradeError('LOCK_IDENTITY_CHANGED')
        entries = []
        names = defaultdict(list)
        for root in roots:
            for name, receipt in sorted(_completed(root).items()):
                path = su.regular(root / name)
                info = path.stat()
                if 'size' in receipt and (type(receipt['size']) is not int or receipt['size'] != info.st_size):
                    raise su.UpgradeError('BACKUP_RECEIPT_SIZE_MISMATCH')
                entry = {'path': path, 'info': info, 'receipt': receipt}
                entries.append(entry)
                names[name].append(entry)
        for group in names.values():
            _compatible(group)
        _check(roots, operator_lock, files, directories)
        for entry in entries:
            entry['sha'] = su.digest(entry['path'])
            if entry['receipt'].get('sha256', entry['sha']) != entry['sha']:
                raise su.UpgradeError('BACKUP_RECEIPT_SHA_MISMATCH')
        groups = defaultdict(list)
        for entry in entries:
            groups[entry['info'].st_dev, entry['info'].st_size, entry['sha']].append(entry)
        for group in groups.values():
            _compatible(group)
        stages = {_stage(target['path']): group[0] for group in groups.values() for target in group[1:]}
        for path, info in files.items():
            if path.name.endswith('.completed-share-v1'):
                if path not in stages or _signature(info) != _signature(stages[path]['info']):
                    raise su.UpgradeError('UNSAFE_SHARING_STAGE')
        completed = {e['path'] for e in entries}
        backup_inodes = {_inode(e['info']) for e in entries}
        if any(path not in completed and path not in stages and _inode(info) in backup_inodes for path, info in files.items()):
            raise su.UpgradeError('UNAUTHORIZED_BACKUP_ALIAS')
        _check(roots, operator_lock, files, directories)
        before = _allocated(entries)
        if receipt_dir is not None:
            receipt_dir = su.safe(receipt_dir)
            directories[receipt_dir] = receipt_dir.stat()
        intent = _intent(roots, groups, receipt_dir, legacy_manifest_sha256)
        _check(roots, operator_lock, files, directories)
        legacy = _legacy(roots, entries, intent, legacy_manifest_sha256)
        free_before = shutil.disk_usage(roots[0]).free
        if free_before < su.RESERVE_BYTES + 64 * 1024:
            raise su.UpgradeError('RESERVE_INSUFFICIENT')
        replacements = 0
        if apply:
            if receipt_dir is not None:
                _save_once(Path(receipt_dir) / 'intent.json', _json(intent))
            for group in groups.values():
                if _read_receipt(Path(receipt_dir) / 'intent.json') != _json(intent):
                    raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
                if shutil.disk_usage(roots[0]).free < su.RESERVE_BYTES + 64 * 1024:
                    raise su.UpgradeError('RESERVE_INSUFFICIENT')
                _legacy_unchanged(legacy)
                source = group[0]
                _sync_file(source, files)
                for target in group[1:]:
                    if _inode(files[target['path']]) == _inode(files[source['path']]) and _stage(target['path']) not in files:
                        su.sync_directory(target['path'].parent)
                        _hash_bound(target, files)
                        continue
                    differs = _inode(files[target['path']]) != _inode(files[source['path']])
                    _publish(source, target, roots, operator_lock, files, directories)
                    replacements += int(differs)
        _check(roots, operator_lock, files, directories)
        for entry in entries:
            _hash_bound(entry, files)
            entry['info'] = files[entry['path']]
        _check(roots, operator_lock, files, directories)
        after = _allocated(entries)
        _legacy_unchanged(legacy)
        free_after = shutil.disk_usage(roots[0]).free
        if free_after < su.RESERVE_BYTES + 64 * 1024:
            raise su.UpgradeError('RESERVE_INSUFFICIENT')
        if apply and receipt_dir is not None:
            if _read_receipt(Path(receipt_dir) / 'intent.json') != _json(intent):
                raise su.UpgradeError('RECEIPT_INPUT_CHANGED')
            _save_once(Path(receipt_dir) / 'complete.json', _json(_completion(intent)))
            _check(roots, operator_lock, files, directories)
            free_after = shutil.disk_usage(roots[0]).free
            if free_after < su.RESERVE_BYTES:
                raise su.UpgradeError('RESERVE_INSUFFICIENT')
        cold_now = {_sha(e['path'].relative_to(roots[1]).as_posix().encode()): _inode(e['info'])
                    for e in entries if e['path'].is_relative_to(roots[1])}
        superseded = bool(legacy) and any(
            cold_now[row['relative_name_sha256']] != (row['before']['dev'], row['before']['inode'])
            for row in intent['entries'] if row['root_index'] == 1)
        return {'ok': True, 'applied': apply, 'completed_paths': len(entries), 'groups': len(groups),
                'replaced_paths': replacements, 'logical_bytes': sum(e['info'].st_size for e in entries),
                'unique_allocated_before_bytes': before, 'unique_allocated_after_bytes': after,
                'tree_allocation_reduction_bytes': before - after,
                'filesystem_free_before_bytes': free_before,
                'filesystem_free_after_bytes': free_after,
                'filesystem_free_delta_bytes': free_after - free_before,
                'reserve_bytes': su.RESERVE_BYTES,
                'legacy_physical_independence_superseded': superseded,
                'legacy_manifest_sha256': legacy_manifest_sha256 or _sha(b''),
                'intent_sha256': _sha(_json(intent))}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise su.UpgradeError('ARGUMENT_INVALID')


def main(argv=None):
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument('--active-root', required=True)
        parser.add_argument('--cold-root', required=True)
        parser.add_argument('--operator-lock', required=True)
        parser.add_argument('--operator-lock-fd', type=int)
        parser.add_argument('--offline', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--receipt-dir')
        parser.add_argument('--legacy-manifest-sha256')
        args = parser.parse_args(argv)
        report = share(args.active_root, args.cold_root, operator_lock=args.operator_lock,
            offline=args.offline, apply=args.apply, operator_lock_fd=args.operator_lock_fd,
            receipt_dir=args.receipt_dir, legacy_manifest_sha256=args.legacy_manifest_sha256)
    except BaseException as error:
        code = str(error) if isinstance(error, su.UpgradeError) and su.re.fullmatch('[A-Z0-9_]+', str(error)) else (
            'IO_ERROR' if isinstance(error, OSError) else 'INTERRUPTED' if isinstance(error, KeyboardInterrupt) else 'INPUT_OR_INTERNAL_ERROR')
        report = {'ok': False, 'code': code}
    try:
        print(json.dumps(report, sort_keys=True), flush=True)
    except OSError:
        return 1
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
