"""Synthetic offline fixtures only; native Linux is the durability gate."""
from contextlib import closing, contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SOURCE = Path(__file__).resolve().parents[2] / 'deploy/cloud/share_completed_backups.py'
SCRATCH = Path(os.environ.get('HERMES_TEST_SCRATCH') or os.environ.get('TMPDIR') or tempfile.gettempdir())


def load_tool():
    spec = importlib.util.spec_from_file_location('share_completed_backups', SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inode(path):
    info = path.stat()
    return info.st_dev, info.st_ino


@unittest.skipUnless(sys.platform == 'linux', 'native Linux flock/directory-fsync required')
class SharingTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCE.is_file(), 'completed backup sharing tool is missing')
        self.tool = load_tool()
        self.temp = tempfile.TemporaryDirectory(prefix='completed-sharing-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.active, self.cold = self.base / 'data', self.base / 'ops/checkpoint/data'
        self.operator = self.base / 'ops/v012-operator.lock'
        self.operator.parent.mkdir(parents=True)
        self.operator.write_bytes(b'')
        self.receipt_dir = self.base / 'ops/share-v1'
        self.receipt_dir.mkdir(mode=0o700)
        self.original = {}
        for root in (self.active, self.cold):
            (root / 'backups').mkdir(parents=True)
            (root / 'runtime').mkdir()
            (root / 'runtime/supervisor.lock').write_bytes(b'')
            with closing(sqlite3.connect(root / 'state.sqlite3')) as db:
                db.execute('CREATE TABLE runtime_maintenanceoperation(kind,state,result)')
                for index in range(2):
                    stream = io.BytesIO()
                    with zipfile.ZipFile(stream, 'w') as archive:
                        archive.writestr(zipfile.ZipInfo('fixture.txt', (2020, 1, 1, 0, 0, 0)), bytes([index + 65]) * (8192 + index * 4096))
                    path = root / 'backups' / f'complete-{index}.bookmarklib.zip'
                    path.write_bytes(stream.getvalue())
                    path.chmod(0o600)
                    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
                    self.original[path] = path.read_bytes()
                    receipt = {'package_path': '/data/backups/' + path.name,
                               'sha256': hashlib.sha256(self.original[path]).hexdigest(),
                               'byte_size': len(self.original[path])}
                    db.execute('INSERT INTO runtime_maintenanceoperation VALUES(?,?,?)', ('backup', 'succeeded', json.dumps(receipt)))
                db.commit()
        self.paths = list(self.original)

    def share(self, **kwargs):
        return self.tool.share(self.active, self.cold, operator_lock=self.operator, offline=True,
                               receipt_dir=self.receipt_dir, **kwargs)

    def assert_readable(self):
        for path, original in self.original.items():
            self.assertEqual(path.read_bytes(), original)

    def test_offline_acknowledgement_required_even_for_nonmutating_dry_run(self):
        before = {path: inode(path) for path in self.paths}
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(RuntimeError, 'OFFLINE_REQUIRED'):
                self.tool.share(self.active, self.cold, operator_lock=self.operator, apply=apply)
        report = self.share()
        self.assertTrue(report['ok'])
        self.assertFalse(report['applied'])
        self.assertEqual(report['replaced_paths'], 0)
        self.assertEqual({path: inode(path) for path in self.paths}, before)
        self.assert_readable()

    def update_receipt(self, root, index, **values):
        with closing(sqlite3.connect(root / 'state.sqlite3')) as db:
            rowid, raw = db.execute('SELECT rowid,result FROM runtime_maintenanceoperation ORDER BY rowid').fetchall()[index]
            receipt = json.loads(raw)
            receipt.update(values)
            db.execute('UPDATE runtime_maintenanceoperation SET result=? WHERE rowid=?', (json.dumps(receipt), rowid))
            db.commit()

    def test_receipt_sha_and_size_checked_before_any_replacement(self):
        before = {path: inode(path) for path in self.paths}
        original = self.original[self.cold / 'backups/complete-1.bookmarklib.zip']
        for change, code in (({'sha256': '0' * 64}, 'BACKUP_RECEIPT_SHA_MISMATCH'),
                             ({'byte_size': len(original) + 1}, 'BACKUP_RECEIPT_SIZE_MISMATCH')):
            self.update_receipt(self.cold, 1, sha256=hashlib.sha256(original).hexdigest(), byte_size=len(original))
            self.update_receipt(self.cold, 1, **change)
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, code):
                self.share(apply=True)
            self.assertEqual({path: inode(path) for path in self.paths}, before)
            self.assert_readable()

    def test_incompatible_metadata_refuses_entire_plan_without_chmod_or_chown(self):
        path = self.cold / 'backups/complete-1.bookmarklib.zip'
        before = {p: inode(p) for p in self.paths}
        changes = [lambda: path.chmod(0o640),
                   lambda: os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_001)),
                   lambda: os.chown(path, 10001, 10001)]
        for change in changes:
            change()
            with self.assertRaisesRegex(RuntimeError, 'BACKUP_METADATA_INCOMPATIBLE'):
                self.share(apply=True)
            self.assertEqual({p: inode(p) for p in self.paths}, before)
            os.chown(path, 0, 0)
            path.chmod(0o600)
            os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
            for name in os.listxattr(path):
                os.removexattr(path, name)
        # WSL tmpfs does not support setting user xattrs; inject its read result.
        with patch.object(self.tool.os, 'listxattr', return_value=['user.fixture']):
            with self.assertRaisesRegex(RuntimeError, 'BACKUP_METADATA_INCOMPATIBLE'):
                self.share(apply=True)
        with patch.object(self.tool.os, 'chmod', side_effect=AssertionError('must not chmod')), \
                patch.object(self.tool.os, 'chown', side_effect=AssertionError('must not chown')):
            self.assertTrue(self.share(apply=True)['ok'])
        self.assert_readable()

    def test_entire_input_tree_must_be_canonical_regular_and_closed_to_external_links(self):
        bad = self.active / 'unrelated'
        cases = [
            (lambda: bad.symlink_to(self.active / 'state.sqlite3'), 'PATH_LINK_FORBIDDEN'),
            (lambda: os.mkfifo(bad), 'PATH_SPECIAL_FORBIDDEN'),
            (lambda: os.link(self.paths[0], bad), 'UNAUTHORIZED_BACKUP_ALIAS'),
            (lambda: (self.active / 'bad:name').write_bytes(b'fixture'), 'RELATIVE_PATH_UNSAFE'),
            (lambda: (self.active / 'state.sqlite3-wal').write_bytes(b'pending'), 'SQLITE'),
            (lambda: (self.active / 'state.sqlite3-shm').write_bytes(b'pending'), 'SQLITE'),
            (lambda: os.link(self.paths[0], self.base / 'outside'), 'EXTERNAL_HARDLINK'),
        ]
        for create, code in cases:
            create()
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, code):
                self.share()
            for path in (bad, self.active / 'bad:name', self.active / 'state.sqlite3-wal',
                         self.active / 'state.sqlite3-shm', self.base / 'outside'):
                if path.exists() or path.is_symlink():
                    path.unlink()
        for first, second in ((str(self.active) + '/.', self.cold), (self.active, self.active),
                              (self.active, self.active / 'backups')):
            with self.subTest(first=str(first)), self.assertRaisesRegex(RuntimeError, 'ROOT'):
                self.tool.share(first, second, operator_lock=self.operator, offline=True)
        self.assert_readable()

    def test_restore_completion_without_size_or_sha_is_hashed_but_unfinished_zip_is_untouched(self):
        unfinished = []
        for root in (self.active, self.cold):
            path = root / 'backups/unfinished.bookmarklib.zip'
            path.write_bytes(self.paths[0].read_bytes())
            unfinished.append((path, inode(path)))
            with closing(sqlite3.connect(root / 'state.sqlite3')) as db:
                db.execute("UPDATE runtime_maintenanceoperation SET kind='restore',result=? WHERE rowid=1",
                           (json.dumps({'pre_restore_backup': 'complete-0.bookmarklib.zip'}),))
                db.execute('INSERT INTO runtime_maintenanceoperation VALUES(?,?,?)', ('backup', 'running', json.dumps({
                    'package_path': '/data/backups/' + path.name, 'byte_size': path.stat().st_size,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})))
                db.commit()
        self.assertTrue(self.share(apply=True)['ok'])
        for path, before in unfinished:
            self.assertEqual(inode(path), before)
        self.assert_readable()

    @contextmanager
    def locked_by_child(self, path):
        code = ('import fcntl,sys; f=open(sys.argv[1],"r+b");'
                'fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print("ready",flush=True); sys.stdin.read(1)')
        child = subprocess.Popen([sys.executable, '-S', '-c', code, str(path)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            yield
        finally:
            child.communicate('x', timeout=10)

    def test_all_real_locks_block_dry_run_and_apply_and_shared_supervisor_inode_locks_once(self):
        for path in (self.operator, self.active / 'runtime/supervisor.lock', self.cold / 'runtime/supervisor.lock'):
            with self.locked_by_child(path):
                for apply in (False, True):
                    with self.assertRaisesRegex(RuntimeError, 'LOCKED'):
                        self.share(apply=apply)
        cold_lock = self.cold / 'runtime/supervisor.lock'
        cold_lock.unlink()
        os.link(self.active / 'runtime/supervisor.lock', cold_lock)
        self.assertTrue(self.share()['ok'])
        outsider = self.base / 'lock-alias'
        os.link(cold_lock, outsider)
        with self.assertRaisesRegex(RuntimeError, 'EXTERNAL_HARDLINK'):
            self.share()

    def test_inherited_operator_lock_remains_held_and_replaced_lock_path_is_refused(self):
        import fcntl
        with self.operator.open('r+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(self.share(operator_lock_fd=held.fileno())['ok'])
            with self.assertRaisesRegex(RuntimeError, 'LOCKED'):
                self.share()
        original = self.tool.su.file_lock
        @contextmanager
        def replaced(path, *args, **kwargs):
            with original(path, *args, **kwargs):
                if path == self.operator:
                    saved = path.with_name('moved-lock')
                    path.rename(saved)
                    path.write_bytes(b'')
                yield
        with patch.object(self.tool.su, 'file_lock', replaced):
            with self.assertRaisesRegex(RuntimeError, 'LOCK_IDENTITY_CHANGED'):
                self.share()
        self.assert_readable()

    def cli_with_inherited_lock(self, fd=None, *, fault=False):
        entry = [str(SOURCE)]
        if fault:
            # Inject only directory fsync failure after real publication.
            code = '''import importlib.util,pathlib,sys
spec=importlib.util.spec_from_file_location('sharing',sys.argv[1])
tool=importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
original=tool.su.sync_directory
cold=pathlib.Path(sys.argv[sys.argv.index('--cold-root')+1])
def fail(path):
    if path == cold/'backups':
        raise OSError(5, 'PRIVATE_SYNTHETIC_DIRECTORY_FSYNC')
    return original(path)
tool.su.sync_directory=fail
raise SystemExit(tool.main(sys.argv[2:]))
'''
            entry = ['-c', code, str(SOURCE)]
        args = [sys.executable, '-B', '-S', *entry, '--active-root', str(self.active),
                '--cold-root', str(self.cold), '--operator-lock', str(self.operator),
                '--receipt-dir', str(self.receipt_dir), '--offline', '--apply']
        if fd is not None:
            args.extend(['--operator-lock-fd', str(fd)])
        return subprocess.run(args, capture_output=True, text=True, timeout=30,
                              pass_fds=() if fd is None else (fd,),
                              env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})

    def competing_lock_modes(self, path=None):
        # Independent opens in fresh processes distinguish SH, EX and no lock.
        code = '''import fcntl,sys
with open(sys.argv[1], 'r+b') as stream:
    try:
        fcntl.flock(stream, getattr(fcntl, 'LOCK_'+sys.argv[2])|fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(23)
'''
        modes = {}
        for mode in ('SH', 'EX'):
            result = subprocess.run([sys.executable, '-B', '-S', '-c', code,
                                     str(self.operator if path is None else path), mode],
                                    capture_output=True, text=True, timeout=10)
            self.assertIn(result.returncode, (0, 23), result.stderr)
            self.assertEqual(result.stdout + result.stderr, '')
            modes[mode] = result.returncode
        return modes

    def fixture_file_state(self):
        return {path.relative_to(self.base).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(), inode(path),
            path.stat().st_mode, path.stat().st_uid, path.stat().st_gid,
            path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_nlink)
            for path in self.base.rglob('*') if path.is_file()}

    def test_cli_rejects_shared_inherited_lock_before_writes_preserving_caller_mode(self):
        import fcntl
        before = self.fixture_file_state()
        with self.operator.open('r+b') as held:
            fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 23})
            result = self.cli_with_inherited_lock(held.fileno())
            after_modes = self.competing_lock_modes()
            # Subtests retain all behavioral observations on the original RED.
            with self.subTest(check='refusal'):
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), {'ok': False, 'code': 'LOCK_IDENTITY_CHANGED'})
                self.assertEqual(result.stderr, '')
            with self.subTest(check='caller_mode'):
                self.assertEqual(after_modes, {'SH': 0, 'EX': 23})
            with self.subTest(check='no_intent_or_inode_replacement'):
                self.assertEqual(self.fixture_file_state(), before)
            with self.subTest(check='no_receipts'):
                self.assertEqual(list(self.receipt_dir.iterdir()), [])
            self.assertEqual((os.fstat(held.fileno()).st_dev, os.fstat(held.fileno()).st_ino), inode(self.operator))
            self.assert_readable()
        self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})

    def test_cli_rejects_unlocked_inherited_descriptor_without_writes_or_locking_caller(self):
        before = self.fixture_file_state()
        with self.operator.open('r+b') as held:
            self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})
            result = self.cli_with_inherited_lock(held.fileno())
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), {'ok': False, 'code': 'LOCK_IDENTITY_CHANGED'})
            self.assertEqual(result.stderr, '')
            self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})
            self.assertEqual(self.fixture_file_state(), before)
            self.assertEqual(list(self.receipt_dir.iterdir()), [])
            self.assertEqual(os.fstat(held.fileno()).st_ino, self.operator.stat().st_ino)
        self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})

    def test_cli_shared_refusal_keeps_callers_lock_after_other_shared_holder_closes(self):
        import fcntl
        before = self.fixture_file_state()
        with self.operator.open('r+b') as held:
            fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.operator.open('r+b') as peer:
                fcntl.flock(peer, fcntl.LOCK_SH | fcntl.LOCK_NB)
                result = self.cli_with_inherited_lock(held.fileno())
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), {'ok': False, 'code': 'LOCK_IDENTITY_CHANGED'})
                self.assertEqual(result.stderr, '')
            # A failed SH->EX conversion can drop the caller's SH lock; the
            # second holder must be gone before testing ownership preservation.
            self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 23})
            self.assertEqual(self.fixture_file_state(), before)
            self.assertEqual(list(self.receipt_dir.iterdir()), [])
        self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})

    def test_cli_independent_open_cannot_impersonate_inherited_exclusive_description(self):
        import fcntl
        before = self.fixture_file_state()
        with self.operator.open('r+b') as held, self.operator.open('r+b') as unrelated:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(os.fstat(held.fileno()).st_ino, os.fstat(unrelated.fileno()).st_ino)
            self.assertEqual(self.competing_lock_modes(), {'SH': 23, 'EX': 23})
            result = self.cli_with_inherited_lock(unrelated.fileno())
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), {'ok': False, 'code': 'WRITER_OR_OPERATOR_LOCKED'})
            self.assertEqual(result.stderr, '')
            self.assertEqual(self.competing_lock_modes(), {'SH': 23, 'EX': 23})
            self.assertEqual(self.fixture_file_state(), before)
            self.assertEqual(list(self.receipt_dir.iterdir()), [])
            held.close()
            # The unrelated description remains open and must still be unlocked.
            self.assertEqual(os.fstat(unrelated.fileno()).st_ino, self.operator.stat().st_ino)
            self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})

    def test_cli_inherited_exclusive_lock_shares_and_stays_held_until_parent_close(self):
        import fcntl
        before = {path: path.stat() for path in self.paths}
        databases = {root / 'state.sqlite3': (root / 'state.sqlite3').read_bytes()
                     for root in (self.active, self.cold)}
        with self.operator.open('r+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.competing_lock_modes(), {'SH': 23, 'EX': 23})
            result = self.cli_with_inherited_lock(held.fileno())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertEqual(result.stderr, '')
            self.assertEqual(self.competing_lock_modes(), {'SH': 23, 'EX': 23})
            self.assertEqual(os.fstat(held.fileno()).st_ino, self.operator.stat().st_ino)
            for target in self.paths[2:]:
                source = self.active / 'backups' / target.name
                self.assertEqual(inode(source), (before[source].st_dev, before[source].st_ino))
                self.assertEqual(inode(source), inode(target))
            for path in self.paths:
                for field in ('st_mode', 'st_size', 'st_uid', 'st_gid', 'st_mtime_ns'):
                    self.assertEqual(getattr(path.stat(), field), getattr(before[path], field))
            for path, content in databases.items():
                self.assertEqual(path.read_bytes(), content)
            self.assert_readable()
            intent = (self.receipt_dir / 'intent.json').read_bytes()
            complete = json.loads((self.receipt_dir / 'complete.json').read_bytes())
            self.assertTrue(complete['ok'])
            self.assertEqual(complete['intent_sha256'], hashlib.sha256(intent).hexdigest())
        self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})

    def test_cli_inherited_exclusive_lock_survives_publication_failure_and_retry(self):
        import fcntl
        with self.operator.open('r+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            failed = self.cli_with_inherited_lock(held.fileno(), fault=True)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertEqual(json.loads(failed.stdout), {'ok': False, 'code': 'IO_ERROR'})
            self.assertEqual(failed.stderr, '')
            self.assertEqual(self.competing_lock_modes(), {'SH': 23, 'EX': 23})
            self.assertEqual(inode(self.paths[0]), inode(self.paths[2]))
            intent = (self.receipt_dir / 'intent.json').read_bytes()
            self.assertFalse((self.receipt_dir / 'complete.json').exists())
            self.assert_readable()
            for root in (self.active, self.cold):
                self.assertEqual(self.competing_lock_modes(root / 'runtime/supervisor.lock'), {'SH': 0, 'EX': 0})
            retry = self.cli_with_inherited_lock(held.fileno())
            self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            self.assertTrue(json.loads(retry.stdout)['ok'])
            self.assertEqual(retry.stderr, '')
            self.assertEqual(self.competing_lock_modes(), {'SH': 23, 'EX': 23})
            self.assertEqual((self.receipt_dir / 'intent.json').read_bytes(), intent)
            self.assertTrue(json.loads((self.receipt_dir / 'complete.json').read_bytes())['ok'])
            self.assertEqual(os.fstat(held.fileno()).st_ino, self.operator.stat().st_ino)
            for target in self.paths[2:]:
                self.assertEqual(inode(target), inode(self.active / 'backups' / target.name))
                self.assertFalse(self.stage(target).exists())
            self.assert_readable()
        self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})

    def test_api_nonexclusive_refusal_preserves_borrowed_fd_for_dry_run_and_apply(self):
        import fcntl
        before = self.fixture_file_state()
        for mode in ('SH', 'UNLOCKED'):
            for apply in (False, True):
                with self.subTest(mode=mode, apply=apply), self.operator.open('r+b') as held:
                    if mode == 'SH':
                        fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(RuntimeError, '^LOCK_IDENTITY_CHANGED$'):
                        self.share(operator_lock_fd=held.fileno(), apply=apply)
                    self.assertEqual(os.fstat(held.fileno()).st_ino, self.operator.stat().st_ino)
                    self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 23 if mode == 'SH' else 0})
                    self.assertEqual(self.fixture_file_state(), before)
                    self.assertEqual(list(self.receipt_dir.iterdir()), [])
                self.assertEqual(self.competing_lock_modes(), {'SH': 0, 'EX': 0})

    def test_hashing_binds_original_inputs_and_checks_all_aliases_before_publication(self):
        before = {p: inode(p) for p in self.paths}
        digest = self.tool.su.digest
        fired = False
        def drift(path):
            nonlocal fired
            result = digest(path)
            if path == self.paths[-1] and not fired:
                fired = True
                source = self.paths[0]
                info = source.stat()
                replacement = source.with_name('replacement')
                replacement.write_bytes(source.read_bytes())
                replacement.chmod(stat.S_IMODE(info.st_mode))
                os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
                os.replace(replacement, source)
            return result
        with patch.object(self.tool.su, 'digest', drift):
            with self.assertRaisesRegex(RuntimeError, 'INPUT_CHANGED'):
                self.share(apply=True)
        for path in self.paths[2:]:
            self.assertEqual(inode(path), before[path])
        self.assert_readable()

    def test_post_stage_other_alias_drift_prevents_replace_and_keeps_originals(self):
        before = {p: inode(p) for p in self.paths}
        link = self.tool.os.link
        def drift(*args, **kwargs):
            result = link(*args, **kwargs)
            self.paths[-1].chmod(0o640)
            return result
        with patch.object(self.tool.os, 'link', drift):
            with self.assertRaisesRegex(RuntimeError, 'INPUT_CHANGED'):
                self.share(apply=True)
        self.assertEqual({p: inode(p) for p in self.paths}, before)
        self.assert_readable()

    def stage(self, target):
        return target.with_name('.' + target.name + '.completed-share-v1')

    def test_fsync_failure_keeps_original_error_and_retry_syncs_already_shared_paths(self):
        failure = OSError('private path must not cross CLI boundary')
        sync = self.tool.su.sync_directory
        def fail_publication(path):
            if path == self.cold / 'backups':
                raise failure
            return sync(path)
        with patch.object(self.tool.su, 'sync_directory', side_effect=fail_publication):
            with self.assertRaises(OSError) as caught:
                self.share(apply=True)
        self.assertIs(caught.exception, failure)
        self.assert_readable()
        self.assertTrue(self.share(apply=True)['ok'])
        with patch.object(self.tool.su, 'sync_directory', side_effect=fail_publication):
            with self.assertRaises(OSError) as caught:
                self.share(apply=True)
        self.assertIs(caught.exception, failure)
        self.assertTrue(self.share(apply=True)['ok'])

    def test_only_exact_stage_bound_to_active_source_can_be_reused_or_removed(self):
        source, target = self.paths[0], self.paths[2]
        stage = self.stage(target)
        os.link(source, stage)
        dry = self.share()
        self.assertTrue(dry['ok'])
        self.assertTrue(stage.exists())
        self.assertTrue(self.share(apply=True)['ok'])
        self.assertFalse(stage.exists())
        # A crash can leave a source-bound stage even after publication.
        os.link(source, stage)
        self.assertTrue(self.share(apply=True)['ok'])
        self.assertFalse(stage.exists())
        stage.write_bytes(source.read_bytes())
        before = inode(stage)
        with self.assertRaisesRegex(RuntimeError, 'UNSAFE_SHARING_STAGE'):
            self.share(apply=True)
        self.assertEqual(inode(stage), before)
        self.assert_readable()

    def test_durable_conversion_receipt_preserves_legacy_manifest_and_binds_retry(self):
        import inspect
        self.assertIn('receipt_dir', inspect.signature(self.tool.share).parameters)
        receipt_dir = self.base / 'ops/sharing-receipt'
        receipt_dir.mkdir(mode=0o700)
        legacy_path = self.cold.parent / 'data-manifest.json'
        legacy = {'version': 1, 'files': {}}
        for path in self.paths[2:]:
            info = path.stat()
            legacy['files'][path.relative_to(self.cold).as_posix()] = {
                'kind': 'independent_copy', 'dev': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
                'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
                'mtime_ns': info.st_mtime_ns, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        legacy_path.write_text(json.dumps(legacy))
        original = legacy_path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        with self.assertRaisesRegex(RuntimeError, 'LEGACY_MANIFEST_ACK_REQUIRED'):
            self.tool.share(self.active, self.cold, operator_lock=self.operator, offline=True,
                            apply=True, receipt_dir=receipt_dir)
        kwargs = dict(operator_lock=self.operator, offline=True, apply=True, receipt_dir=receipt_dir,
                      legacy_manifest_sha256=digest)
        report = self.tool.share(self.active, self.cold, **kwargs)
        self.assertTrue(report['ok'])
        self.assertTrue(report['legacy_physical_independence_superseded'])
        self.assertEqual(report['legacy_manifest_sha256'], digest)
        self.assertEqual(legacy_path.read_bytes(), original)
        intent = json.loads((receipt_dir / 'intent.json').read_bytes())
        complete = json.loads((receipt_dir / 'complete.json').read_bytes())
        self.assertEqual(intent['legacy_manifest_sha256'], digest)
        self.assertEqual(len(intent['entries']), 4)
        self.assertTrue(complete['ok'])
        self.assertEqual(complete['intent_sha256'], hashlib.sha256((receipt_dir / 'intent.json').read_bytes()).hexdigest())
        self.assertEqual(sum(row['before']['inode'] != row['after']['inode'] for row in intent['entries']), 2)
        for path in receipt_dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(str(self.base), path.read_text())
            self.assertNotIn('complete-0', path.read_text())
        intent_bytes = (receipt_dir / 'intent.json').read_bytes()
        self.assertTrue(self.tool.share(self.active, self.cold, **kwargs)['ok'])
        dry = self.tool.share(self.active, self.cold, **{**kwargs, 'apply': False})
        self.assertTrue(dry['legacy_physical_independence_superseded'])
        self.assertEqual((receipt_dir / 'intent.json').read_bytes(), intent_bytes)
        # Equivalent bytes at an unauthorized new inode must not reset a retry.
        path = self.paths[2]
        temp = path.with_name('new-inode')
        temp.write_bytes(path.read_bytes())
        temp.chmod(0o600)
        os.utime(temp, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
        os.replace(temp, path)
        with self.assertRaisesRegex(RuntimeError, 'RECEIPT_INPUT_CHANGED'):
            self.tool.share(self.active, self.cold, **kwargs)
        self.assertEqual(legacy_path.read_bytes(), original)
        self.assert_readable()

    def test_apply_requires_receipt_and_fresh_reserve_and_cli_is_private_json(self):
        with self.assertRaisesRegex(RuntimeError, 'RECEIPT_REQUIRED'):
            self.tool.share(self.active, self.cold, operator_lock=self.operator, offline=True, apply=True)
        low = type('Usage', (), {'free': 512 * 1024**2 - 1})()
        with patch.object(self.tool.shutil, 'disk_usage', return_value=low):
            with self.assertRaisesRegex(RuntimeError, 'RESERVE_INSUFFICIENT'):
                self.share(apply=True)
        self.assertFalse((self.receipt_dir / 'intent.json').exists())
        self.assertTrue(hasattr(self.tool, 'main'), 'privacy-safe CLI missing')
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        base_args = [sys.executable, '-S', str(SOURCE), '--active-root', str(self.active),
                     '--cold-root', str(self.cold), '--operator-lock', str(self.operator)]
        for extra in ([], ['--offline', '--secret-private-fixture'], ['--offline', '--apply']):
            result = subprocess.run(base_args + extra, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, '')
            report = json.loads(result.stdout)
            self.assertFalse(report['ok'])
            self.assertNotIn('private-fixture', result.stdout)
            self.assertNotIn(str(self.base), result.stdout)
        result = subprocess.run(base_args + ['--offline', '--apply', '--receipt-dir', str(self.receipt_dir)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertEqual(result.stderr, '')
        self.assert_readable()

    def test_lock_identity_between_acquisition_and_inventory_cannot_be_rebound(self):
        scan = self.tool._scan
        fired = False
        def swapped(*args, **kwargs):
            nonlocal fired
            if not fired:
                fired = True
                self.operator.rename(self.operator.with_name('old-lock'))
                self.operator.write_bytes(b'')
            return scan(*args, **kwargs)
        with patch.object(self.tool, '_scan', swapped):
            with self.assertRaisesRegex(RuntimeError, 'LOCK_IDENTITY_CHANGED'):
                self.share(apply=True)
        self.assert_readable()

    def test_final_readback_rechecks_previous_alias_metadata_before_success(self):
        digest = self.tool.su.digest
        target = self.paths[-1]
        fired = False
        def drift(path):
            nonlocal fired
            value = digest(path)
            # Trigger during the final all-alias hash pass, not publish readback.
            if path == self.paths[0] and inode(self.paths[1]) == inode(target):
                fired = True
            elif path == target and fired:
                self.paths[0].chmod(0o640)
            return value
        with patch.object(self.tool.su, 'digest', drift):
            with self.assertRaisesRegex(RuntimeError, 'INPUT_CHANGED'):
                self.share(apply=True)
        self.assertFalse((self.receipt_dir / 'complete.json').exists())
        self.assert_readable()

    def test_conflicting_completion_evidence_blocks_before_replacement(self):
        failure = OSError('fixture publication failure')
        with patch.object(self.tool.os, 'replace', side_effect=failure):
            with self.assertRaises(OSError):
                self.share(apply=True)
        path = self.receipt_dir / 'complete.json'
        path.write_text('{"ok":true,"wrong":true}')
        path.chmod(0o600)
        before = {p: inode(p) for p in self.paths}
        with self.assertRaisesRegex(RuntimeError, 'RECEIPT_INPUT_CHANGED'):
            self.share(apply=True)
        self.assertEqual({p: inode(p) for p in self.paths}, before)
        self.assert_readable()

    def test_metadata_is_checked_before_archive_reads_and_raw_receipt_spelling_is_not_normalized(self):
        self.paths[-1].chmod(0o640)
        digest = self.tool.su.digest
        def no_archive_read(path):
            if path in self.original:
                self.fail('archive was hashed before metadata compatibility')
            return digest(path)
        with patch.object(self.tool.su, 'digest', no_archive_read):
            with self.assertRaisesRegex(RuntimeError, 'BACKUP_METADATA_INCOMPATIBLE'):
                self.share()
        self.paths[-1].chmod(0o600)
        self.update_receipt(self.cold, 1, package_path='/data/backups/./complete-1.bookmarklib.zip')
        with self.assertRaisesRegex(RuntimeError, 'BACKUP_COMPLETION_RECEIPT_INVALID'):
            self.share()
        self.assert_readable()

    def test_stage_identity_is_rechecked_after_hashing_before_atomic_replace(self):
        source, target = self.paths[0], self.paths[2]
        stage = self.stage(target)
        digest = self.tool.su.digest
        fired = False
        def swapped(path):
            nonlocal fired
            value = digest(path)
            if path == target and stage.exists() and not fired:
                fired = True
                rogue = stage.with_name('rogue')
                rogue.write_bytes(b'not an authorized backup')
                os.replace(rogue, stage)
            return value
        with patch.object(self.tool.su, 'digest', swapped):
            with self.assertRaisesRegex(RuntimeError, 'INPUT_CHANGED'):
                self.share(apply=True)
        self.assertTrue(stage.exists(), 'do not erase unbound staging evidence')
        self.assert_readable()

    def test_receipt_directory_identity_cannot_be_rebound_during_publication(self):
        digest = self.tool.su.digest
        fired = False
        def swapped(path):
            nonlocal fired
            value = digest(path)
            if path == self.paths[2] and self.stage(path).exists() and not fired:
                fired = True
                self.receipt_dir.rename(self.receipt_dir.with_name('retained-original-receipt'))
                self.receipt_dir.mkdir(mode=0o700)
            return value
        with patch.object(self.tool.su, 'digest', swapped):
            with self.assertRaisesRegex(RuntimeError, 'INPUT_CHANGED'):
                self.share(apply=True)
        self.assertFalse((self.receipt_dir / 'complete.json').exists())
        self.assert_readable()

    def test_changed_intent_never_gets_a_success_receipt(self):
        sync = self.tool.su.sync_directory
        def tampered(path):
            sync(path)
            if path == self.cold / 'backups':
                (self.receipt_dir / 'intent.json').write_text('{"tampered":true}')
        with patch.object(self.tool.su, 'sync_directory', tampered):
            with self.assertRaisesRegex(RuntimeError, 'RECEIPT_INPUT_CHANGED'):
                self.share(apply=True)
        self.assertFalse((self.receipt_dir / 'complete.json').exists())
        self.assert_readable()

    def test_receipt_sidecars_and_noncanonical_arguments_fail_before_writes(self):
        before = {p: inode(p) for p in self.paths}
        sidecar = self.receipt_dir / 'complete.json'
        sidecar.symlink_to(self.base / 'missing')
        with self.assertRaisesRegex(RuntimeError, 'PATH_LINK_FORBIDDEN'):
            self.share(apply=True)
        self.assertEqual({p: inode(p) for p in self.paths}, before)
        sidecar.unlink()
        for changes in ({'operator_lock': str(self.operator.parent) + '/./' + self.operator.name},
                        {'receipt_dir': str(self.receipt_dir) + '/.'},
                        {'legacy_manifest_sha256': 'not-a-digest'}):
            kwargs: dict[str, object] = dict(operator_lock=self.operator, receipt_dir=self.receipt_dir, offline=True, apply=True)
            kwargs.update(changes)
            with self.subTest(changes=tuple(changes)), self.assertRaisesRegex(RuntimeError, 'ARGUMENT_INVALID'):
                self.tool.share(self.active, self.cold, **kwargs)
        self.assertFalse((self.receipt_dir / 'intent.json').exists())
        self.assert_readable()

    def test_namespace_reduction_is_not_free_space_while_old_readers_hold_inodes(self):
        import shutil
        readers = [path.open('rb') for path in self.paths[2:]]
        try:
            expected = sum(os.fstat(stream.fileno()).st_blocks * 512 for stream in readers)
            report = self.share(apply=True)
            self.assertEqual(report['tree_allocation_reduction_bytes'], expected)
            for stream, path in zip(readers, self.paths[2:]):
                self.assertEqual(os.fstat(stream.fileno()).st_nlink, 0)
                self.assertGreater(os.fstat(stream.fileno()).st_blocks, 0)
                self.assertEqual(stream.read(), self.original[path])
            self.assertLess(report['filesystem_free_delta_bytes'], expected)
            self.assertEqual(report['filesystem_free_after_bytes'], shutil.disk_usage(self.active).free)
            print(json.dumps({'held_reader_count': len(readers), 'logical_bytes': report['logical_bytes'],
                              'tree_allocation_reduction_bytes': expected,
                              'filesystem_free_delta_bytes': report['filesystem_free_delta_bytes']}))
        finally:
            for stream in readers:
                stream.close()
        self.assert_readable()

    def crash_at(self, operation):
        code = '''import importlib.util,os,pathlib,sys
spec=importlib.util.spec_from_file_location('sharing',sys.argv[1]); tool=importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
original=getattr(tool.os,sys.argv[5])
def crash(*a,**kw):
    original(*a,**kw)
    os._exit(71)
setattr(tool.os,sys.argv[5],crash)
base=pathlib.Path(sys.argv[2])
tool.share(base/'data',base/'ops/checkpoint/data',operator_lock=base/'ops/v012-operator.lock',offline=True,apply=True,receipt_dir=pathlib.Path(sys.argv[3]))
'''
        result = subprocess.run([sys.executable, '-S', '-c', code, str(SOURCE), str(self.base),
                                 str(self.receipt_dir), 'unused', operation], capture_output=True, text=True)
        self.assertEqual(result.returncode, 71, result.stdout + result.stderr)
        self.assertEqual(result.stdout + result.stderr, '')
        intent = (self.receipt_dir / 'intent.json').read_bytes()
        self.assertFalse((self.receipt_dir / 'complete.json').exists())
        self.assert_readable()
        result = subprocess.run([sys.executable, '-S', str(SOURCE), '--active-root', str(self.active),
            '--cold-root', str(self.cold), '--operator-lock', str(self.operator), '--offline', '--apply',
            '--receipt-dir', str(self.receipt_dir)], capture_output=True, text=True,
            env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertEqual((self.receipt_dir / 'intent.json').read_bytes(), intent)
        self.assert_readable()
        for path in self.paths[2:]:
            self.assertEqual(inode(path), inode(self.active / 'backups' / path.name))
            self.assertFalse(self.stage(path).exists())

    def test_real_child_exit_after_link_retries_from_original_receipt(self):
        self.crash_at('link')

    def test_real_child_exit_after_replace_retries_from_original_receipt(self):
        self.crash_at('replace')

    def test_source_fsync_failure_precedes_link_and_intent_fsync_failure_is_retryable(self):
        fsync = self.tool.os.fsync
        failure = OSError('private fsync fixture')
        def fail_intent(fd):
            path = self.receipt_dir / 'intent.json'
            if path.exists() and _fd_inode(fd) == inode(path):
                raise failure
            return fsync(fd)
        def _fd_inode(fd):
            info = os.fstat(fd)
            return info.st_dev, info.st_ino
        before = {p: inode(p) for p in self.paths}
        with patch.object(self.tool.os, 'fsync', fail_intent):
            with self.assertRaises(OSError) as caught:
                self.share(apply=True)
        self.assertIs(caught.exception, failure)
        self.assertTrue((self.receipt_dir / 'intent.json').exists())
        def fail_source(fd):
            if _fd_inode(fd) == inode(self.paths[0]):
                raise failure
            return fsync(fd)
        with patch.object(self.tool.os, 'fsync', fail_source):
            with self.assertRaises(OSError) as caught:
                self.share(apply=True)
        self.assertIs(caught.exception, failure)
        self.assertEqual({p: inode(p) for p in self.paths}, before)
        self.assertFalse(self.stage(self.paths[2]).exists())
        self.assertTrue(self.share(apply=True)['ok'])
        self.assert_readable()

    def test_fresh_reserve_failure_between_groups_retains_partial_progress_for_retry(self):
        before = {p: inode(p) for p in self.paths}
        good = type('Usage', (), {'free': 1024**3})()
        low = type('Usage', (), {'free': 512 * 1024**2 - 1})()
        with patch.object(self.tool.shutil, 'disk_usage', side_effect=[good, good, low]):
            with self.assertRaisesRegex(RuntimeError, 'RESERVE_INSUFFICIENT'):
                self.share(apply=True)
        self.assertEqual(inode(self.paths[2]), inode(self.paths[0]))
        self.assertEqual(inode(self.paths[3]), before[self.paths[3]])
        self.assertFalse((self.receipt_dir / 'complete.json').exists())
        self.assert_readable()
        self.assertTrue(self.share(apply=True)['ok'])

    def test_different_filesystem_roots_are_refused(self):
        # /tmp is a second private tmpfs in the verified container harness.
        with tempfile.TemporaryDirectory(prefix='sharing-other-fs-', dir='/tmp') as other:
            if Path(other).stat().st_dev == self.active.stat().st_dev:
                self.skipTest('second native filesystem not configured')
            with self.assertRaisesRegex(RuntimeError, 'ROOT_CROSS_DEVICE'):
                self.tool.share(self.active, Path(other), operator_lock=self.operator, offline=True)
        self.assert_readable()

    def test_shares_two_completed_zip_groups_preserving_active_inode_bytes_metadata(self):
        before = {path: path.stat() for path in self.paths}
        report = self.share(apply=True)
        self.assertTrue(report['ok'])
        self.assertTrue(report['applied'])
        self.assertEqual(report['completed_paths'], 4)
        self.assertEqual(report['groups'], 2)
        self.assertEqual(report['replaced_paths'], 2)
        self.assertEqual(report['logical_bytes'], sum(len(value) for value in self.original.values()))
        self.assertEqual(report['unique_allocated_before_bytes'], sum(info.st_blocks * 512 for info in before.values()))
        expected = sum(before[path].st_blocks * 512 for path in self.paths if path.is_relative_to(self.cold))
        self.assertEqual(report['tree_allocation_reduction_bytes'], expected)
        self.assertIsInstance(report['filesystem_free_after_bytes'], int)
        self.assertEqual(report['reserve_bytes'], 512 * 1024**2)
        for name in ('complete-0.bookmarklib.zip', 'complete-1.bookmarklib.zip'):
            active, cold = self.active / 'backups' / name, self.cold / 'backups' / name
            self.assertEqual(inode(active), (before[active].st_dev, before[active].st_ino))
            self.assertEqual(inode(active), inode(cold))
            for field in ('st_size', 'st_mode', 'st_uid', 'st_gid', 'st_mtime_ns'):
                self.assertEqual(getattr(cold.stat(), field), getattr(before[cold], field))
        self.assert_readable()
        self.assertNotIn(str(self.base), json.dumps(report))


class PortableCliTests(unittest.TestCase):
    def test_private_argument_and_os_errors_are_json_in_fresh_stdlib_interpreter(self):
        for args in (['--private-title-fixture'], ['--active-root', '/private-missing-active', '--cold-root',
                    '/private-missing-cold', '--operator-lock', '/private-missing-lock', '--offline']):
            result = subprocess.run([sys.executable, '-S', str(SOURCE), *args], capture_output=True, text=True,
                                    env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, '')
            value = json.loads(result.stdout)
            self.assertFalse(value['ok'])
            self.assertNotIn('private-', result.stdout)
        tool = load_tool()
        output = io.StringIO()
        with patch.object(tool, 'share', side_effect=OSError('private-token-and-title')), redirect_stdout(output):
            self.assertEqual(tool.main(['--active-root', '/a', '--cold-root', '/b', '--operator-lock', '/c']), 1)
        self.assertEqual(json.loads(output.getvalue()), {'ok': False, 'code': 'IO_ERROR'})

    def test_non_linux_has_explicit_refusal_not_fake_durability(self):
        tool = load_tool()
        with patch.object(tool.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(RuntimeError, 'LINUX_REQUIRED'):
                tool.share('/a', '/b', operator_lock='/c', offline=True)


if __name__ == '__main__':
    unittest.main()
