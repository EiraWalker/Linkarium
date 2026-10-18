#!/usr/bin/env python3
"""Root-owned fixed-operation launcher. Public activation requires a local Linux audit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import subprocess
import threading
import shutil
import time
from uuid import UUID

MAX_CAPTURE_BYTES = 96 * 1024 * 1024
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
CAPTURE_SCRATCH_BYTES = 4 * MAX_CAPTURE_BYTES
SPOOL_TTL_SECONDS = 3600
MAX_SPOOL_BYTES = 1024 * 1024 * 1024
MAX_SPOOL_ATTEMPTS = 32

BLOCKED_IPV4 = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
    '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
    '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
    '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')


def run(*args, input=None, check=True):
    return subprocess.run(args, input=input, text=True, capture_output=True, check=check, timeout=15)


def validated_preview(result, scratch, variant):
    """Root boundary: never trust a sandbox path, hash, image type or dimensions.

    This intentionally stays self-contained in the root-owned launcher, rather
    than importing code from the application-writable deployment tree.
    """
    preview_path = result.get('preview_path')
    if not preview_path:
        if result.get('preview_sha256') is not None or result.get('preview_byte_size', 0):
            raise RuntimeError('Incomplete preview result')
        return None
    digest, size = result.get('preview_sha256'), result.get('preview_byte_size')
    width, height = (1366, 768) if variant == 'desktop' else (390, 844)
    if (not result.get('artifact_path') or not isinstance(digest, str) or len(digest) != 64
            or any(value not in '0123456789abcdef' for value in digest)
            or type(size) is not int or not 4 <= size <= MAX_PREVIEW_BYTES
            or result.get('viewport_width') != width or result.get('viewport_height') != height):
        raise RuntimeError('Invalid preview result')
    name = Path(preview_path).name
    if name != digest + '.jpg':
        raise RuntimeError('Invalid preview filename')
    path = scratch / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
        raise RuntimeError('Invalid preview file')
    content = Launcher.read_regular(path, MAX_PREVIEW_BYTES)
    if len(content) != size:
        raise RuntimeError('Invalid preview size')
    if hashlib.sha256(content).hexdigest() != digest or content[:2] != b'\xff\xd8' or content[-2:] != b'\xff\xd9':
        raise RuntimeError('Invalid preview content')
    offset, dimensions, scan = 2, None, False
    while offset < len(content) - 2:
        if content[offset] != 0xff:
            raise RuntimeError('Invalid preview JPEG header')
        while offset < len(content) and content[offset] == 0xff:
            offset += 1
        if offset >= len(content):
            break
        marker = content[offset]
        offset += 1
        if marker in (0, 0xd8, 0xd9) or 0xd0 <= marker <= 0xd7 or offset + 2 > len(content):
            raise RuntimeError('Invalid preview JPEG header')
        length = int.from_bytes(content[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(content) - 2:
            raise RuntimeError('Invalid preview JPEG segment')
        if marker in (0xc0, 0xc1, 0xc2):
            if dimensions is not None or length < 8 or content[offset + 2] != 8:
                raise RuntimeError('Invalid preview JPEG dimensions')
            dimensions = (int.from_bytes(content[offset + 5:offset + 7], 'big'),
                          int.from_bytes(content[offset + 3:offset + 5], 'big'))
        if marker == 0xda:
            scan = True
            break
        offset += length
    if not scan or dimensions != (width, height):
        raise RuntimeError('Invalid preview JPEG dimensions')
    return name, content


class Launcher:
    def __init__(self, config):
        self.config = config
        self.spool = Path(os.path.abspath(config['spool_root']))
        self.active = {}
        self.pending = set()
        self.cancelled = set()
        self.lock = threading.Lock()
        self.execution_lock = threading.Lock()
        self.storage_ready = False

    def initialize_storage(self):
        """Make the shared root immutable to the app; only its inbox is writable.

        Every component is opened without following symlinks. Once initialized,
        private paths and result paths have no application-writable ancestor.
        """
        if len(self.spool.parts) < 3:
            raise RuntimeError('Capture spool must be a dedicated directory')
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            components = self.spool.parts[1:]
            for index, component in enumerate(components):
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                final = index == len(components) - 1
                if info.st_uid != 0 or (not final and info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                    raise RuntimeError('Capture spool ancestors must be root-owned and protected')
            os.fchown(descriptor, 0, self.config['app_gid'])
            os.fchmod(descriptor, 0o750)
            for name, mode in (('incoming', 0o770), ('private', 0o700), ('results', 0o750)):
                try:
                    os.mkdir(name, mode, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    info = os.fstat(child)
                    if info.st_uid != 0 or (name != 'incoming' and info.st_mode & 0o022):
                        raise RuntimeError('Capture spool internal directories are not trusted')
                    os.fchown(child, 0, self.config['app_gid'])
                    os.fchmod(child, mode)
                finally:
                    os.close(child)
        finally:
            os.close(descriptor)
        self.storage_ready = True

    @staticmethod
    def read_regular(path, maximum):
        if path.is_symlink():
            raise RuntimeError('Expected a nonlinked regular file')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                raise RuntimeError('Expected a bounded nonlinked regular file')
            content = stream.read(maximum + 1)
            if len(content) > maximum:
                raise RuntimeError('File exceeds size budget')
            return content

    def acquire(self, identity):
        if not self.storage_ready:
            self.initialize_storage()
        self.reap()
        entries = list((self.spool / 'results').iterdir()) + list((self.spool / 'private').iterdir())
        retained = sum(path.stat(follow_symlinks=False).st_size
                       for entry in entries if entry.is_dir() and not entry.is_symlink()
                       for path in entry.rglob('*') if not path.is_symlink() and path.is_file())
        if len(entries) >= MAX_SPOOL_ATTEMPTS or retained + MAX_CAPTURE_BYTES + MAX_PREVIEW_BYTES > MAX_SPOOL_BYTES:
            raise RuntimeError('Capture spool capacity is exhausted; release completed captures first')
        attempt = identity[0]
        incoming = os.open(self.spool / 'incoming', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        private = os.open(self.spool / 'private', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        moved = False
        try:
            if (self.spool / 'private' / attempt).exists() or (self.spool / 'results' / attempt).exists():
                raise ValueError('Capture attempt already has retained output')
            os.rename(attempt, attempt, src_dir_fd=incoming, dst_dir_fd=private)
            moved = True
            descriptor = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=private)
            try:
                os.fchown(descriptor, 0, 0)
                os.fchmod(descriptor, 0o700)
                with os.scandir(descriptor) as entries:
                    if any(entry.name != 'request.json' for entry in entries):
                        raise ValueError('Attempt inbox contains unexpected entries')
            finally:
                os.close(descriptor)
        except Exception:
            if moved:
                self.remove_folder(self.spool / 'private' / attempt)
            raise
        finally:
            os.close(incoming)
            os.close(private)
        return self.spool / 'private' / attempt

    def remove_folder(self, folder):
        # rmtree's fd-based implementation refuses symlink substitution at every
        # recursive directory boundary. Never recurse through an active mount.
        if folder.is_symlink():
            folder.unlink()
            return
        if not folder.exists():
            return
        scratch = folder / 'scratch'
        if not scratch.is_symlink() and os.path.ismount(scratch):
            if (folder.parent != self.spool / 'private'
                    and (folder.parent != self.spool or folder.lstat().st_uid != 0)):
                raise RuntimeError('Refusing to traverse an unexpected spool mount')
            run('umount', str(scratch), check=False)
            if os.path.ismount(scratch):
                raise RuntimeError('Capture scratch mount has not been released')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError('Secure recursive spool cleanup is unavailable')
        shutil.rmtree(folder)

    def release(self, identity):
        if not self.storage_ready:
            self.initialize_storage()
        for kind in ('private', 'results'):
            folder = self.spool / kind / identity[0]
            if not folder.exists():
                continue
            recorded = json.loads(self.read_regular(folder / '.identity.json', 1024))
            if tuple(recorded) != identity:
                raise ValueError('Capture output identity mismatch')
            self.remove_folder(folder)
        incoming = self.spool / 'incoming' / identity[0]
        if incoming.exists() or incoming.is_symlink():
            self.remove_folder(incoming)
        return {'ok': True, 'released': True, 'identity': identity}

    def reap(self):
        if not self.storage_ready:
            return
        cutoff = time.time() - SPOOL_TTL_SECONDS
        with self.lock:
            protected = {identity[0] for identity in self.active} | {identity[0] for identity in self.pending}
        # UUID folders directly under the root are the pre-release-protocol layout.
        # Sweep them too, so upgrading does not strand the very files being fixed.
        for kind in ('incoming', 'private', 'results', 'legacy'):
            parent = self.spool if kind == 'legacy' else self.spool / kind
            for folder in parent.iterdir():
                try:
                    attempt = str(UUID(folder.name))
                    if attempt != folder.name or attempt in protected or folder.lstat().st_mtime >= cutoff:
                        continue
                    # Restarted launchers retain systemd's exact attempt identity.
                    if kind in ('private', 'legacy') and not folder.is_symlink():
                        manifest = folder / '.identity.json'
                        if manifest.exists():
                            identity = tuple(json.loads(self.read_regular(manifest, 1024)))
                        elif kind == 'legacy' and (folder / 'sealed.json').exists():
                            identity = self.identity(json.loads(self.read_regular(folder / 'sealed.json', 16384)))
                        else:
                            identity = (attempt, 'unknown', 0)
                        if self.persistent_unit_running(identity):
                            continue
                    self.remove_folder(folder)
                except (OSError, ValueError, RuntimeError):
                    continue

    @staticmethod
    def unit_description(identity):
        return 'bookmark-capture|' + '|'.join(str(value) for value in identity)

    def persistent_unit_running(self, identity):
        if os.name != 'posix':
            return False
        # A launcher restart loses its Python process map, not systemd's cgroups.
        # Inspect the exact persisted unit identity before acknowledging cancellation.
        result = run('systemctl', 'show', 'bmc-' + identity[0], '--property=LoadState',
            '--property=ActiveState', '--property=Description', check=False)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not fields or (result.returncode != 0 and fields.get('LoadState') != 'not-found'):
            raise RuntimeError('Cannot verify persisted capture cgroup state')
        active = fields.get('ActiveState') in ('active', 'activating', 'deactivating', 'reloading')
        if active and fields.get('Description') != self.unit_description(identity):
            raise RuntimeError('Persisted capture unit identity does not match the requested generation/epoch')
        return active

    @staticmethod
    def identity(payload):
        attempt = str(UUID(payload['attempt_uuid']))
        generation = payload['generation']
        epoch = payload['library_epoch']
        if not isinstance(generation, str) or not generation.replace('-', '').replace('_', '').isalnum() or len(generation) > 80:
            raise ValueError('Invalid generation')
        if type(epoch) is not int or epoch < 0:
            raise ValueError('Invalid library epoch')
        return attempt, generation, epoch

    def handle(self, payload):
        identity = self.identity(payload)
        action = payload.get('action')
        if action not in ('start', 'cancel', 'status', 'release') or set(payload) != {'action', 'attempt_uuid', 'generation', 'library_epoch'}:
            raise ValueError('Only fixed launcher operations are accepted')
        if action in ('cancel', 'status', 'release'):
            cancelling = action in ('cancel', 'release')
            with self.lock:
                process = self.active.get(identity)
                pending = identity in self.pending
                if cancelling and pending:
                    self.cancelled.add(identity)
            if cancelling and process is not None:
                # systemd kills all members of the exact attempt's cgroup, not just its direct child.
                run('systemctl', 'kill', '--kill-whom=all', '--signal=KILL', 'bmc-' + identity[0], check=False)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    raise RuntimeError('Capture process group has not exited')
            persisted = self.persistent_unit_running(identity)
            if cancelling and persisted:
                run('systemctl', 'stop', 'bmc-' + identity[0], check=False)
                persisted = self.persistent_unit_running(identity)
                if persisted:
                    raise RuntimeError('Persisted capture cgroup has not exited')
            if action == 'release':
                with self.execution_lock:
                    return self.release(identity)
            return {'ok': True, 'running': persisted or (process is not None and process.poll() is None),
                    'exited': (process is None or process.poll() is not None) and
                              (not pending or action == 'cancel' or identity in self.cancelled) and not persisted,
                    'identity': identity}
        # No default public fallback. This file is populated only after Linux acceptance has passed.
        attestation = Path(self.config['verification_file'])
        if not attestation.is_file() or attestation.is_symlink() or attestation.stat().st_uid != 0:
            return {'ok': False, 'error': {'code': 'isolation_unverified', 'message': 'Linux capture containment has not passed deployment acceptance'}}
        proof = json.loads(attestation.read_text())
        if proof.get('launcher_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest() or proof.get('public_capture_verified') is not True:
            return {'ok': False, 'error': {'code': 'isolation_unverified', 'message': 'Linux capture acceptance is missing or stale'}}
        with self.lock:
            if identity in self.pending or identity in self.active:
                raise ValueError('Attempt is already active')
            self.pending.add(identity)
        try:
            with self.execution_lock:
                with self.lock:
                    if identity in self.cancelled:
                        return {'ok': False, 'error': {'code': 'cancelled', 'message': 'Capture was cancelled before execution'}}
                return self.start(identity)
        finally:
            with self.lock:
                self.pending.discard(identity)
                self.cancelled.discard(identity)

    def start(self, identity):
        attempt, generation, epoch = identity
        folder = None
        try:
            folder = self.acquire(identity)
            return self.execute(identity, folder)
        finally:
            # Even validation, cancellation and failed sandbox startup must not
            # retain root-owned inputs or scratch files indefinitely.
            if folder is not None:
                self.remove_folder(folder)

    def execute(self, identity, folder):
        attempt, generation, epoch = identity
        body = json.loads(self.read_regular(folder / 'request.json', 16384))
        if set(body) != {'attempt_uuid', 'generation', 'library_epoch', 'url', 'variant', 'mode'} or self.identity(body) != identity:
            raise ValueError('Attempt input identity mismatch')
        if body['variant'] not in ('desktop', 'mobile') or body['mode'] not in ('capture', 'metadata'):
            raise ValueError('Unknown task kind')
        (folder / '.identity.json').write_text(json.dumps(identity))
        output = folder / 'output'
        output.mkdir(mode=0o750, exist_ok=False)
        os.chown(output, 0, self.config['app_gid'])
        (output / '.identity.json').write_text(json.dumps(identity))
        scratch = folder / 'scratch'
        scratch.mkdir(mode=0o750, exist_ok=False)
        sealed = folder / 'sealed.json'
        # Root-owned budget: callers still cannot add budget or filesystem fields.
        body.update({'output_dir': '/output', 'deny_hosts': self.config['deny_hosts'], 'development': False,
                     'max_bytes': MAX_CAPTURE_BYTES})
        with sealed.open('x', encoding='utf-8') as handle:
            json.dump(body, handle)
        os.chmod(sealed, 0o444)
        namespace = 'bmc-' + attempt[:8]
        interface = 'bc' + attempt.replace('-', '')[:10]
        peer = interface + 'p'
        nft_table = 'bmc_' + attempt.replace('-', '')[:12]
        rootfs = self.config['rootfs']
        unit = 'bmc-' + attempt
        process = None
        mounted_scratch = False
        try:
            # Limit writes at the filesystem, then copy verified final bytes out.
            run('mount', '-t', 'tmpfs', '-o', f'size={CAPTURE_SCRATCH_BYTES},mode=0750,uid=65534,gid=' + str(self.config['app_gid']),
                'bookmark-capture-output', str(scratch))
            mounted_scratch = True
            run('ip', 'netns', 'add', namespace)
            run('ip', 'link', 'add', interface, 'type', 'veth', 'peer', 'name', peer)
            run('ip', 'link', 'set', peer, 'netns', namespace)
            run('ip', 'addr', 'add', '169.254.253.1/30', 'dev', interface)
            run('ip', 'link', 'set', interface, 'up')
            run('ip', '-n', namespace, 'addr', 'add', '169.254.253.2/30', 'dev', peer)
            run('ip', '-n', namespace, 'link', 'set', peer, 'up')
            run('ip', '-n', namespace, 'link', 'set', 'lo', 'up')
            run('ip', '-n', namespace, 'route', 'add', 'default', 'via', '169.254.253.1')
            denied = ', '.join((*BLOCKED_IPV4, *self.config['deny_ipv4']))
            resolvers = ', '.join(self.config['resolvers'])
            ns_rules = f'''table inet capture {{
              set denied {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {denied} }} }}
              chain input {{ type filter hook input priority 0; policy drop; quota over {MAX_CAPTURE_BYTES} bytes drop; ct state established,related accept; }}
              chain output {{ type filter hook output priority 0; policy drop;
                quota over {MAX_CAPTURE_BYTES} bytes drop
                meta nfproto ipv6 drop
                ip daddr @denied drop
                ip daddr {{ {resolvers} }} udp dport 53 accept
                ip daddr {{ {resolvers} }} tcp dport 53 accept
                tcp dport {{80,443}} accept
              }}
            }}'''
            run('ip', 'netns', 'exec', namespace, 'nft', '-f', '-', input=ns_rules)
            host_rules = f'''table inet {nft_table} {{
              set denied {{ type ipv4_addr; flags interval; auto-merge; elements = {{ {denied} }} }}
              chain input {{ type filter hook input priority -5; policy accept; iifname "{interface}" drop; }}
              chain forward {{ type filter hook forward priority -5; policy accept;
                iifname "{interface}" meta nfproto ipv6 drop
                iifname "{interface}" ip daddr @denied drop
                iifname "{interface}" ip daddr {{ {resolvers} }} udp dport 53 accept
                iifname "{interface}" ip daddr {{ {resolvers} }} tcp dport 53 accept
                iifname "{interface}" tcp dport {{80,443}} accept
                iifname "{interface}" drop
                oifname "{interface}" ct state established,related accept
                oifname "{interface}" drop
              }}
            }}
            table ip {nft_table} {{
              chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
                ip saddr 169.254.253.2 oifname != "{interface}" masquerade
              }}
            }}'''
            run('nft', '-f', '-', input=host_rules)
            argv = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec', '--unit=' + unit,
                '-p', 'Description=' + self.unit_description(identity),
                '-p', 'RootDirectory=' + rootfs, '-p', 'NetworkNamespacePath=/run/netns/' + namespace,
                '-p', 'User=65534', '-p', 'Group=' + str(self.config['app_gid']),
                '-p', 'BindPaths=' + str(scratch) + ':/output', '-p', 'BindReadOnlyPaths=' + str(sealed) + ':/request.json',
                '-p', 'ProtectSystem=strict', '-p', 'ReadWritePaths=/output', '-p', 'ProtectHome=yes',
                '-p', 'TemporaryFileSystem=/tmp:rw,size=256M', '-p', 'PrivateDevices=yes', '-p', 'PrivateUsers=yes',
                '-p', 'NoNewPrivileges=yes', '-p', 'MemoryMax=1536M', '-p', 'TasksMax=256',
                '-p', 'RuntimeMaxSec=125', '-p', 'KillMode=control-group',
                '-p', 'RestrictAddressFamilies=AF_INET AF_UNIX',
                '--setenv=BOOKMARK_CAPTURE_ISOLATED=1', '--setenv=PYTHONPATH=/opt/capture/backend',
                '--setenv=PLAYWRIGHT_BROWSERS_PATH=/opt/capture/browsers', '--setenv=MONOLITH_PATH=/opt/capture/monolith',
                '/opt/capture/venv/bin/python', '-m', 'capture', '--request', '/request.json']
            with self.lock:
                if identity in self.cancelled:
                    return {'ok': False, 'error': {'code': 'cancelled', 'message': 'Capture was cancelled before execution'}}
                process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                self.active[identity] = process
            stdout, _stderr = process.communicate(timeout=130)
            # All sandbox descendants must exit before root reads their output.
            run('systemctl', 'stop', unit)
            lines = [line for line in stdout.splitlines() if line.startswith('{')]
            if process.returncode or not lines:
                return {'ok': False, 'error': {'code': 'capture_failed', 'message': 'Isolated capture execution failed'}}
            response = json.loads(lines[-1])
            preview = validated_preview(response['result'], scratch, body['variant']) if response.get('ok') else None
            if response.get('ok') and response['result'].get('artifact_path'):
                artifact = scratch / Path(response['result']['artifact_path']).name
                content = self.read_regular(artifact, MAX_CAPTURE_BYTES)
                digest = response['result'].get('sha256')
                if (not content or type(response['result'].get('byte_size')) is not int
                        or len(content) != response['result']['byte_size'] or artifact.name != str(digest) + '.html'):
                    raise RuntimeError('Invalid capture output')
                actual_hash = hashlib.sha256(content).hexdigest()
                if actual_hash != digest:
                    raise RuntimeError('Capture output hash mismatch')
                target = output / artifact.name
                with target.open('xb') as target_file:
                    target_file.write(content)
                    target_file.flush()
                    os.fsync(target_file.fileno())
                os.chmod(target, 0o640)
                os.chown(target, 0, self.config['app_gid'])
                if preview is not None:
                    preview_name, preview_content = preview
                    preview_target = output / preview_name
                    with preview_target.open('xb') as preview_file:
                        preview_file.write(preview_content)
                        preview_file.flush()
                        os.fsync(preview_file.fileno())
                    os.chmod(preview_target, 0o640)
                    os.chown(preview_target, 0, self.config['app_gid'])
            if response.get('ok'):
                os.rename(output, self.spool / 'results' / attempt)
            return response
        finally:
            run('systemctl', 'stop', unit, check=False)
            with self.lock:
                self.active.pop(identity, None)
            run('nft', 'delete', 'table', 'inet', nft_table, check=False)
            run('nft', 'delete', 'table', 'ip', nft_table, check=False)
            run('ip', 'link', 'delete', interface, check=False)
            run('ip', 'netns', 'delete', namespace, check=False)
            if mounted_scratch:
                run('umount', str(scratch), check=False)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            _pid, uid, gid = __import__('struct').unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.server.launcher.config['app_uid']:
                raise ValueError('Launcher caller UID is not authorized')
            self.request.settimeout(10)
            line = self.rfile.readline(16385)
            if len(line) > 16384:
                raise ValueError('Oversized launcher request')
            result = self.server.launcher.handle(json.loads(line))
        except Exception:
            result = {'ok': False, 'error': {'code': 'launcher_failed', 'message': 'Launcher refused or failed the operation'}}
        self.wfile.write(json.dumps(result).encode() + b'\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/bookmark-capture/launcher.json')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Capture launcher requires its root-owned system service')
    config_path = Path(args.config)
    if config_path.stat().st_uid != 0 or config_path.stat().st_mode & 0o022:
        raise SystemExit('Launcher config must be root-owned and not group/world writable')
    config = json.loads(config_path.read_text())
    path = Path(config['socket_path'])
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(path.parent, 0, config['app_gid'])
    os.chmod(path.parent, 0o750)
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit('Refusing to replace a non-socket launcher path')
        path.unlink()
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        last_reap = 0

        def service_actions(self):
            if time.monotonic() - self.last_reap >= 60 and self.launcher.execution_lock.acquire(blocking=False):
                try:
                    self.launcher.reap()
                    self.last_reap = time.monotonic()
                finally:
                    self.launcher.execution_lock.release()
    with Server(str(path), Handler) as server:
        server.launcher = Launcher(config)
        server.launcher.initialize_storage()
        os.chown(path, 0, config['app_gid'])
        os.chmod(path, 0o660)
        server.serve_forever()


if __name__ == '__main__':
    main()
