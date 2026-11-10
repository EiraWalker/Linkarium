"""Local scratch fixtures and fake Docker only; never contacts production."""
import hashlib
import importlib.util
import os
import io
import json
import tarfile
import sqlite3
import gzip
import copy
from contextlib import closing, contextmanager
from pathlib import Path
import tempfile
import unittest
from collections.abc import Callable
from unittest.mock import patch
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[2] / 'deploy/cloud/storage_upgrade.py'
SCRATCH = Path(os.environ.get('HERMES_TEST_SCRATCH') or os.environ.get('TMPDIR') or tempfile.gettempdir())


def load_operator():
    spec = importlib.util.spec_from_file_location('storage_upgrade', SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocker:
    """Protocol fixture; only subprocess I/O is replaced, not deployment logic."""
    def __init__(self, root, old, args):
        self.root, self.old, self.args = root, old, args
        self.calls, self.failure = [], False
        self.on_up: Callable[[str], None] | None = None
        self.exists, self.remove_on_failure = True, False
        self.command_error = RuntimeError
        self.old_id, self.new_id = 'sha256:' + '1' * 64, 'sha256:' + '2' * 64
        self.current_id = self.old_id
        self.running = True
        self.config_files = str(old / 'deploy/cloud/compose.yaml')
        self.images = {self.old_id: self.image(self.old_id, 'v0.2.0', args.expected_base)}
        self.environment = {'BOOKMARK_DATA_ROOT': '/data', 'BOOKMARK_PASSKEY_ORIGIN': 'https://fixture.invalid'}
        self.code_environment = False
        self.origin_uses_version = False
        self.ports = {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8000'}]}
        self.runtime_ports = copy.deepcopy(self.ports)
        self.host_ports = copy.deepcopy(self.ports)
        self.runtime_env = self.effective_env(self.old_id)

    def effective_env(self, image_id):
        values = dict(item.split('=', 1) for item in self.images[image_id]['env'])
        values.update(self.resolved_environment())
        return [key + '=' + value for key, value in values.items() if value is not None]

    def resolved_environment(self, deployment=None):
        # Fixture interpolation for the two deployment values only. Real Compose
        # resolution is exercised separately against an isolated local daemon.
        values = dict(line.split('=', 1) for line in (self.root / 'production.env').read_text().splitlines()
                      if line and not line.startswith('#') and '=' in line)
        if deployment is not None:
            values.update(LINKARIUM_VERSION=deployment['version'], LINKARIUM_REVISION=deployment['commit'])
        result = dict(self.environment)
        if self.code_environment:
            result.update({key: values[key] for key in ('LINKARIUM_VERSION', 'LINKARIUM_REVISION')})
        if self.origin_uses_version:
            result['BOOKMARK_PASSKEY_ORIGIN'] = 'https://' + values['LINKARIUM_VERSION'] + '.invalid'
        return result

    @staticmethod
    def image(image_id, version, commit):
        return {'id': image_id, 'user': '10001:10001', 'env': ['PATH=/usr/local/bin:/usr/bin', 'PYTHONUNBUFFERED=1'], 'labels': {
            'org.opencontainers.image.version': version, 'org.opencontainers.image.revision': commit}}

    def __call__(self, args, **options):
        self.calls.append(list(args))
        if args[:2] == ['image', 'inspect']:
            if args[3] == '{{json .Config.Env}}':
                return json.dumps(self.images[args[-1]]['env']).encode()
            return json.dumps(self.images[args[-1]]).encode()
        if args[:2] == ['container', 'inspect']:
            if not self.exists:
                raise self.command_error('DOCKER_COMMAND_FAILED')
            image = self.images[self.current_id]
            return json.dumps({'id': 'container-fixture', 'image_id': self.current_id, 'running': self.running,
                'health': 'healthy' if self.running else 'unhealthy', 'labels': {**image['labels'],
                'com.docker.compose.project': 'linkarium', 'com.docker.compose.service': 'app',
                'com.docker.compose.project.config_files': self.config_files},
                'env': self.runtime_env, 'ports': self.runtime_ports, 'host_ports': self.host_ports,
                'mounts': [{'Type': 'bind', 'Source': str(self.root / 'data'), 'Destination': '/data', 'RW': True}]}).encode()
        if args[0] == 'build':
            image = self.image(self.new_id, self.args.version, self.args.commit)
            image['env'] = list(self.images[self.old_id]['env'])
            self.images[self.new_id] = image
            self.images['linkarium:' + self.args.version] = image
            return b''
        if args[0] == 'run':
            return b'{"verified": true}'
        if args[0] == 'compose':
            configs = [Path(args[i + 1]) for i, value in enumerate(args) if value == '-f']
            if 'ps' in args:
                return b'container-fixture\n' if self.exists else b''
            if 'config' in args:
                return json.dumps({'services': {'app': {'image': 'linkarium:v0.2.0',
                    'environment': self.resolved_environment(options.get('deployment')),
                    'volumes': [{'type': 'bind', 'source': str(self.root / 'data'), 'target': '/data'}],
                    'ports': [{'host_ip': '127.0.0.1', 'target': 8000, 'published': '8000', 'protocol': 'tcp'}]}}}).encode()
            if 'stop' in args:
                self.running = False
                return b''
            if 'up' in args:
                self.exists = True
                self.current_id = json.loads(configs[-1].read_bytes())['services']['app']['image']
                self.config_files = ','.join(map(str, configs))
                self.running = True
                self.runtime_env = self.effective_env(self.current_id)
                self.runtime_ports = copy.deepcopy(self.ports)
                self.host_ports = copy.deepcopy(self.ports)
                if self.on_up:
                    self.on_up(self.current_id)
                if self.failure and self.current_id == self.new_id:
                    if self.remove_on_failure:
                        self.exists = False
                    raise RuntimeError('FAKE_NEW_START_FAILED')
                return b''
        raise AssertionError('Unexpected fake Docker command: ' + repr(args))


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCE.is_file(), 'storage deployment operator is missing')
        self.op = load_operator()
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='storage-upgrade-test-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_path_rejects_parent_traversal_and_outside_scope(self):
        (self.root / 'data').mkdir()
        for path in (self.root / 'data/../escape', self.root.parent / 'escape'):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'PATH'):
                self.op.safe(path, self.root / 'data')
        self.assertEqual(self.op.safe(self.root / 'data', self.root), self.root / 'data')
    def test_env_replaces_only_two_values_preserving_all_other_bytes(self):
        original = (b'# private fixture\r\nTOKEN=\xff$secret # leave exactly\r\n'
                    b'export LINKARIUM_VERSION = "v0.2.0"  # keep comment\r\n'
                    b'LINKARIUM_REVISION=' + b'1' * 40 + b'\nEND=no-final-newline')
        self.assertTrue(hasattr(self.op, 'updated_env'), 'byte preserving environment update missing')
        updated = self.op.updated_env(original, 'v0.2.1-storage.1', '2' * 40)
        self.assertEqual(updated, original.replace(b'"v0.2.0"', b'"v0.2.1-storage.1"').replace(b'1' * 40, b'2' * 40))
        for bad in (original + b'\nLINKARIUM_VERSION=v9', original.replace(b'LINKARIUM_REVISION=', b'OTHER=')):
            with self.assertRaisesRegex(RuntimeError, 'ENV'):
                self.op.updated_env(bad, 'v0.2.1', '2' * 40)
        for version in ('../../evil', 'v1\nRUN false', 'v1.2.3;false'):
            with self.assertRaisesRegex(RuntimeError, 'ARGUMENT'):
                self.op.updated_env(original, version, '2' * 40)
    def release(self, version='v0.2.1', commit='2' * 40):
        root = self.root / 'releases' / ('Linkarium-' + version + '-deploy')
        root.mkdir(parents=True)
        files = {'backend/supervisor.py': b'# fixture', 'backend/runtime/migrations/0001.py': b'# runtime schema',
                 'backend/library/migrations/0001.py': b'# library schema',
                 'frontend/dist/index.html': b'<html>fixture</html>', 'LICENSE': b'fixture license',
                 'deploy/cloud/Dockerfile': b'FROM fixture', 'deploy/cloud/compose.yaml': b'fixture compose',
                 'deploy/requirements.lock': b'# fixture pinned requirements'}
        for name, content in files.items():
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
        (root / 'release.json').write_text(json.dumps({'application': 'Linkarium', 'version': version,
            'commit': commit, 'git_tree': 'a' * 40, 'prebuilt_frontend': True, 'includes_personal_data': False}))
        self.manifest(root)
        return root

    def manifest(self, root):
        (root / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' +
            p.relative_to(root).as_posix() + '\n' for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS'))

    def test_release_hashes_exact_file_set_and_expected_revision(self):
        self.assertTrue(hasattr(self.op, 'verify_release'), 'release verification missing')
        root = self.release()
        self.assertEqual(self.op.verify_release(root, 'v0.2.1', '2' * 40)['commit'], '2' * 40)
        with self.assertRaisesRegex(RuntimeError, 'METADATA'):
            self.op.verify_release(root, 'v0.2.1', '3' * 40)
        (root / 'extra').write_bytes(b'unmanifested')
        with self.assertRaisesRegex(RuntimeError, 'FILE_SET'):
            self.op.verify_release(root, 'v0.2.1', '2' * 40)
        (root / 'extra').unlink()
        (root / 'backend/supervisor.py').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'HASH'):
            self.op.verify_release(root, 'v0.2.1', '2' * 40)

    def test_extract_rejects_traversal_links_duplicates_and_wrong_root(self):
        self.assertTrue(hasattr(self.op, 'extract_package'), 'safe package extraction missing')
        root = self.release()
        package = self.root / 'good.tar.gz'
        with tarfile.open(package, 'w:gz') as archive:
            archive.add(root, arcname=root.name)
        destination = self.root / 'unpacked'
        destination.mkdir()
        sha = hashlib.sha256(package.read_bytes()).hexdigest()
        extracted = self.op.extract_package(package, sha, destination, 'v0.2.1', '2' * 40)
        self.assertEqual(extracted.name, root.name)
        with self.assertRaisesRegex(RuntimeError, 'PACKAGE_HASH'):
            self.op.extract_package(package, '0' * 64, destination, 'v0.2.1', '2' * 40)
        for index, (name, kind) in enumerate(((root.name + '/../escape', 'file'),
                ('/absolute', 'file'), ('other/file', 'file'), (root.name + '/symlink', 'symlink'),
                (root.name + '/hardlink', 'hardlink'), (root.name + '/duplicate', 'duplicate'),
                (root.name + '/./ambiguous', 'file'), (root.name + '/back\\slash', 'file'))):
            with self.subTest(name=name):
                bad = self.root / f'bad-{index}.tar.gz'
                with tarfile.open(bad, 'w:gz') as archive:
                    member = tarfile.TarInfo(name)
                    if kind in ('symlink', 'hardlink'):
                        member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                        member.linkname = '../outside'
                    archive.addfile(member, io.BytesIO(b''))
                    if kind == 'duplicate':
                        archive.addfile(member, io.BytesIO(b''))
                out = self.root / f'out-{index}'
                out.mkdir()
                with self.assertRaisesRegex(RuntimeError, 'PACKAGE_UNSAFE'):
                    self.op.extract_package(bad, hashlib.sha256(bad.read_bytes()).hexdigest(), out, 'v0.2.1', '2' * 40)
                self.assertEqual(list(out.iterdir()), [])
    def source_data(self):
        data = self.root / 'data'
        generation = data / 'generations/initial'
        generation.mkdir(parents=True)
        (data / 'runtime').mkdir()
        (data / 'runtime/supervisor.lock').write_bytes(b'')
        (data / 'runtime/secret-key').write_bytes(b'private-fixture')
        (data / 'backups').mkdir()
        archive = data / 'backups/completed.bookmarklib.zip'
        archive.write_bytes(b'completed-fixture-archive' * 2048)
        receipt = {'package_path': '/data/backups/' + archive.name, 'byte_size': archive.stat().st_size,
                   'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
        with closing(sqlite3.connect(data / 'state.sqlite3')) as db:
            db.executescript("CREATE TABLE runtime_runtimestate(id,active_generation,maintenance_mode);"
                "INSERT INTO runtime_runtimestate VALUES(1,'initial',0);"
                "CREATE TABLE runtime_maintenanceoperation(kind,state,result);"
                "CREATE TABLE auth_user(id,name); INSERT INTO auth_user VALUES(1,'private-fixture');")
            db.execute('INSERT INTO runtime_maintenanceoperation VALUES(?,?,?)', ('backup', 'succeeded', json.dumps(receipt)))
            db.commit()
        with closing(sqlite3.connect(generation / 'library.sqlite3')) as db:
            db.executescript('CREATE TABLE library_bookmark(id); INSERT INTO library_bookmark VALUES(1);'
                'CREATE TABLE library_snapshotversion(id); CREATE TABLE library_artifact(id);'
                'CREATE TABLE library_job(state);')
        payload = b'<html>private fixture raw artifact</html>'
        sha = hashlib.sha256(payload).hexdigest()
        raw = generation / 'artifacts' / sha[:2] / (sha + '.html')
        raw.parent.mkdir(parents=True)
        raw.write_bytes(payload)
        compressed = raw.with_suffix('.html.gz')
        compressed.write_bytes(gzip.compress(payload, mtime=0))
        (data / 'work').mkdir()
        (data / 'work/unfinished.bookmarklib.zip').write_bytes(b'in-progress')
        return data, raw, compressed, archive

    def test_cold_checkpoint_links_completed_zip_and_raw_gz_but_copies_sqlite(self):
        self.assertTrue(hasattr(self.op, 'cold_snapshot'), 'cold checkpoint missing')
        data, raw, compressed, archive = self.source_data()
        journal = self.root / 'ops/checkpoint'
        journal.mkdir(parents=True)
        # The archive already has an old cold alias. A new alias must not copy it.
        old = self.root / 'old-archive-alias'
        os.link(archive, old)
        plan = self.op.plan_data(data, journal)
        expected = sum(p.stat().st_size for p in data.rglob('*') if p.is_file() and p not in (raw, compressed, archive))
        self.assertEqual(plan['copy_bytes'], expected)
        manifest = self.op.cold_snapshot(data, journal)
        self.assertEqual(manifest['copied_bytes'], expected)
        for source in (raw, compressed, archive):
            target = journal / 'data' / source.relative_to(data)
            self.assertEqual((source.stat().st_dev, source.stat().st_ino), (target.stat().st_dev, target.stat().st_ino))
        for name in ('state.sqlite3', 'generations/initial/library.sqlite3', 'runtime/secret-key', 'work/unfinished.bookmarklib.zip'):
            self.assertNotEqual((data / name).stat().st_ino, (journal / 'data' / name).stat().st_ino)
            self.assertEqual((data / name).read_bytes(), (journal / 'data' / name).read_bytes())
        self.assertFalse(manifest['independent_disaster_backup'])
        self.op.verify_cold(journal)
        (journal / 'data/runtime/secret-key').write_bytes(b'corrupt-fixture')
        with self.assertRaisesRegex(RuntimeError, 'COLD'):
            self.op.verify_cold(journal)

    def test_cold_space_gate_counts_nonfinal_zip_and_fails_without_writing(self):
        self.assertTrue(hasattr(self.op, 'plan_data'), 'cold space planning missing')
        data, *_ = self.source_data()
        journal = self.root / 'ops/checkpoint'
        journal.mkdir(parents=True)
        unknown = data / 'backups/unknown.bookmarklib.zip'
        unknown.write_bytes(b'not-completed' * 1024)
        plan = self.op.plan_data(data, journal)
        self.assertEqual(plan['entries']['backups/unknown.bookmarklib.zip']['kind'], 'copy')
        usage = type('Usage', (), {'free': plan['copy_bytes'] + self.op.RESERVE_BYTES - 1})()
        with patch.object(self.op.shutil, 'disk_usage', return_value=usage):
            with self.assertRaisesRegex(RuntimeError, 'SPACE'):
                self.op.cold_snapshot(data, journal)
        self.assertFalse((journal / 'data').exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink/FIFO fixture; run on Linux')
    def test_cold_rejects_linked_paths_and_special_files(self):
        self.assertTrue(hasattr(self.op, 'plan_data'), 'cold space planning missing')
        data, *_ = self.source_data()
        journal = self.root / 'checkpoint'
        journal.mkdir()
        link = data / 'backups/alias.bookmarklib.zip'
        link.symlink_to(data / 'state.sqlite3')
        with self.assertRaisesRegex(RuntimeError, 'LINK'):
            self.op.plan_data(data, journal)
        link.unlink()
        try:
            getattr(os, 'mkfifo')(data / 'fifo')
        except OSError as error:
            if error.errno == 95:
                self.skipTest('scratch filesystem lacks FIFOs; run container tmpfs/ext4 fixture')
            raise
        with self.assertRaisesRegex(RuntimeError, 'SPECIAL'):
            self.op.plan_data(data, journal)
    def test_identity_summary_includes_devices_sessions_passkeys_without_rows(self):
        self.assertTrue(hasattr(self.op, 'identity'), 'identity continuity gate missing')
        data, *_ = self.source_data()
        with closing(sqlite3.connect(data / 'state.sqlite3')) as db:
            for table in ('runtime_executiondevice', 'django_session', 'runtime_passkeycredential',
                          'runtime_authstate', 'runtime_filekeycredential', 'runtime_providersecret'):
                db.execute('CREATE TABLE ' + table + '(id, secret)')
                db.execute('INSERT INTO ' + table + ' VALUES(1,?)', ('secret-not-for-output',))
            db.commit()
        before = self.op.identity(data)
        for table in ('runtime_executiondevice', 'django_session', 'runtime_passkeycredential'):
            self.assertEqual(before['tables'][table]['count'], 1)
        self.assertNotIn('secret-not-for-output', json.dumps(before))
        with closing(sqlite3.connect(data / 'state.sqlite3')) as db:
            db.execute('DELETE FROM runtime_executiondevice')
            db.commit()
        self.assertNotEqual(before, self.op.identity(data))

    def test_code_rollback_guard_accepts_new_business_rows_but_not_schema_or_compression(self):
        self.assertTrue(hasattr(self.op, 'rollback_guard'), 'code rollback guard missing')
        data, raw, compressed, _ = self.source_data()
        before = self.op.rollback_guard(data)
        with closing(sqlite3.connect(data / 'generations/initial/library.sqlite3')) as db:
            db.execute('INSERT INTO library_bookmark VALUES(2)')
            db.commit()
        self.assertEqual(before, self.op.rollback_guard(data))
        compressed.write_bytes(compressed.read_bytes() + b'changed-representation')
        self.assertNotEqual(before, self.op.rollback_guard(data))
        before_schema = self.op.rollback_guard(data)
        with closing(sqlite3.connect(data / 'generations/initial/library.sqlite3')) as db:
            db.execute('ALTER TABLE library_bookmark ADD COLUMN extra')
            db.commit()
        self.assertNotEqual(before_schema, self.op.rollback_guard(data))
    def deployment(self):
        old = self.release('v0.2.0', '1' * 40)
        new = self.release()
        package = self.root / 'package.tar.gz'
        with tarfile.open(package, 'w:gz') as archive:
            archive.add(new, arcname=new.name)
        new.rename(self.root / 'packaged-source')
        (self.root / 'current').symlink_to(old, target_is_directory=True)
        (self.root / 'ops').mkdir()
        data, *_ = self.source_data()
        with closing(sqlite3.connect(data / 'state.sqlite3')) as db:
            for name in ('runtime_authstate', 'runtime_passkeycredential', 'runtime_filekeycredential',
                         'runtime_providersecret', 'runtime_executiondevice', 'django_session'):
                db.execute('CREATE TABLE ' + name + '(id, value)')
            db.commit()
        original = b'# keep\r\nLINKARIUM_VERSION=v0.2.0\r\nLINKARIUM_REVISION=' + b'1' * 40 + b'\nPRIVATE_FIXTURE=opaque-$bytes\n'
        (self.root / 'production.env').write_bytes(original)
        args = SimpleNamespace(version='v0.2.1', commit='2' * 40, expected_base='1' * 40,
                               package=package, package_sha256=hashlib.sha256(package.read_bytes()).hexdigest())
        fake = FakeDocker(self.root, old, args)
        fake.command_error = self.op.UpgradeError
        for name, value in (('BASE', self.root), ('docker', fake)):
            patcher = patch.object(self.op, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return args, fake, old, original

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_offline_pins_current_image_and_removes_stale_directories(self):
        self.assertTrue(hasattr(self.op, 'prepare'), 'prepare phase missing')
        args, fake, old, _ = self.deployment()
        receipt = self.op.prepare(args)
        self.assertEqual(receipt['old_image_id'], fake.old_id)
        self.assertEqual(receipt['new_image_id'], fake.new_id)
        self.assertEqual((self.root / 'current').resolve(), old)
        journal = self.op.journal_path(args)
        text = (journal / 'Dockerfile.offline').read_text()
        self.assertIn('FROM ' + fake.old_id, text)
        self.assertIn('USER 0:0', text)
        self.assertLess(text.index('RUN rm -rf /app/backend /app/frontend'), text.index('COPY backend/'))
        self.assertTrue(text.rstrip().endswith('USER 10001:10001'))
        builds = [c for c in fake.calls if c[0] == 'build']
        self.assertEqual(len(builds), 1)
        self.assertIn('--pull=false', builds[0])
        self.assertIn('--network=none', builds[0])
        self.assertNotIn('pip', text)
        self.assertFalse(any('stop' in c for c in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_refuses_expected_base_or_dependency_or_schema_drift(self):
        self.assertTrue(hasattr(self.op, 'prepare'), 'prepare phase missing')
        args, fake, old, _ = self.deployment()
        args.expected_base = '3' * 40
        with self.assertRaisesRegex(RuntimeError, 'BASE'):
            self.op.prepare(args)
        args.expected_base = '1' * 40
        (old / 'deploy/requirements.lock').write_bytes(b'different old deps')
        self.manifest(old)
        with self.assertRaisesRegex(RuntimeError, 'DEPENDENCIES'):
            self.op.prepare(args)
        self.assertFalse(any(c[0] == 'build' for c in fake.calls))
    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_activate_and_verify_preserve_identity_and_only_change_two_env_values(self):
        self.assertTrue(hasattr(self.op, 'activate'), 'activation phase missing')
        args, fake, old, original = self.deployment()
        prepared = self.op.prepare(args)
        calls_before = len(fake.calls)
        with patch.object(self.op, 'loopback_ready', return_value=True):
            result = self.op.activate(args)
            verified = self.op.verify(args)
        self.assertTrue(verified['ready'])
        self.assertTrue(verified['identity_continuity'])
        self.assertTrue(verified['cold_checkpoint_verified'])
        self.assertEqual(verified['counts']['bookmarks'], 1)
        self.assertEqual(verified['image_id'], fake.new_id)
        self.assertEqual((self.root / 'current').resolve(), Path(prepared['new_release']))
        self.assertEqual((self.root / 'production.env').read_bytes(), original.replace(b'v0.2.0', b'v0.2.1').replace(b'1' * 40, b'2' * 40))
        self.assertEqual((self.op.journal_path(args) / 'production.env').read_bytes(), original)
        self.assertNotIn('opaque-$bytes', json.dumps(result))
        for call in fake.calls[calls_before:]:
            self.assertNotIn('build', call)
            if 'up' in call:
                for flag in ('--no-build', '--pull', 'never', '--force-recreate', '--wait', '--no-deps'):
                    self.assertIn(flag, call)
                self.assertEqual(call[-1], 'app')
            if 'stop' in call:
                self.assertEqual(call[-1], 'app')
        journal = self.op.journal_path(args)
        self.assertEqual(journal.stat().st_mode & 0o777, 0o700)
        self.assertEqual((journal / 'production.env').stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_activate_space_or_env_drift_fails_before_stop(self):
        self.assertTrue(hasattr(self.op, 'activate'), 'activation phase missing')
        args, fake, _, original = self.deployment()
        self.op.prepare(args)
        usage = SimpleNamespace(free=1)
        with patch.object(self.op.shutil, 'disk_usage', return_value=usage):
            with self.assertRaisesRegex(RuntimeError, 'SPACE'):
                self.op.activate(args)
        (self.root / 'production.env').write_bytes(original + b'# externally changed\n')
        with self.assertRaisesRegex(RuntimeError, 'DRIFT'):
            self.op.activate(args)
        self.assertFalse(any('stop' in c for c in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_failed_new_start_rolls_back_code_without_rewinding_auth_or_business(self):
        self.assertTrue(hasattr(self.op, 'activate'), 'activation phase missing')
        args, fake, old, original = self.deployment()
        self.op.prepare(args)
        fake.failure = True
        def write_latest(image_id):
            if image_id != fake.new_id:
                return
            with closing(sqlite3.connect(self.root / 'data/state.sqlite3')) as db:
                db.execute('DELETE FROM auth_user')
                db.commit()
            with closing(sqlite3.connect(self.root / 'data/generations/initial/library.sqlite3')) as db:
                db.execute('INSERT INTO library_bookmark VALUES(2)')
                db.commit()
        fake.on_up = write_latest
        with patch.object(self.op, 'loopback_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'START_FAILED'):
                self.op.activate(args)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual((self.root / 'production.env').read_bytes(), original)
        self.assertEqual(fake.current_id, fake.old_id)
        with closing(sqlite3.connect(self.root / 'data/state.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auth_user').fetchone()[0], 0)
        with closing(sqlite3.connect(self.root / 'data/generations/initial/library.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM library_bookmark').fetchone()[0], 2)
        with closing(sqlite3.connect(self.op.journal_path(args) / 'data/state.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auth_user').fetchone()[0], 1)
        self.assertEqual(list(self.root.glob('data.failed*')), [])
    @unittest.skipUnless(os.name == 'posix', 'POSIX directory permissions')
    def test_extracted_code_directories_are_traversable_by_nonroot_image_user(self):
        root = self.release()
        package = self.root / 'package.tar.gz'
        with tarfile.open(package, 'w:gz') as archive:
            archive.add(root, arcname=root.name)
        out = self.root / 'out'
        out.mkdir()
        extracted = self.op.extract_package(package, hashlib.sha256(package.read_bytes()).hexdigest(), out, 'v0.2.1', '2' * 40)
        self.assertEqual((extracted / 'backend').stat().st_mode & 0o005, 0o005)

    def test_cold_backup_rejects_uncheckpointed_wal_without_modifying_it(self):
        self.assertTrue(hasattr(self.op, 'require_quiescent_sqlite'), 'cold WAL safety gate missing')
        data, *_ = self.source_data()
        wal = data / 'state.sqlite3-wal'
        wal.write_bytes(b'uncheckpointed-fixture')
        before = wal.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'WAL'):
            self.op.require_quiescent_sqlite(data)
        self.assertEqual(wal.read_bytes(), before)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_failed_start_refuses_code_rollback_after_compression(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        fake.failure = True
        def compress_on_start(image_id):
            if image_id == fake.new_id:
                path = next((self.root / 'data/generations').rglob('*.html.gz'))
                path.write_bytes(path.read_bytes() + b'changed')
        fake.on_up = compress_on_start
        with self.assertRaisesRegex(RuntimeError, 'ROLLBACK_REFUSED_OR_FAILED'):
            self.op.activate(args)
        self.assertFalse(fake.running)
        self.assertEqual(fake.current_id, fake.new_id)
        self.assertEqual(json.loads((self.op.journal_path(args) / 'phase.json').read_bytes())['phase'], 'manual_recovery_required')

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_verify_detects_session_change_without_restoring_any_auth_data(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        with patch.object(self.op, 'loopback_ready', return_value=True):
            self.op.activate(args)
        with closing(sqlite3.connect(self.root / 'data/state.sqlite3')) as db:
            db.execute("INSERT INTO django_session VALUES(1,'new-session-fixture')")
            db.commit()
        before = (self.root / 'data/state.sqlite3').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'IDENTITY'):
            self.op.verify(args)
        self.assertEqual((self.root / 'data/state.sqlite3').read_bytes(), before)
        self.assertTrue(fake.running)

    def test_cli_requires_explicit_revision_base_and_never_accepts_base_override(self):
        self.assertTrue(hasattr(self.op, 'parse_args'), 'CLI missing')
        argv = ['verify', '--version', 'v0.2.1', '--commit', '2' * 40, '--expected-base', '1' * 40]
        self.assertEqual(self.op.parse_args(argv).phase, 'verify')
        with patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            self.op.parse_args(argv + ['--base', str(self.root)])
        with patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            self.op.parse_args(['activate', '--version', 'v0.2.1', '--commit', 'bad'])
        with patch.object(self.op, 'BASE', self.root):
            with self.assertRaisesRegex(RuntimeError, 'FIXED_BASE'):
                self.op.require_host()
    @unittest.skipUnless(os.name == 'posix', 'POSIX root-private permission fixture')
    def test_extraction_under_private_umask_does_not_make_image_code_unreadable(self):
        root = self.release()
        package = self.root / 'private.tar.gz'
        # Omit explicit parent entries: implicit dirs must also be safe.
        with tarfile.open(package, 'w:gz') as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.add(path, arcname=root.name + '/' + path.relative_to(root).as_posix())
        out = self.root / 'private-out'
        out.mkdir()
        previous = os.umask(0o077)
        try:
            extracted = self.op.extract_package(package, hashlib.sha256(package.read_bytes()).hexdigest(), out, 'v0.2.1', '2' * 40)
        finally:
            os.umask(previous)
        self.assertEqual((extracted / 'backend').stat().st_mode & 0o005, 0o005)

    @unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0, 'requires root Linux fixture')
    def test_root_env_permissions_are_required(self):
        path = self.root / 'private.env'
        path.write_bytes(b'private-fixture')
        path.chmod(0o600)
        self.op.require_root_owned(path, 0o600)
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'MODE'):
            self.op.require_root_owned(path, 0o600)

    def test_docker_failure_and_main_do_not_expose_secrets(self):
        result = SimpleNamespace(returncode=1, stdout=b'private-fixture-token', stderr=b'private-fixture-password')
        with patch.object(self.op.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, '^DOCKER_COMMAND_FAILED$'):
                self.op.docker(['compose', 'config'])
        environment = run.call_args.kwargs['env']
        self.assertEqual(environment['DOCKER_BUILDKIT'], '0')
        self.assertNotIn('BOOKMARK_HOST_DATA', environment)
        self.assertIn('unix:///var/run/docker.sock', run.call_args.args[0])
        output = io.StringIO()
        with patch.object(self.op, 'require_host', side_effect=RuntimeError('private-fixture-password')), patch('sys.stdout', output):
            result = self.op.main(['verify', '--version', 'v0.2.1', '--commit', '2' * 40, '--expected-base', '1' * 40])
        self.assertEqual(result, 1)
        self.assertNotIn('private-fixture', output.getvalue())
    def test_package_changed_after_outer_hash_is_not_accepted(self):
        root = self.release()
        package, changed = self.root / 'original.tar.gz', self.root / 'changed.tar.gz'
        with tarfile.open(package, 'w:gz') as archive:
            archive.add(root, arcname=root.name)
        sha = hashlib.sha256(package.read_bytes()).hexdigest()
        (root / 'backend/supervisor.py').write_bytes(b'changed-after-outer-sha')
        self.manifest(root)
        with tarfile.open(changed, 'w:gz') as archive:
            archive.add(root, arcname=root.name)
        out = self.root / 'out'
        out.mkdir()
        original_open = tarfile.open
        def swap(*args, **kwargs):
            package.write_bytes(changed.read_bytes())
            return original_open(*args, **kwargs)
        with patch.object(self.op.tarfile, 'open', side_effect=swap):
            with self.assertRaisesRegex(RuntimeError, 'PACKAGE'):
                self.op.extract_package(package, sha, out, 'v0.2.1', '2' * 40)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_refuses_source_drift_during_offline_build(self):
        args, fake, old, _ = self.deployment()
        def drifting(arguments, **kwargs):
            result = fake(arguments, **kwargs)
            if arguments[0] == 'build':
                (old / 'backend/supervisor.py').write_bytes(b'concurrent-source-drift')
                self.manifest(old)
            return result
        with patch.object(self.op, 'docker', side_effect=drifting):
            with self.assertRaisesRegex(RuntimeError, 'DRIFT'):
                self.op.prepare(args)
        self.assertFalse((self.op.journal_path(args) / 'prepared.json').exists())

    def test_loopback_ready_uses_actual_http_and_rejects_redirect(self):
        import http.server
        import threading
        class Handler(http.server.BaseHTTPRequestHandler):
            status = 200
            def do_GET(self):
                self.send_response(self.status)
                if self.status == 302:
                    self.send_header('Location', 'http://example.invalid/secret')
                self.end_headers()
                self.wfile.write(b'{"ready":true}')
            def log_message(self, format, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.op.loopback_ready(server.server_address[1]))
            Handler.status = 302
            with self.assertRaisesRegex(RuntimeError, 'REDIRECT'):
                self.op.loopback_ready(server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_rollback_restarts_old_image_if_failed_recreate_removed_container(self):
        args, fake, old, original = self.deployment()
        self.op.prepare(args)
        fake.failure = fake.remove_on_failure = True
        with patch.object(self.op, 'loopback_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'FAKE_NEW_START_FAILED'):
                self.op.activate(args)
        self.assertTrue(fake.running)
        self.assertTrue(fake.exists)
        self.assertEqual(fake.current_id, fake.old_id)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual((self.root / 'production.env').read_bytes(), original)
    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_unexpected_startup_schema_change_is_not_verified_or_rolled_back(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        def change_schema(image_id):
            if image_id == fake.new_id:
                with closing(sqlite3.connect(self.root / 'data/generations/initial/library.sqlite3')) as db:
                    db.execute('ALTER TABLE library_bookmark ADD COLUMN unexpected')
                    db.commit()
        fake.on_up = change_schema
        with patch.object(self.op, 'loopback_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'ROLLBACK_REFUSED_OR_FAILED'):
                self.op.activate(args)
        self.assertFalse(fake.running)
        self.assertEqual(fake.current_id, fake.new_id)
    @unittest.skipUnless(os.name == 'posix', 'POSIX O_NOFOLLOW semantics')
    def test_digest_opens_regular_file_with_no_follow(self):
        file = self.root / 'hash-fixture'
        file.write_bytes(b'fixture')
        original_open = os.open
        with patch.object(self.op.os, 'open', wraps=original_open) as opened:
            self.assertEqual(self.op.digest(file), hashlib.sha256(b'fixture').hexdigest())
        self.assertTrue(opened.called, 'hashing must use an explicit no-follow file descriptor')
        self.assertTrue(opened.call_args.args[1] & getattr(os, 'O_NOFOLLOW'))


    def runtime_fixture(self):
        old = self.release('v0.2.0', '1' * 40)
        (self.root / 'production.env').write_text('LINKARIUM_VERSION=v0.2.0\nLINKARIUM_REVISION=' + '1' * 40 + '\n')
        args = SimpleNamespace(version='v0.2.1', commit='2' * 40, expected_base='1' * 40)
        fake = FakeDocker(self.root, old, args)
        for name, value in (('BASE', self.root), ('docker', fake)):
            patcher = patch.object(self.op, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake, old, json.loads((old / 'release.json').read_bytes())

    def test_runtime_requires_complete_unique_environment_and_actual_ports(self):
        fake, old, metadata = self.runtime_fixture()
        good_env, good_ports = list(fake.runtime_env), copy.deepcopy(fake.ports)
        cases = [
            ('missing_env', 'runtime_env', None),
            ('missing_key', 'runtime_env', good_env[:-1]),
            ('duplicate', 'runtime_env', good_env + good_env[-1:]),
            ('extra_auth', 'runtime_env', good_env + ['BOOKMARK_AUTH_DEBUG=1']),
            ('malformed_env', 'runtime_env', good_env + ['UNSET']),
            ('missing_ports', 'runtime_ports', None),
            ('missing_publication', 'runtime_ports', {}),
            ('duplicate_publication', 'runtime_ports', {'8000/tcp': good_ports['8000/tcp'] * 2}),
            ('extra_publication', 'runtime_ports', {**good_ports, '9000/tcp': good_ports['8000/tcp']}),
            ('host_wildcard', 'host_ports', {'8000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8000'}]}),
            ('missing_host_ports', 'host_ports', None),
        ]
        for name, attr, value in cases:
            with self.subTest(case=name):
                with patch.object(fake, attr, value), self.assertRaisesRegex(RuntimeError, 'CONFIG'):
                    self.op.require_container(old, metadata)
        self.op.require_container(old, metadata)

    def test_runtime_includes_actual_image_defaults_and_compose_overrides(self):
        fake, old, metadata = self.runtime_fixture()
        fake.images[fake.old_id]['env'] += ['BOOKMARK_TRUST_PROXY=0', 'DEFAULT_ONLY=synthetic-image-default']
        fake.environment['BOOKMARK_TRUST_PROXY'] = '1'
        fake.runtime_env = fake.effective_env(fake.old_id)
        fake.runtime_env.reverse()  # Docker list order is not effective configuration.
        self.op.require_container(old, metadata)

    def test_matching_but_unexpected_code_version_is_rejected(self):
        fake, old, metadata = self.runtime_fixture()
        fake.environment['LINKARIUM_VERSION'] = 'v9.9.9'
        fake.runtime_env = fake.effective_env(fake.old_id)
        with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
            self.op.require_container(old, metadata)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_rejects_runtime_auth_environment_drift(self):
        args, fake, _, _ = self.deployment()
        fake.environment['BOOKMARK_PASSKEY_ORIGIN'] = 'https://disk-only.invalid'
        with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
            self.op.prepare(args)
        self.assertFalse(any('stop' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_rejects_actual_published_port_drift(self):
        args, fake, _, _ = self.deployment()
        fake.runtime_ports['8000/tcp'][0]['HostIp'] = '0.0.0.0'
        with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
            self.op.prepare(args)
        self.assertFalse(any('stop' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_startup_count_loss_is_not_active_and_never_restores_business_database(self):
        args, fake, old, _ = self.deployment()
        self.op.prepare(args)
        library = self.root / 'data/generations/initial/library.sqlite3'
        def lose_bookmark(image_id):
            if image_id == fake.new_id:
                with closing(sqlite3.connect(library)) as db:
                    db.execute('DELETE FROM library_bookmark')
                    db.commit()
        fake.on_up = lose_bookmark
        with patch.object(self.op, 'loopback_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'COUNTS'):
                self.op.activate(args)
        journal = self.op.journal_path(args)
        self.assertEqual(json.loads((journal / 'phase.json').read_bytes())['phase'], 'code_rolled_back')
        self.assertFalse((journal / 'activation-receipt.json').exists())
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(self.op.idle_counts(self.root / 'data')['bookmarks'], 0)
        self.assertEqual(self.op.idle_counts(journal / 'data')['bookmarks'], 1)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_rejects_target_image_default_configuration_drift(self):
        args, fake, _, _ = self.deployment()
        def change_default(arguments, **kwargs):
            raw = fake(arguments, **kwargs)
            if arguments[0] == 'build':
                fake.images[fake.new_id]['env'].append('BOOKMARK_AUTH_DEBUG=1')
            return raw
        with patch.object(self.op, 'docker', side_effect=change_default):
            with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
                self.op.prepare(args)
        self.assertFalse(any('stop' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_configuration_binding_allows_expected_version_and_revision_updates(self):
        args, fake, _, _ = self.deployment()
        fake.code_environment = True
        fake.runtime_env = fake.effective_env(fake.old_id)
        prepared = self.op.prepare(args)
        self.assertIn('runtime_config_sha256', prepared)
        with patch.object(self.op, 'loopback_ready', return_value=True):
            activated = self.op.activate(args)
            verified = self.op.verify(args)
        self.assertEqual(prepared['runtime_config_sha256'], activated['runtime_config_sha256'])
        self.assertEqual(activated['runtime_config_sha256'], verified['runtime_config_sha256'])
        self.assertIn('LINKARIUM_VERSION=' + args.version, fake.runtime_env)
        self.assertIn('LINKARIUM_REVISION=' + args.commit, fake.runtime_env)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_rejects_auth_config_derived_from_changing_version(self):
        args, fake, _, _ = self.deployment()
        fake.origin_uses_version = True
        fake.runtime_env = fake.effective_env(fake.old_id)
        with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
            self.op.prepare(args)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_activate_binds_matching_runtime_config_to_prepared_digest(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        fake.environment['BOOKMARK_PASSKEY_ORIGIN'] = 'https://coordinated-drift.invalid'
        fake.runtime_env = fake.effective_env(fake.old_id)
        with patch.object(self.op, 'loopback_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
                self.op.activate(args)
        self.assertFalse(any('stop' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_activate_rechecks_runtime_drift_after_space_plan_before_stop(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        plan = self.op.plan_data
        def drift_during_plan(*arguments, **kwargs):
            result = plan(*arguments, **kwargs)
            fake.runtime_env.append('BOOKMARK_AUTH_DEBUG=1')
            return result
        with patch.object(self.op, 'plan_data', side_effect=drift_during_plan), patch.object(self.op, 'loopback_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
                self.op.activate(args)
        self.assertFalse(any('stop' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_standalone_verify_rejects_matching_but_unbound_runtime_configuration(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        with patch.object(self.op, 'loopback_ready', return_value=True):
            self.op.activate(args)
            fake.environment['BOOKMARK_TRUST_PROXY'] = '1'
            fake.runtime_env = fake.effective_env(fake.new_id)
            with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
                self.op.verify(args)

    def test_runtime_rejects_missing_or_duplicate_image_defaults(self):
        fake, old, metadata = self.runtime_fixture()
        for defaults in (None, ['PATH=/bin', 'PATH=/bin']):
            with self.subTest(defaults=defaults), patch.dict(fake.images[fake.old_id], env=defaults):
                with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
                    self.op.require_container(old, metadata)

    def test_prospective_compose_environment_injects_only_validated_code_fields(self):
        result = SimpleNamespace(returncode=0, stdout=b'{}', stderr=b'')
        metadata = {'version': 'v0.2.1', 'commit': '2' * 40, 'BOOKMARK_AUTH_DEBUG': '1'}
        with patch.object(self.op.subprocess, 'run', return_value=result) as run:
            self.op.docker(['compose', 'config'], deployment=metadata)
        environment = run.call_args.kwargs['env']
        self.assertEqual(environment['LINKARIUM_VERSION'], metadata['version'])
        self.assertEqual(environment['LINKARIUM_REVISION'], metadata['commit'])
        self.assertNotIn('BOOKMARK_AUTH_DEBUG', environment)
        with patch.object(self.op.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'ARGUMENT'):
                self.op.docker(['compose', 'config'], deployment={**metadata, 'commit': 'invalid'})
        run.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_prepare_rechecks_matching_configuration_changed_during_build(self):
        args, fake, _, _ = self.deployment()
        def drift(arguments, **kwargs):
            result = fake(arguments, **kwargs)
            if arguments[0] == 'build':
                fake.environment['BOOKMARK_HTTPS'] = '1'
                fake.runtime_env = fake.effective_env(fake.old_id)
            return result
        with patch.object(self.op, 'docker', side_effect=drift), self.assertRaisesRegex(RuntimeError, 'CONFIG'):
            self.op.prepare(args)
        self.assertFalse((self.op.journal_path(args) / 'prepared.json').exists())

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_activate_rejects_actual_port_drift_since_prepare_without_stopping(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        fake.runtime_ports['8000/tcp'][0]['HostPort'] = '9000'
        with self.assertRaisesRegex(RuntimeError, 'CONFIG'):
            self.op.activate(args)
        self.assertFalse(any('stop' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_cli_configuration_sentinels_never_enter_receipts_or_logs(self):
        args, fake, _, _ = self.deployment()
        sentinels = ['SYNTHETIC_RUNTIME_SECRET_92d5', 'SYNTHETIC_IMAGE_SECRET_e38a']
        fake.environment['BOOKMARK_PASSKEY_ORIGIN'] = sentinels[0]
        fake.images[fake.old_id]['env'].append('DEFAULT_AUTH_SECRET=' + sentinels[1])
        fake.runtime_env = fake.effective_env(fake.old_id)
        argv = ['--version', args.version, '--commit', args.commit, '--expected-base', args.expected_base]
        output = io.StringIO()
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        with patch.object(self.op, 'require_host'), patch.object(self.op, 'loopback_ready', return_value=True), patch('sys.stdout', output):
            self.assertEqual(self.op.main(['prepare', *argv, '--package', str(args.package), '--package-sha256', args.package_sha256]), 0)
            self.assertEqual(self.op.main(['activate', *argv]), 0)
            self.assertEqual(self.op.main(['verify', *argv]), 0)
            fake.runtime_env.append('BOOKMARK_AUTH_DEBUG=' + sentinels[0])
            self.assertEqual(self.op.main(['verify', *argv]), 1)
        journal = self.op.journal_path(args)
        evidence = output.getvalue().encode() + json.dumps(fake.calls).encode()
        for path in journal.iterdir():
            if path.is_file():
                evidence += path.read_bytes()
        for sentinel in sentinels:
            self.assertNotIn(sentinel.encode(), evidence)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['code'], 'RUNTIME_CONFIG_MISMATCH')

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_counts_baseline_is_taken_after_stop_while_supervisor_lock_is_held(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        stop, lock, write_json = self.op.stop, self.op.file_lock, self.op.write_json
        held, recorded = [], []
        def stop_with_last_write(*a, **kw):
            stop(*a, **kw)
            with closing(sqlite3.connect(self.root / 'data/generations/initial/library.sqlite3')) as db:
                db.execute('INSERT INTO library_bookmark VALUES(2)')
                db.commit()
        @contextmanager
        def tracked_lock(*a, **kw):
            with lock(*a, **kw):
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()
        def observe(path, value):
            if path.name == 'counts-before.json':
                self.assertTrue(held)
                self.assertFalse(fake.running)
                self.assertEqual(value['bookmarks'], 2)
                recorded.append(value)
            write_json(path, value)
        with patch.object(self.op, 'stop', side_effect=stop_with_last_write), patch.object(self.op, 'file_lock', tracked_lock), patch.object(self.op, 'write_json', side_effect=observe), patch.object(self.op, 'loopback_ready', return_value=True):
            result = self.op.activate(args)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0], result['counts'])
        self.assertEqual(recorded[0], self.op.idle_counts(self.op.journal_path(args) / 'data', immutable=True))

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_unexplained_startup_count_increase_is_refused_without_rewinding_it(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        def insert(image_id):
            if image_id == fake.new_id:
                with closing(sqlite3.connect(self.root / 'data/generations/initial/library.sqlite3')) as db:
                    db.execute('INSERT INTO library_snapshotversion VALUES(1)')
                    db.commit()
        fake.on_up = insert
        with patch.object(self.op, 'loopback_ready', return_value=True), self.assertRaisesRegex(RuntimeError, 'COUNTS'):
            self.op.activate(args)
        self.assertEqual(self.op.idle_counts(self.root / 'data')['snapshots'], 1)
        self.assertEqual(self.op.idle_counts(self.op.journal_path(args) / 'data')['snapshots'], 0)
        self.assertFalse((self.op.journal_path(args) / 'activation-receipt.json').exists())

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_cold_counts_mismatch_refuses_cutover_even_if_file_manifest_is_valid(self):
        args, fake, old, _ = self.deployment()
        self.op.prepare(args)
        snapshot = self.op.cold_snapshot
        def wrong_snapshot(data, journal):
            result = snapshot(data, journal)
            library = journal / 'data/generations/initial/library.sqlite3'
            with closing(sqlite3.connect(library)) as db:
                db.execute('DELETE FROM library_bookmark')
                db.commit()
            manifest = json.loads((journal / 'data-manifest.json').read_bytes())
            manifest['files']['generations/initial/library.sqlite3']['sha256'] = self.op.digest(library)
            self.op.write_json(journal / 'data-manifest.json', manifest)
            self.op.atomic_bytes(journal / 'data-manifest.sha256', (self.op.digest(journal / 'data-manifest.json') + '\n').encode())
            self.op.verify_cold(journal)  # Prove it is the count gate, not a byte-hash refusal.
            return result
        with patch.object(self.op, 'cold_snapshot', side_effect=wrong_snapshot), patch.object(self.op, 'loopback_ready', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'COUNTS'):
                self.op.activate(args)
        self.assertEqual(fake.current_id, fake.old_id)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(self.op.idle_counts(self.root / 'data')['bookmarks'], 1)

    @unittest.skipUnless(os.name == 'posix', 'phase fixtures require POSIX current symlink/locks')
    def test_standalone_verify_counts_gate_does_not_modify_or_restore_databases(self):
        args, fake, _, _ = self.deployment()
        self.op.prepare(args)
        with patch.object(self.op, 'loopback_ready', return_value=True):
            self.op.activate(args)
            library = self.root / 'data/generations/initial/library.sqlite3'
            with closing(sqlite3.connect(library)) as db:
                db.execute('DELETE FROM library_bookmark')
                db.commit()
            before = {p: p.read_bytes() for p in (library, self.root / 'data/state.sqlite3')}
            with self.assertRaisesRegex(RuntimeError, 'COUNTS'):
                self.op.verify(args)
            # Changing the recorded baseline alone must not bypass the cold gate.
            self.op.write_json(self.op.journal_path(args) / 'counts-before.json', self.op.idle_counts(self.root / 'data'))
            with self.assertRaisesRegex(RuntimeError, 'COUNTS'):
                self.op.verify(args)
        self.assertTrue(fake.running)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_counts_reject_each_unexplained_business_or_generation_change(self):
        data, *_ = self.source_data()
        before = self.op.idle_counts(data)
        self.op.require_counts(data, before)
        for key in ('bookmarks', 'snapshots', 'artifacts', 'active_jobs', 'active_maintenance', 'maintenance_mode', 'generation'):
            with self.subTest(key=key):
                changed = {**before, key: 'other-generation' if key == 'generation' else before[key] + 1}
                with self.assertRaisesRegex(RuntimeError, 'COUNTS'):
                    self.op.require_counts(data, changed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
