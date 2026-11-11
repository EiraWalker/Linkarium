"""Root-only, offline Linkarium code upgrade. Never migrates or restores data."""
from pathlib import Path
import os
import stat
import re
import hashlib
import json
import shutil
import tarfile
import sqlite3
import uuid
import subprocess
import urllib.request
import argparse
import signal
from contextlib import contextmanager
from pathlib import PurePosixPath

BASE = Path('/srv/linkarium')


class UpgradeError(RuntimeError):
    """Only constant, privacy-safe reason codes cross the console boundary."""


def safe(path, root=None):
    path = Path(path)
    if '..' in path.parts:
        raise UpgradeError('PATH_TRAVERSAL')
    path = path.absolute()
    if root is not None and not path.is_relative_to(Path(root).absolute()):
        raise UpgradeError('PATH_OUTSIDE_SCOPE')
    for item in (*reversed(path.parents), path):
        try:
            attributes = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, 'st_reparse_tag', 0):
            raise UpgradeError('PATH_LINK_FORBIDDEN')
        if not (stat.S_ISDIR(attributes.st_mode) or stat.S_ISREG(attributes.st_mode)):
            raise UpgradeError('PATH_SPECIAL_FORBIDDEN')
    return path


def validate_arguments(version, commit, expected_base=None):
    if (not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*', version)
            or len(version) > 100 or not re.fullmatch('[0-9a-f]{40}', commit)
            or (expected_base is not None and not re.fullmatch('[0-9a-f]{40}', expected_base))):
        raise UpgradeError('ARGUMENT_INVALID')


def updated_env(original, version, commit):
    """Change value bytes only; retain encoding, quotes, comments and newlines."""
    validate_arguments(version, commit)
    for key, value in ((b'LINKARIUM_VERSION', version), (b'LINKARIUM_REVISION', commit)):
        assignments = re.findall(rb'(?m)^[ \t]*(?:export[ \t]+)?' + key + rb'[ \t]*=', original)
        pattern = (rb'(?m)^([ \t]*(?:export[ \t]+)?' + key +
                   rb'[ \t]*=[ \t]*)(\"[^\"\r\n]*\"|\'[^\'\r\n]*\'|[^\s#\"\']+)([ \t]*(?:#[^\r\n]*)?)(?=\r?$)')
        matches = list(re.finditer(pattern, original))
        if len(assignments) != 1 or len(matches) != 1:
            raise UpgradeError('ENV_DEPLOYMENT_KEYS_INVALID')
        match = matches[0]
        quoted = match[2][:1] in (b'\"', b"'")
        replacement = value.encode('ascii')
        if quoted:
            replacement = match[2][:1] + replacement + match[2][-1:]
        original = original[:match.start(2)] + replacement + original[match.end(2):]
    return original


RESERVE_BYTES = 512 * 1024**2


def regular(path):
    path = safe(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise UpgradeError('PATH_NOT_REGULAR')
    return path


def digest(path):
    path = regular(path)
    before = path.stat()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise UpgradeError('HASH_SOURCE_CHANGED')
        return hashlib.file_digest(source, 'sha256').hexdigest()


def walk(root):
    root = safe(root)
    files, directories = [], [root]
    if not root.is_dir():
        raise UpgradeError('PATH_NOT_DIRECTORY')
    def failed(error):
        # A partial tree is not a valid release, checkpoint or safety inventory.
        raise error
    for current, dirs, names in os.walk(root, followlinks=False, onerror=failed):
        for name in dirs + names:
            path = safe(Path(current) / name, root)
            (directories if path.is_dir() else files).append(path)
    return sorted(files), sorted(directories, key=lambda p: (len(p.parts), str(p)))


def relative_name(name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or str(path) != name or any(p in ('', '.', '..') for p in name.split('/'))
            or any(ord(c) < 32 for c in name)):
        raise UpgradeError('RELATIVE_PATH_UNSAFE')
    return path


def verify_release(root, version, commit):
    validate_arguments(version, commit)
    root = safe(root)
    metadata = json.loads(regular(root / 'release.json').read_bytes())
    if (metadata.get('application') != 'Linkarium' or metadata.get('version') != version
            or metadata.get('commit') != commit or not re.fullmatch('[0-9a-f]{40}', metadata.get('git_tree', ''))
            or metadata.get('prebuilt_frontend') is not True or metadata.get('includes_personal_data') is not False):
        raise UpgradeError('RELEASE_METADATA_MISMATCH')
    listed = set()
    for line in regular(root / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match:
            raise UpgradeError('RELEASE_MANIFEST_INVALID')
        name = str(relative_name(match[2]))
        if name in listed or name == 'SHA256SUMS':
            raise UpgradeError('RELEASE_MANIFEST_DUPLICATE')
        if digest(root / name) != match[1]:
            raise UpgradeError('RELEASE_FILE_HASH_MISMATCH')
        listed.add(name)
    files, _ = walk(root)
    if listed != {p.relative_to(root).as_posix() for p in files} - {'SHA256SUMS'}:
        raise UpgradeError('RELEASE_FILE_SET_MISMATCH')
    for required in ('backend/supervisor.py', 'frontend/dist/index.html', 'LICENSE',
                     'deploy/cloud/compose.yaml', 'deploy/cloud/Dockerfile', 'deploy/requirements.lock'):
        if required not in listed:
            raise UpgradeError('RELEASE_REQUIRED_FILE_MISSING')
    return metadata


def extract_package(package, expected_sha, destination, version, commit):
    validate_arguments(version, commit)
    if not re.fullmatch('[0-9a-f]{64}', expected_sha) or digest(package) != expected_sha:
        raise UpgradeError('PACKAGE_HASH_MISMATCH')
    destination = safe(destination)
    name = 'Linkarium-' + version + '-deploy'
    # Validate every member before creating anything. Do not use extractall:
    # only plain directories/files are allowed, never archive owners or links.
    with tarfile.open(regular(package), 'r:gz') as archive:
        members, names, expanded = [], set(), 0
        for member in archive:
            try:
                relative = relative_name(member.name)
            except UpgradeError:
                raise UpgradeError('PACKAGE_UNSAFE_ENTRY') from None
            if (relative.parts[0] != name or not (member.isdir() or member.isfile())
                    or member.name in names or member.size < 0
                    or (len(relative.parts) == 1 and not member.isdir())):
                raise UpgradeError('PACKAGE_UNSAFE_ENTRY')
            names.add(member.name)
            members.append(member)
            expanded += member.size
            if len(members) > 10000 or expanded > 1024**3:
                raise UpgradeError('PACKAGE_EXPANSION_LIMIT')
        kinds = {m.name: m.isdir() for m in members}
        for member in members:
            for parent in PurePosixPath(member.name).parents:
                if str(parent) in kinds and not kinds[str(parent)]:
                    raise UpgradeError('PACKAGE_UNSAFE_ENTRY')
        if shutil.disk_usage(destination).free < expanded * 3 + RESERVE_BYTES:
            raise UpgradeError('PREPARE_SPACE_INSUFFICIENT')
        if not members or (destination / name).exists():
            raise UpgradeError('PACKAGE_DESTINATION_EXISTS_OR_EMPTY')
        for member in members:
            target = safe(destination / member.name, destination)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if member.isdir():
                target.mkdir(exist_ok=True, mode=0o755)
                os.chmod(target, 0o755)
            else:
                incoming = archive.extractfile(member)
                if incoming is None:
                    raise UpgradeError('PACKAGE_FILE_MISSING')
                with incoming, target.open('xb') as output:
                    shutil.copyfileobj(incoming, output)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                if target.stat().st_size != member.size:
                    raise UpgradeError('PACKAGE_FILE_SIZE_MISMATCH')
    extracted = destination / name
    for directory in walk(extracted)[1]:
        os.chmod(directory, 0o755)  # Include implicit tar parents under umask 077.
    verify_release(extracted, version, commit)
    if digest(package) != expected_sha:
        raise UpgradeError('PACKAGE_CHANGED_DURING_EXTRACTION')
    return extracted


ARTIFACT = re.compile(r'generations/[A-Za-z0-9_-]{1,80}/artifacts/([0-9a-f]{2})/([0-9a-f]{64})\.html(?:\.gz)?\Z')
BACKUP = re.compile(r'backups/[A-Za-z0-9][A-Za-z0-9_.-]*\.bookmarklib\.zip\Z')


@contextmanager
def database(path, immutable=False):
    connection = sqlite3.connect(regular(path).as_uri() + '?mode=ro' + ('&immutable=1' if immutable else ''), uri=True, timeout=10)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.rollback()
        connection.close()


def completed_backups(data, immutable=False):
    result = {}
    with database(data / 'state.sqlite3', immutable) as connection:
        for kind, raw in connection.execute("SELECT kind,result FROM runtime_maintenanceoperation WHERE state='succeeded' AND kind IN ('backup','restore')"):
            receipt = json.loads(raw)
            if kind == 'backup':
                path = PurePosixPath(receipt.get('package_path', ''))
                name = 'backups/' + path.name
                if (str(path) not in ('/data/' + name, (data / name).as_posix())
                        or not BACKUP.fullmatch(name) or not isinstance(receipt.get('byte_size'), int)
                        or not re.fullmatch('[0-9a-f]{64}', receipt.get('sha256', ''))):
                    raise UpgradeError('BACKUP_COMPLETION_RECEIPT_INVALID')
                value = {'size': receipt['byte_size'], 'sha256': receipt['sha256']}
            else:
                basename = receipt.get('pre_restore_backup', '')
                if not basename:
                    continue
                name = 'backups/' + basename
                if '/' in basename or not BACKUP.fullmatch(name):
                    raise UpgradeError('BACKUP_COMPLETION_RECEIPT_INVALID')
                value = {}  # A succeeded restore proves its pre-restore archive completed.
            if name in result and result[name] != value:
                raise UpgradeError('BACKUP_COMPLETION_RECEIPT_CONFLICT')
            result[name] = value
    return result


def stamp(path):
    info = regular(path).stat()
    return {'size': info.st_size, 'dev': info.st_dev, 'inode': info.st_ino, 'mtime_ns': info.st_mtime_ns}


def plan_data(data, journal, cold=False):
    data, journal = safe(data), safe(journal)
    if cold:
        require_quiescent_sqlite(data)
    files, directories = walk(data)
    receipts, entries, copied = completed_backups(data, cold), {}, 0
    device = journal.stat().st_dev
    for source in files:
        name = source.relative_to(data).as_posix()
        attributes = stamp(source)
        match = ARTIFACT.fullmatch(name)
        artifact = bool(match and match[1] == match[2][:2])
        kind = 'artifact_link' if artifact else 'backup_link' if name in receipts else 'copy'
        if kind != 'copy' and attributes['dev'] != device:
            raise UpgradeError('HARDLINK_CROSS_DEVICE')
        if name in receipts and 'size' in receipts[name] and attributes['size'] != receipts[name]['size']:
            raise UpgradeError('BACKUP_RECEIPT_SIZE_MISMATCH')
        if kind == 'copy':
            copied += attributes['size']
        entries[name] = {**attributes, 'kind': kind, 'receipt': receipts.get(name)}
    overhead = (len(files) + len(directories)) * 4096 + 16 * 1024**2
    return {'entries': entries, 'directories': [p.relative_to(data).as_posix() for p in directories],
            'copy_bytes': copied, 'overhead_bytes': overhead, 'reserve_bytes': RESERVE_BYTES,
            'free_bytes': shutil.disk_usage(journal).free}


def require_space(plan):
    if plan['free_bytes'] < plan['copy_bytes'] + plan['overhead_bytes'] + plan['reserve_bytes']:
        raise UpgradeError('COLD_SNAPSHOT_SPACE_INSUFFICIENT')


def sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, content):
    path = safe(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path, value):
    atomic_bytes(path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())


def preserve_attributes(source, target):
    info = source.stat()
    # Never call this on hardlinks: chmod/chown would mutate live shared inodes.
    if os.name == 'posix':
        os.chown(target, info.st_uid, info.st_gid)
    shutil.copystat(source, target, follow_symlinks=False)


def copy_verified(source, target):
    before = stamp(source)
    checksum = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(source, flags), 'rb') as incoming, target.open('xb') as output:
        current = os.fstat(incoming.fileno())
        if (current.st_dev, current.st_ino) != (before['dev'], before['inode']):
            raise UpgradeError('COLD_SOURCE_CHANGED')
        for chunk in iter(lambda: incoming.read(2 * 1024**2), b''):
            checksum.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    preserve_attributes(source, target)
    if stamp(source) != before or target.stat().st_size != before['size'] or digest(target) != checksum.hexdigest():
        raise UpgradeError('COLD_COPY_HASH_OR_SOURCE_CHANGED')
    if (target.stat().st_dev, target.stat().st_ino) == (before['dev'], before['inode']):
        raise UpgradeError('COLD_COPY_NOT_INDEPENDENT')
    return checksum.hexdigest()


def cold_snapshot(data, journal):
    """Caller must stop writers and hold supervisor.lock throughout this call."""
    plan = plan_data(data, journal, cold=True)
    require_space(plan)
    destination = safe(journal / 'data', journal)
    if destination.exists():
        raise UpgradeError('COLD_DESTINATION_EXISTS')
    for name in plan['directories']:
        (destination / name).mkdir(mode=0o700)
    manifest = {'version': 1, 'files': {}, 'copied_bytes': 0, 'linked_bytes': 0,
                'independent_disaster_backup': False, 'source': str(data),
                'migration_additional_data_root': str(destination)}
    for name, entry in plan['entries'].items():
        source, target = safe(data / name, data), safe(destination / name, destination)
        if stamp(source) != {k: entry[k] for k in ('size', 'dev', 'inode', 'mtime_ns')}:
            raise UpgradeError('COLD_SOURCE_CHANGED')
        if entry['kind'] == 'copy':
            checksum = copy_verified(source, target)
            manifest['copied_bytes'] += entry['size']
        else:
            os.link(source, target, follow_symlinks=False)
            if stamp(target) != stamp(source):
                raise UpgradeError('COLD_LINK_INODE_MISMATCH')
            checksum = digest(target) if entry['kind'] == 'backup_link' else None
            if entry['receipt'] and checksum != entry['receipt'].get('sha256', checksum):
                raise UpgradeError('COLD_BACKUP_SHA_MISMATCH')
            manifest['linked_bytes'] += entry['size']
        if stamp(source) != {k: entry[k] for k in ('size', 'dev', 'inode', 'mtime_ns')}:
            raise UpgradeError('COLD_SOURCE_CHANGED')
        manifest['files'][name] = {**stamp(target), 'kind': entry['kind'], 'sha256': checksum}
    if {p.relative_to(data).as_posix() for p in walk(data)[0]} != set(plan['entries']):
        raise UpgradeError('COLD_FILE_SET_CHANGED')
    for name in reversed(plan['directories']):
        preserve_attributes(data / name, destination / name)
        sync_directory(destination / name)
    write_json(journal / 'data-manifest.json', manifest)
    atomic_bytes(journal / 'data-manifest.sha256', (digest(journal / 'data-manifest.json') + '\n').encode())
    verify_cold(journal)
    return manifest


def verify_cold(journal):
    path = safe(journal / 'data-manifest.json', journal)
    if digest(path) != regular(journal / 'data-manifest.sha256').read_text().strip():
        raise UpgradeError('COLD_MANIFEST_HASH_MISMATCH')
    manifest = json.loads(path.read_bytes())
    root = safe(journal / 'data', journal)
    if {p.relative_to(root).as_posix() for p in walk(root)[0]} != set(manifest['files']):
        raise UpgradeError('COLD_FILE_SET_CHANGED')
    for name, entry in manifest['files'].items():
        relative_name(name)
        path = regular(root / name)
        current = stamp(path)
        if current['size'] != entry['size']:
            raise UpgradeError('COLD_FILE_SIZE_CHANGED')
        if entry['kind'] in ('artifact_link', 'backup_link'):
            if current != {k: entry[k] for k in current}:
                raise UpgradeError('COLD_LINK_CHANGED')
        elif entry['kind'] != 'copy':
            raise UpgradeError('COLD_MANIFEST_KIND_INVALID')
        if entry['kind'] != 'artifact_link' and digest(path) != entry['sha256']:
            raise UpgradeError('COLD_FILE_HASH_CHANGED')
    return manifest


IDENTITY_TABLES = ('auth_user', 'runtime_authstate', 'runtime_passkeycredential',
                   'runtime_filekeycredential', 'runtime_providersecret',
                   'runtime_executiondevice', 'django_session')


def row_summary(rows):
    checksum, count = hashlib.sha256(), 0
    for row in rows:
        values = [(type(value).__name__, value.hex() if isinstance(value, bytes) else value) for value in row]
        checksum.update(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode() + b'\n')
        count += 1
    return {'count': count, 'sha256': checksum.hexdigest()}


def identity(data, immutable=False):
    result = {'tables': {}, 'files': {}}
    with database(data / 'state.sqlite3', immutable) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in IDENTITY_TABLES:
            if table not in names:
                raise UpgradeError('IDENTITY_TABLE_MISSING')
            columns = [row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')]
            order = ','.join('"' + c.replace('"', '""') + '"' for c in columns)
            result['tables'][table] = row_summary(connection.execute('SELECT * FROM "' + table + '" ORDER BY ' + order))
    for name in ('runtime/secret-key', 'runtime/login-key.pem'):
        path = safe(data / name, data)
        result['files'][name] = {'size': path.stat().st_size, 'sha256': digest(path)} if path.exists() else None
    if result['files']['runtime/secret-key'] is None:
        raise UpgradeError('IDENTITY_SECRET_KEY_MISSING')
    return result


def require_quiescent_sqlite(data):
    # Immutable reads must never ignore an uncheckpointed WAL. Leave it intact
    # for manual recovery; do not run a checkpoint/migration behind the operator.
    for path in walk(data)[0]:
        if path.name.endswith(('.sqlite3-wal', '.sqlite3-journal')) and path.stat().st_size:
            raise UpgradeError('COLD_SQLITE_WAL_OR_JOURNAL_REQUIRES_REVIEW')


def rollback_guard(data, immutable=True):
    """No business-row hashes: rollback must keep the latest business/auth DBs."""
    if immutable:
        require_quiescent_sqlite(data)
    result = {'schemas': {}, 'gzip_representations': {}}
    for path in walk(data)[0]:
        name = path.relative_to(data).as_posix()
        if name == 'state.sqlite3' or re.fullmatch(r'generations/[A-Za-z0-9_-]{1,80}/library\.sqlite3', name):
            with database(path, immutable=immutable) as connection:
                rows = list(connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
                if any(row[1] == 'django_migrations' for row in rows):
                    rows += list(connection.execute('SELECT app,name FROM django_migrations ORDER BY app,name'))
                rows.append(('user_version', connection.execute('PRAGMA user_version').fetchone()[0]))
                result['schemas'][name] = row_summary(rows)
        if ARTIFACT.fullmatch(name) and name.endswith('.gz'):
            result['gzip_representations'][name] = stamp(path)
    if 'state.sqlite3' not in result['schemas']:
        raise UpgradeError('ROLLBACK_STATE_DATABASE_MISSING')
    return result


def current_release(expected_base=None):
    safe(BASE)
    current = BASE / 'current'
    if not current.is_symlink():
        raise UpgradeError('CURRENT_NOT_SYMLINK')
    target = current.readlink()
    if not target.is_absolute():
        target = BASE / target
    target = safe(target, BASE / 'releases')
    if target.parent != BASE / 'releases':
        raise UpgradeError('CURRENT_PATH_INVALID')
    metadata = json.loads(regular(target / 'release.json').read_bytes())
    if expected_base is not None and metadata.get('commit') != expected_base:
        raise UpgradeError('CURRENT_BASE_CHANGED')
    verify_release(target, metadata['version'], metadata['commit'])
    return target, metadata


def journal_path(args):
    validate_arguments(args.version, args.commit, args.expected_base)
    return safe(BASE / 'ops' / ('storage-upgrade-' + args.version + '-' + args.commit), BASE / 'ops')


def docker(arguments, *, cwd=None, input=None, timeout=600, deployment=None):
    # Never inherit COMPOSE_*, DOCKER_CONTEXT, LINKARIUM_* or credentials from
    # the caller. No subprocess stderr/stdout or env values reach the console.
    environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   'HOME': '/root', 'LANG': 'C.UTF-8', 'DOCKER_BUILDKIT': '0'}
    if deployment is not None:
        # Resolve the prospective Compose config without writing another env
        # file or inheriting arbitrary caller variables. Only these may change.
        validate_arguments(deployment['version'], deployment['commit'])
        environment.update(LINKARIUM_VERSION=deployment['version'], LINKARIUM_REVISION=deployment['commit'])
    try:
        result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *arguments],
            cwd=cwd, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=environment, check=False)
    except (OSError, subprocess.SubprocessError):
        raise UpgradeError('DOCKER_EXECUTION_FAILED') from None
    if result.returncode:
        raise UpgradeError('DOCKER_COMMAND_FAILED')
    return result.stdout


def image_info(reference, version, commit):
    fmt = '{"id":{{json .Id}},"labels":{{json .Config.Labels}},"user":{{json .Config.User}}}'
    value = json.loads(docker(['image', 'inspect', '--format', fmt, reference]))
    labels = value.get('labels') or {}
    if (not re.fullmatch(r'sha256:[0-9a-f]{64}', value.get('id', ''))
            or labels.get('org.opencontainers.image.revision') != commit
            or labels.get('org.opencontainers.image.version') != version
            or value.get('user') != '10001:10001'):
        raise UpgradeError('IMAGE_REVISION_OR_USER_MISMATCH')
    return value['id']


def container_info():
    fmt = ('{"id":{{json .Id}},"image_id":{{json .Image}},"running":{{json .State.Running}},'
           '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
           '"env":{{json .Config.Env}},"ports":{{json .NetworkSettings.Ports}},'
           '"host_ports":{{json .HostConfig.PortBindings}},'
           '"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}')
    return json.loads(docker(['container', 'inspect', '--format', fmt, 'linkarium-app-1']))


def require_container(release, metadata, image_id=None, overlay=None, healthy=True):
    value = container_info()
    labels = value.get('labels') or {}
    expected_configs = [str(release / 'deploy/cloud/compose.yaml')]
    if overlay is not None:
        expected_configs.append(str(overlay))
    if (labels.get('com.docker.compose.project') != 'linkarium'
            or labels.get('com.docker.compose.service') != 'app'
            or labels.get('com.docker.compose.project.config_files') != ','.join(expected_configs)
            or labels.get('org.opencontainers.image.revision') != metadata['commit']
            or labels.get('org.opencontainers.image.version') != metadata['version']):
        raise UpgradeError('CONTAINER_SCOPE_OR_REVISION_MISMATCH')
    mounts = value.get('mounts', [])
    if (len(mounts) != 1 or mounts[0].get('Type') != 'bind' or mounts[0].get('Destination') != '/data'
            or mounts[0].get('Source') != str(BASE / 'data') or mounts[0].get('RW') is not True):
        raise UpgradeError('CONTAINER_DATA_MOUNT_MISMATCH')
    actual_id = image_info(value['image_id'], metadata['version'], metadata['commit'])
    if image_id is not None and actual_id != image_id:
        raise UpgradeError('CONTAINER_IMAGE_DRIFT')
    if healthy and (not value['running'] or value['health'] != 'healthy'):
        raise UpgradeError('CONTAINER_NOT_HEALTHY')
    expected = service_config(release, actual_id, overlay)
    if (environment_map(value.get('env')) != expected['environment']
            or value.get('ports') != expected['ports'] or value.get('host_ports') != expected['ports']):
        raise UpgradeError('RUNTIME_CONFIG_MISMATCH')
    value['runtime_config_sha256'] = config_digest(expected, metadata)
    value['port'] = int(expected['ports']['8000/tcp'][0]['HostPort'])
    return value


def compose(release, overlay=None):
    args = ['compose', '-p', 'linkarium', '--project-directory', str(release), '--env-file', str(BASE / 'production.env'),
            '-f', str(release / 'deploy/cloud/compose.yaml')]
    if overlay:
        args += ['-f', str(overlay)]
    return args


def environment_map(values):
    # Docker Env is a list, not a map: do not silently collapse duplicates.
    if not isinstance(values, list):
        raise UpgradeError('RUNTIME_CONFIG_ENV_INVALID')
    result = {}
    for item in values:
        if not isinstance(item, str) or '=' not in item:
            raise UpgradeError('RUNTIME_CONFIG_ENV_INVALID')
        key, value = item.split('=', 1)
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', key) or key in result:
            raise UpgradeError('RUNTIME_CONFIG_ENV_INVALID')
        result[key] = value
    return result


def service_config(release, image_id, overlay=None, deployment=None):
    # Full resolved config stays in memory; never write or print it.
    config = json.loads(docker(compose(release, overlay) + ['config', '--format', 'json'], deployment=deployment))
    if set(config.get('services', {})) != {'app'}:
        raise UpgradeError('COMPOSE_SERVICE_SCOPE_MISMATCH')
    app = config['services']['app']
    volumes, ports = app.get('volumes', []), app.get('ports', [])
    if (len(volumes) != 1 or volumes[0].get('type') != 'bind' or volumes[0].get('target') != '/data'
            or volumes[0].get('source') != str(BASE / 'data') or volumes[0].get('read_only', False)
            or app.get('environment', {}).get('BOOKMARK_DATA_ROOT') != '/data'
            or len(ports) != 1 or ports[0].get('host_ip') != '127.0.0.1'
            or int(ports[0].get('target', 0)) != 8000 or ports[0].get('protocol', 'tcp') != 'tcp'):
        raise UpgradeError('COMPOSE_DATA_OR_LOOPBACK_MISMATCH')
    port = str(ports[0].get('published', ''))
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise UpgradeError('COMPOSE_PORT_INVALID')
    defaults = environment_map(json.loads(docker(['image', 'inspect', '--format', '{{json .Config.Env}}', image_id])))
    environment = app.get('environment')
    if not isinstance(environment, dict):
        raise UpgradeError('RUNTIME_CONFIG_ENV_INVALID')
    for key, value in environment.items():
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', key) or (value is not None and not isinstance(value, str)):
            raise UpgradeError('RUNTIME_CONFIG_ENV_INVALID')
        if value is None:
            defaults.pop(key, None)
        else:
            defaults[key] = value
    return {'environment': defaults, 'ports': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': port}]}}


def config_digest(config, metadata):
    environment = dict(config['environment'])
    for key, expected in (('LINKARIUM_VERSION', metadata['version']), ('LINKARIUM_REVISION', metadata['commit'])):
        if key in environment:
            if environment[key] != expected:
                raise UpgradeError('RUNTIME_CONFIG_CODE_METADATA_MISMATCH')
            environment[key] = None  # Bind presence, permit only expected code values.
    return hashlib.sha256(json.dumps({'environment': environment, 'ports': config['ports']},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def require_config_binding(receipt, actual):
    expected = receipt.get('runtime_config_sha256')
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected) or actual != expected:
        raise UpgradeError('PREPARED_RUNTIME_CONFIG_DRIFT')


def require_target_config(receipt, overlay=None):
    metadata = {'version': receipt['version'], 'commit': receipt['commit']}
    config = service_config(Path(receipt['new_release']), receipt['new_image_id'], overlay, deployment=metadata)
    require_config_binding(receipt, config_digest(config, metadata))


def application_files(release):
    result = {}
    for directory in ('backend', 'frontend/dist'):
        for path in walk(release / directory)[0]:
            result[path.relative_to(release).as_posix()] = digest(path)
    result['LICENSE'] = digest(release / 'LICENSE')
    result['requirements.lock'] = digest(release / 'deploy/requirements.lock')
    return result


# One observed immutable base, not a general extra-file allowlist. The pinned
# complete release manifest also binds release.json and all its metadata bytes.
LEGACY_BASE_IMAGE = {
    'image_id': 'sha256:9678767011890258ddd0d24460c6976cf479aa35cb23f1d3761676437de0c5c5',
    'release': '/srv/linkarium/releases/Linkarium-v0.2.0-local-ai.1-deploy',
    'version': 'v0.2.0-local-ai.1',
    'commit': '32b1b857b1ba3dc7b5835a5e750cef2ef4ed8576',
    'manifest_sha256': '3b14035e756c0b4b6af0503fa4c5602f37781b1e5706f5cb9789506cfb1ca79a',
    'files': {'frontend/dist/assets/index-D4nBYapU.js':
              'c4d161304e3084502915f49e7e989106b643e01729006b28beb6eceb13c5d587'},
    'provenance': {
        'evidence': 'legacy-asset-provenance-v1-output.txt',
        'retained_releases': ['Linkarium-v0.1.2-deploy', 'Linkarium-v0.1.3-firefox.1-deploy',
                              'Linkarium-v0.1.3-firefox.2-deploy'],
        'asset_bytes': 167117,
    },
}


IMAGE_CHECK = '''import hashlib,json,os,stat,sys
from pathlib import Path
expected=json.load(sys.stdin)
actual={}
for root in (Path('/app/backend'),Path('/app/frontend/dist')):
    assert root.is_dir() and not root.is_symlink()
    for parent,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            p=Path(parent)/name
            assert not p.is_symlink()
            if p.is_dir(): continue
            assert stat.S_ISREG(p.stat().st_mode)
            with p.open('rb') as f: actual[str(p.relative_to('/app'))]=hashlib.file_digest(f,'sha256').hexdigest()
for name in ('LICENSE','requirements.lock'):
    p=Path('/app')/name
    assert not p.is_symlink() and p.is_file()
    with p.open('rb') as f: actual[name]=hashlib.file_digest(f,'sha256').hexdigest()
assert actual==expected
print('{"verified":true}')
'''


def verify_image_source(image_id, release, *, base_journal=None):
    expected = application_files(release)
    legacy = None
    manifest_sha = digest(release / 'SHA256SUMS') if base_journal is not None else None
    # Only prepare's old-base check supplies a journal; every target stays exact
    # against its release alone, even if given the pinned legacy image ID.
    if base_journal is not None and image_id == LEGACY_BASE_IMAGE['image_id']:
        if (str(release) != LEGACY_BASE_IMAGE['release']
                or manifest_sha != LEGACY_BASE_IMAGE['manifest_sha256']):
            raise UpgradeError('LEGACY_BASE_IDENTITY_MISMATCH')
        verify_release(release, LEGACY_BASE_IMAGE['version'], LEGACY_BASE_IMAGE['commit'])
        if expected.keys() & LEGACY_BASE_IMAGE['files'].keys():
            raise UpgradeError('LEGACY_BASE_FILE_CONFLICT')
        expected.update(LEGACY_BASE_IMAGE['files'])
        legacy = LEGACY_BASE_IMAGE
    result = json.loads(docker(['run', '--rm', '-i', '--pull=never', '--network', 'none', '--read-only',
        '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--memory', '256m', '--pids-limit', '64', '--entrypoint', 'python', image_id, '-c', IMAGE_CHECK],
        input=json.dumps(expected, sort_keys=True).encode(), timeout=180))
    if result != {'verified': True}:
        raise UpgradeError('IMAGE_SOURCE_MISMATCH')
    proof = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    if base_journal is not None:
        write_json(base_journal / 'base-source-proof.json', {
            'image_id': image_id, 'release': str(release), 'release_manifest_sha256': manifest_sha,
            'source_sha256': proof, 'files': expected, 'legacy_inventory': legacy})
    return proof


def require_compatible(old, new):
    for name in ('deploy/requirements.lock', 'deploy/cloud/Dockerfile', 'deploy/cloud/compose.yaml'):
        if digest(old / name) != digest(new / name):
            raise UpgradeError('OFFLINE_DEPENDENCIES_OR_COMPOSE_CHANGED')
    def migrations(root):
        return {p.relative_to(root).as_posix(): digest(p) for p in walk(root / 'backend')[0]
                if 'migrations' in p.relative_to(root).parts}
    if not migrations(old) or migrations(old) != migrations(new):
        raise UpgradeError('SCHEMA_MIGRATIONS_CHANGED')


def prepare(args):
    validate_arguments(args.version, args.commit, args.expected_base)
    old, metadata = current_release(args.expected_base)
    old_manifest_sha = digest(old / 'SHA256SUMS')
    environment = regular(BASE / 'production.env').read_bytes()
    if updated_env(environment, metadata['version'], metadata['commit']) != environment:
        raise UpgradeError('ENV_CURRENT_METADATA_MISMATCH')
    container = require_container(old, metadata)
    port = container['port']
    journal = journal_path(args)
    journal.mkdir(mode=0o700)
    write_json(journal / 'phase.json', {'phase': 'preparing'})
    staging = safe(journal / 'extraction', journal)
    staging.mkdir(mode=0o700)
    extracted = extract_package(args.package, args.package_sha256, staging, args.version, args.commit)
    require_compatible(old, extracted)
    target = safe(BASE / 'releases' / extracted.name, BASE / 'releases')
    if target.exists():
        raise UpgradeError('TARGET_RELEASE_ALREADY_EXISTS')
    old_proof = verify_image_source(container['image_id'], old, base_journal=journal)
    new_manifest_sha = digest(extracted / 'SHA256SUMS')
    extracted.rename(target)
    sync_directory(target.parent)
    dockerfile = ('FROM ' + container['image_id'] + '\nUSER 0:0\n'
                  'RUN rm -rf /app/backend /app/frontend\n'
                  'COPY backend/ /app/backend/\nCOPY frontend/dist/ /app/frontend/dist/\n'
                  'COPY LICENSE /app/LICENSE\n'
                  'LABEL org.opencontainers.image.version="' + args.version + '"\n'
                  'LABEL org.opencontainers.image.revision="' + args.commit + '"\nUSER 10001:10001\n')
    atomic_bytes(journal / 'Dockerfile.offline', dockerfile.encode())
    docker(['build', '--pull=false', '--network=none', '-f', str(journal / 'Dockerfile.offline'),
            '-t', 'linkarium:' + args.version, '.'], cwd=target, timeout=900)
    new_id = image_info('linkarium:' + args.version, args.version, args.commit)
    new_proof = verify_image_source(new_id, target)
    # Refuse a concurrent code/env/container change even if the build succeeded.
    if (current_release(args.expected_base)[0] != old or regular(BASE / 'production.env').read_bytes() != environment
            or digest(old / 'SHA256SUMS') != old_manifest_sha or digest(target / 'SHA256SUMS') != new_manifest_sha):
        raise UpgradeError('PREPARE_BASE_DRIFT')
    latest = require_container(old, metadata, container['image_id'])
    require_config_binding(container, latest['runtime_config_sha256'])
    receipt = {'version': args.version, 'commit': args.commit, 'expected_base': args.expected_base,
        'old_release': str(old), 'new_release': str(target), 'old_metadata': metadata,
        'old_image_id': container['image_id'], 'new_image_id': new_id,
        'old_source_sha256': old_proof, 'new_source_sha256': new_proof, 'port': port,
        'base_source_proof_sha256': digest(journal / 'base-source-proof.json'),
        'runtime_config_sha256': container['runtime_config_sha256'],
        'package_sha256': args.package_sha256, 'env_sha256': hashlib.sha256(environment).hexdigest(),
        'old_manifest_sha256': old_manifest_sha, 'new_manifest_sha256': new_manifest_sha}
    require_target_config(receipt)
    write_json(journal / 'prepared.json', receipt)
    write_json(journal / 'phase.json', {'phase': 'prepared'})
    return receipt


@contextmanager
def file_lock(path, create=False):
    import fcntl
    path = safe(path)
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UpgradeError('LOCK_NOT_REGULAR')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpgradeError('WRITER_OR_OPERATOR_LOCKED') from None
        yield
    finally:
        os.close(fd)


def idle_counts(data, require_idle=False, immutable=False):
    with database(data / 'state.sqlite3', immutable) as connection:
        row = connection.execute('SELECT active_generation,maintenance_mode FROM runtime_runtimestate WHERE id=1').fetchone()
        if not row or not re.fullmatch('[A-Za-z0-9_-]{1,80}', row[0]):
            raise UpgradeError('ACTIVE_GENERATION_INVALID')
        generation, maintenance = row
        active = connection.execute("SELECT COUNT(*) FROM runtime_maintenanceoperation WHERE state IN ('uploading','validating','queued','running')").fetchone()[0]
    with database(safe(data / 'generations' / generation / 'library.sqlite3', data), immutable) as connection:
        jobs = connection.execute("SELECT COUNT(*) FROM library_job WHERE state IN ('queued','running','retry_wait')").fetchone()[0]
        counts = {name: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for name, table in (('bookmarks', 'library_bookmark'), ('snapshots', 'library_snapshotversion'), ('artifacts', 'library_artifact'))}
    if require_idle and (maintenance or active or jobs):
        raise UpgradeError('INSTANCE_NOT_IDLE')
    return {**counts, 'generation': generation, 'maintenance_mode': maintenance,
            'active_maintenance': active, 'active_jobs': jobs}


def require_counts(data, before, immutable=False):
    after = idle_counts(data, immutable=immutable)
    if after != before:
        raise UpgradeError('BUSINESS_COUNTS_CONTINUITY_FAILED')
    return after


def load_prepared(args):
    journal = journal_path(args)
    value = json.loads(regular(journal / 'prepared.json').read_bytes())
    if any(value.get(key) != getattr(args, key) for key in ('version', 'commit', 'expected_base')):
        raise UpgradeError('PREPARED_ARGUMENT_DRIFT')
    for key in ('old_release', 'new_release'):
        path = safe(value[key], BASE / 'releases')
        if path.parent != BASE / 'releases':
            raise UpgradeError('PREPARED_RELEASE_PATH_INVALID')
    if value['new_release'] != str(BASE / 'releases' / ('Linkarium-' + args.version + '-deploy')):
        raise UpgradeError('PREPARED_TARGET_PATH_INVALID')
    for key in ('old_image_id', 'new_image_id'):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', value[key]):
            raise UpgradeError('PREPARED_IMAGE_INVALID')
    return journal, value


def check_releases(receipt):
    old, target = Path(receipt['old_release']), Path(receipt['new_release'])
    verify_release(old, receipt['old_metadata']['version'], receipt['expected_base'])
    verify_release(target, receipt['version'], receipt['commit'])
    for name, root in (('old', old), ('new', target)):
        if digest(root / 'SHA256SUMS') != receipt[name + '_manifest_sha256']:
            raise UpgradeError('RELEASE_MANIFEST_DRIFT')
    require_compatible(old, target)
    image_info(receipt['old_image_id'], receipt['old_metadata']['version'], receipt['expected_base'])
    if image_info('linkarium:' + receipt['version'], receipt['version'], receipt['commit']) != receipt['new_image_id']:
        raise UpgradeError('PREPARED_IMAGE_DRIFT')


def pinned_overlay(journal, name, image_id):
    path = safe(journal / (name + '-image.compose.json'), journal)
    write_json(path, {'services': {'app': {'image': image_id, 'pull_policy': 'never'}}})
    return path


def start(release, overlay):
    docker(compose(release, overlay) + ['up', '-d', '--no-build', '--pull', 'never', '--no-deps',
        '--force-recreate', '--wait', '--wait-timeout', '180', 'app'], timeout=240)


def stop(release, overlay=None):
    docker(compose(release, overlay) + ['stop', '--timeout', '60', 'app'], timeout=90)
    try:
        running = container_info()['running']
    except UpgradeError:
        # A failed recreate may have removed app already. Prove absence in the
        # exact Compose project/service; an unreachable daemon is not absence.
        if docker(compose(release, overlay) + ['ps', '--all', '--quiet', 'app']).strip():
            raise UpgradeError('LINKARIUM_STOP_STATE_UNKNOWN') from None
        return
    if running:
        raise UpgradeError('LINKARIUM_DID_NOT_STOP')


def set_current(release):
    release = safe(release, BASE / 'releases')
    if release.parent != BASE / 'releases':
        raise UpgradeError('CURRENT_TARGET_OUTSIDE_SCOPE')
    temporary = BASE / ('.current-storage-' + uuid.uuid4().hex)
    temporary.symlink_to(release, target_is_directory=True)
    try:
        temporary.replace(BASE / 'current')
        sync_directory(BASE)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def loopback_ready(port):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise UpgradeError('LOOPBACK_REDIRECT_FORBIDDEN')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open('http://127.0.0.1:' + str(port) + '/health/ready', timeout=15) as response:
            if response.status != 200 or json.loads(response.read(8192)).get('ready') is not True:
                raise UpgradeError('LOOPBACK_NOT_READY')
    except UpgradeError:
        raise
    except Exception:
        raise UpgradeError('LOOPBACK_HEALTH_FAILED') from None
    return True


def check_active(journal, receipt):
    """Shared code/config binding; deliberately does not compare cold data."""
    current, metadata = current_release(receipt['commit'])
    if str(current) != receipt['new_release'] or metadata['version'] != receipt['version']:
        raise UpgradeError('ACTIVE_RELEASE_DRIFT')
    check_releases(receipt)
    overlay = journal / 'new-image.compose.json'
    if json.loads(regular(overlay).read_bytes()) != {'services': {'app': {'image': receipt['new_image_id'], 'pull_policy': 'never'}}}:
        raise UpgradeError('PINNED_OVERLAY_DRIFT')
    container = require_container(current, metadata, receipt['new_image_id'], overlay)
    require_config_binding(receipt, container['runtime_config_sha256'])
    original = regular(journal / 'production.env').read_bytes()
    if (hashlib.sha256(original).hexdigest() != receipt['env_sha256'] or
            regular(BASE / 'production.env').read_bytes() != updated_env(original, receipt['version'], receipt['commit'])):
        raise UpgradeError('ACTIVE_ENV_DRIFT')
    return current, metadata, container


def verify(args):
    journal, receipt = load_prepared(args)
    phase = json.loads(regular(journal / 'phase.json').read_bytes())['phase']
    if phase not in ('starting', 'active'):
        raise UpgradeError('ACTIVATION_NOT_READY_FOR_VERIFY')
    current, metadata, container = check_active(journal, receipt)
    cold = verify_cold(journal)
    counts_before = json.loads(regular(journal / 'counts-before.json').read_bytes())
    require_counts(journal / 'data', counts_before, immutable=True)
    before = json.loads(regular(journal / 'identity-before.json').read_bytes())
    if identity(journal / 'data', immutable=True) != before:
        raise UpgradeError('COLD_IDENTITY_CONTINUITY_FAILED')
    after = identity(BASE / 'data')
    if after != before:
        raise UpgradeError('ACTIVE_IDENTITY_CONTINUITY_FAILED')
    guard = json.loads(regular(journal / 'rollback-guard.json').read_bytes())
    if rollback_guard(BASE / 'data', immutable=False) != guard:
        raise UpgradeError('ACTIVE_SCHEMA_OR_COMPRESSION_CHANGED')
    ready = loopback_ready(receipt['port'])
    counts = require_counts(BASE / 'data', counts_before)
    return {'current': str(current), 'metadata': {k: metadata[k] for k in ('version', 'commit', 'git_tree')},
            'image_id': container['image_id'], 'revision': args.commit, 'ready': ready, 'counts': counts,
            'runtime_config_sha256': container['runtime_config_sha256'],
            'identity_continuity': True, 'schema_and_storage_unchanged': True, 'business_counts_unchanged': True,
            'identity_counts': {k: v['count'] for k, v in after['tables'].items()},
            'cold_checkpoint_verified': True, 'cold_manifest_sha256': digest(journal / 'data-manifest.json'),
            'cold_checkpoint': str(journal), 'copied_bytes': cold['copied_bytes'], 'linked_bytes': cold['linked_bytes'],
            'migration_additional_data_root': str(journal / 'data'), 'independent_disaster_backup': False}


def rollback_code(journal, receipt, new_start_attempted, *, explicit=False):
    """Restore only env/current/image. NEVER copy/replace/delete business/auth DBs."""
    counts_before, identity_before = {}, {}
    old, target = Path(receipt['old_release']), Path(receipt['new_release'])
    current, _ = current_release()
    if current not in ((target,) if explicit else (old, target)):
        raise UpgradeError('ROLLBACK_CURRENT_DRIFT')
    original = regular(journal / 'production.env').read_bytes()
    if hashlib.sha256(original).hexdigest() != receipt['env_sha256']:
        raise UpgradeError('ROLLBACK_ENV_CHECKPOINT_CHANGED')
    replacement = updated_env(original, receipt['version'], receipt['commit'])
    if regular(BASE / 'production.env').read_bytes() not in ((replacement,) if explicit else (original, replacement)):
        raise UpgradeError('ROLLBACK_LATEST_ENV_CHANGED')
    stop(current, journal / 'new-image.compose.json' if current == target else None)
    with file_lock(BASE / 'data/runtime/supervisor.lock'):
        if explicit:
            # Stopping releases actual network ports. Recheck disk bindings here;
            # full running-container configuration is checked before/after stop.
            if current_release(receipt['commit'])[0] != target:
                raise UpgradeError('ROLLBACK_CURRENT_DRIFT')
            if regular(BASE / 'production.env').read_bytes() != updated_env(original, receipt['version'], receipt['commit']):
                raise UpgradeError('ACTIVE_ENV_DRIFT')
        if new_start_attempted:
            before = json.loads(regular(journal / 'rollback-guard.json').read_bytes())
            if rollback_guard(BASE / 'data') != before:
                raise UpgradeError('ROLLBACK_REFUSED_SCHEMA_OR_COMPRESSION_CHANGED')
        verify_release(old, receipt['old_metadata']['version'], receipt['expected_base'])
        if digest(old / 'SHA256SUMS') != receipt['old_manifest_sha256']:
            raise UpgradeError('ROLLBACK_OLD_RELEASE_DRIFT')
        image_info(receipt['old_image_id'], receipt['old_metadata']['version'], receipt['expected_base'])
        if explicit:
            counts_before = idle_counts(BASE / 'data', require_idle=True, immutable=True)
            identity_before = identity(BASE / 'data', immutable=True)
            write_json(journal / 'rollback-counts-before.json', counts_before)
            write_json(journal / 'rollback-identity-before.json', identity_before)
        atomic_bytes(BASE / 'production.env', original)
        set_current(old)
    overlay = pinned_overlay(journal, 'old', receipt['old_image_id'])
    start(old, overlay)
    container = require_container(old, receipt['old_metadata'], receipt['old_image_id'], overlay)
    require_config_binding(receipt, container['runtime_config_sha256'])
    ready = loopback_ready(receipt['port'])
    result = {}
    if explicit:
        if current_release(receipt['expected_base'])[0] != old:
            raise UpgradeError('ROLLBACK_CURRENT_DRIFT')
        if regular(BASE / 'production.env').read_bytes() != original:
            raise UpgradeError('ROLLBACK_ENV_DRIFT')
        if json.loads(regular(overlay).read_bytes()) != {'services': {'app': {'image': receipt['old_image_id'], 'pull_policy': 'never'}}}:
            raise UpgradeError('PINNED_OVERLAY_DRIFT')
        guard = json.loads(regular(journal / 'rollback-guard.json').read_bytes())
        if rollback_guard(BASE / 'data', immutable=False) != guard:
            raise UpgradeError('ROLLBACK_REFUSED_SCHEMA_OR_COMPRESSION_CHANGED')
        counts = require_counts(BASE / 'data', counts_before)
        after = identity(BASE / 'data')
        if after != identity_before:
            raise UpgradeError('ROLLBACK_IDENTITY_CONTINUITY_FAILED')
        result = {'code_rolled_back': True, 'business_and_auth_data_restored': False,
                  'image_id': container['image_id'], 'ready': ready,
                  'runtime_config_sha256': container['runtime_config_sha256'],
                  'identity_continuity': True, 'business_counts_unchanged': True,
                  'counts': {k: v for k, v in counts.items() if k != 'generation'},
                  'generation_sha256': hashlib.sha256(counts['generation'].encode()).hexdigest(),
                  'identity_counts': {k: v['count'] for k, v in after['tables'].items()}}
        write_json(journal / 'rollback-receipt.json', result)
    write_json(journal / 'phase.json', {'phase': 'code_rolled_back', 'business_and_auth_data_restored': False})
    return result


def rollback(args):
    journal, receipt = load_prepared(args)
    if json.loads(regular(journal / 'phase.json').read_bytes()).get('phase') != 'active':
        raise UpgradeError('PHASE_NOT_ACTIVE_MANUAL_REVIEW_REQUIRED')
    _, metadata, container = check_active(journal, receipt)
    if receipt['old_metadata']['commit'] != args.expected_base:
        raise UpgradeError('PREPARED_OLD_METADATA_DRIFT')
    if container['port'] != receipt['port']:
        raise UpgradeError('PREPARED_PORT_DRIFT')
    old_config = service_config(Path(receipt['old_release']), receipt['old_image_id'], deployment=receipt['old_metadata'])
    require_config_binding(receipt, config_digest(old_config, receipt['old_metadata']))
    activation = json.loads(regular(journal / 'activation-receipt.json').read_bytes())
    expected = {'current': receipt['new_release'], 'image_id': receipt['new_image_id'],
                'revision': args.commit, 'metadata': {k: metadata[k] for k in ('version', 'commit', 'git_tree')},
                'runtime_config_sha256': receipt['runtime_config_sha256'], 'cold_checkpoint': str(journal)}
    if (any(activation.get(key) != value for key, value in expected.items())
            or any(activation.get(key) is not True for key in ('ready', 'identity_continuity',
                'schema_and_storage_unchanged', 'business_counts_unchanged', 'cold_checkpoint_verified'))):
        raise UpgradeError('ACTIVATION_RECEIPT_MISMATCH')
    guard = json.loads(regular(journal / 'rollback-guard.json').read_bytes())
    current_guard = rollback_guard(BASE / 'data', immutable=False)
    if current_guard != guard or current_guard['gzip_representations']:
        raise UpgradeError('ROLLBACK_REFUSED_SCHEMA_OR_COMPRESSION_CHANGED')
    idle_counts(BASE / 'data', require_idle=True)
    identity(BASE / 'data')  # Validate current identity inputs, not historical cold rows.
    # The representation scan can be slow. Rebind immediately before stop.
    check_active(journal, receipt)
    try:
        write_json(journal / 'phase.json', {'phase': 'rolling_back', 'business_and_auth_data_restored': False})
        return rollback_code(journal, receipt, True, explicit=True)
    except BaseException:
        write_json(journal / 'phase.json', {'phase': 'manual_recovery_required', 'business_and_auth_data_restored': False})
        raise


def activate(args):
    journal, receipt = load_prepared(args)
    if json.loads(regular(journal / 'phase.json').read_bytes())['phase'] != 'prepared':
        raise UpgradeError('PHASE_NOT_PREPARED_MANUAL_REVIEW_REQUIRED')
    old, metadata = current_release(args.expected_base)
    if str(old) != receipt['old_release']:
        raise UpgradeError('CURRENT_RELEASE_DRIFT')
    check_releases(receipt)
    container = require_container(old, metadata, receipt['old_image_id'])
    require_config_binding(receipt, container['runtime_config_sha256'])
    require_target_config(receipt)
    original = regular(BASE / 'production.env').read_bytes()
    if hashlib.sha256(original).hexdigest() != receipt['env_sha256']:
        raise UpgradeError('PREPARED_ENV_DRIFT')
    replacement = updated_env(original, args.version, args.commit)
    if container['port'] != receipt['port']:
        raise UpgradeError('PREPARED_PORT_DRIFT')
    idle_counts(BASE / 'data', require_idle=True)
    # The expensive ZIPs are excluded only by completed-operation receipts.
    # This first budget gate is deliberately BEFORE any service stop.
    plan = plan_data(BASE / 'data', journal)
    require_space(plan)
    write_json(journal / 'space-plan.json', {k: plan[k] for k in ('copy_bytes', 'overhead_bytes', 'reserve_bytes', 'free_bytes')})
    atomic_bytes(journal / 'production.env', original)
    overlay = pinned_overlay(journal, 'new', receipt['new_image_id'])
    target = Path(receipt['new_release'])
    # Repeat after the potentially long space scan and immediately before the
    # stop boundary. A pre-stop refusal must not enter code rollback or stop.
    if regular(BASE / 'production.env').read_bytes() != original or current_release(args.expected_base)[0] != old:
        raise UpgradeError('CUTOVER_BASE_DRIFT')
    require_config_binding(receipt, require_container(old, metadata, receipt['old_image_id'])['runtime_config_sha256'])
    require_target_config(receipt, overlay)
    new_start_attempted = False
    try:
        write_json(journal / 'phase.json', {'phase': 'stopping'})
        stop(old)
        with file_lock(BASE / 'data/runtime/supervisor.lock'):
            require_quiescent_sqlite(BASE / 'data')
            counts_before = idle_counts(BASE / 'data', require_idle=True, immutable=True)
            write_json(journal / 'counts-before.json', counts_before)
            before = identity(BASE / 'data', immutable=True)
            write_json(journal / 'identity-before.json', before)
            write_json(journal / 'rollback-guard.json', rollback_guard(BASE / 'data'))
            cold_snapshot(BASE / 'data', journal)
            require_counts(journal / 'data', counts_before, immutable=True)
            require_counts(BASE / 'data', counts_before, immutable=True)
            if identity(journal / 'data', immutable=True) != before or identity(BASE / 'data', immutable=True) != before:
                raise UpgradeError('COLD_IDENTITY_CONTINUITY_FAILED')
            write_json(journal / 'phase.json', {'phase': 'cold_complete', 'checkpoint_is_not_code_rollback': True})
            if regular(BASE / 'production.env').read_bytes() != original or current_release(args.expected_base)[0] != old:
                raise UpgradeError('CUTOVER_BASE_DRIFT')
            atomic_bytes(BASE / 'production.env', replacement)
            set_current(target)
        write_json(journal / 'phase.json', {'phase': 'starting'})
        new_start_attempted = True
        start(target, overlay)
        result = verify(args)
        write_json(journal / 'activation-receipt.json', result)
        write_json(journal / 'phase.json', {'phase': 'active'})
        return result
    except BaseException:
        try:
            rollback_code(journal, receipt, new_start_attempted)
        except BaseException:
            write_json(journal / 'phase.json', {'phase': 'manual_recovery_required', 'business_and_auth_data_restored': False})
            raise UpgradeError('AUTOMATIC_CODE_ROLLBACK_REFUSED_OR_FAILED') from None
        raise


def require_root_owned(path, mode=None):
    info = safe(path).stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or (mode is not None and stat.S_IMODE(info.st_mode) != mode):
        raise UpgradeError('ROOT_OWNERSHIP_OR_MODE_REQUIRED')


def require_host():
    if BASE != Path('/srv/linkarium'):
        raise UpgradeError('FIXED_BASE_REQUIRED')
    if os.name != 'posix' or os.geteuid() != 0:
        raise UpgradeError('LINUX_ROOT_REQUIRED')
    for path in (BASE.parent, BASE, BASE / 'releases', BASE / 'ops'):
        if not safe(path).is_dir():
            raise UpgradeError('DEPLOYMENT_LAYOUT_INVALID')
        require_root_owned(path)
    if not safe(BASE / 'data').is_dir():
        raise UpgradeError('DATA_DIRECTORY_INVALID')
    require_root_owned(regular(BASE / 'production.env'), 0o600)
    if (BASE / 'production.env').stat().st_nlink != 1:
        raise UpgradeError('ENV_HARDLINK_FORBIDDEN')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('phase', choices=('prepare', 'activate', 'verify', 'rollback'))
    parser.add_argument('--version', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--expected-base', required=True)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--package-sha256')
    args = parser.parse_args(argv)
    try:
        validate_arguments(args.version, args.commit, args.expected_base)
        if args.phase == 'prepare':
            if args.package is None or not re.fullmatch('[0-9a-f]{64}', args.package_sha256 or ''):
                raise UpgradeError('PREPARE_PACKAGE_AND_SHA256_REQUIRED')
        elif args.package is not None or args.package_sha256 is not None:
            raise UpgradeError('PACKAGE_ARGUMENTS_ONLY_FOR_PREPARE')
    except UpgradeError as error:
        parser.error(str(error))
    return args


def emit(value):
    try:
        print(json.dumps(value, sort_keys=True), flush=True)
    except (BrokenPipeError, OSError):
        pass  # A disconnected observer must not interrupt cold cutover.


def main(argv=None):
    args = parse_args(argv)
    completed_rollback = False
    journal = None
    try:
        require_host()
        os.umask(0o077)
        journal = journal_path(args)
        if journal.exists():
            require_root_owned(journal, 0o700)
        def interrupted(signum, frame):
            raise UpgradeError('OPERATOR_INTERRUPTED')
        signal.signal(signal.SIGTERM, interrupted)
        # Shared with the reviewed previous operator; no parallel cold upgrade.
        with file_lock(BASE / 'ops/v012-operator.lock', create=True):
            emit({'phase': args.phase, 'event': 'started'})
            if args.phase == 'prepare':
                prepared = prepare(args)
                result = {k: prepared[k] for k in ('version', 'commit', 'expected_base', 'old_image_id', 'new_image_id')}
                result.update(journal=str(journal), prepared=True)
            elif args.phase == 'activate':
                result = activate(args)
            elif args.phase == 'rollback':
                result = rollback(args)
                completed_rollback = True
            else:
                result = verify(args)
        emit({'phase': args.phase, 'event': 'complete', **result})
        return 0
    except BaseException as error:
        if completed_rollback and journal is not None:
            try:
                write_json(journal / 'phase.json', {'phase': 'manual_recovery_required', 'business_and_auth_data_restored': False})
            except BaseException:
                pass  # The failed CLI outcome remains authoritative if storage is unavailable.
        # Never print arbitrary exception messages, argv, environment or rows.
        code = str(error) if isinstance(error, UpgradeError) and re.fullmatch('[A-Z0-9_]+', str(error)) else type(error).__name__
        emit({'phase': args.phase, 'event': 'failed', 'code': code})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
