"""Borrowed EX plus exhaustive enumeration at the public sharing API seam.

Synthetic native files only. EACCES is a real kernel denial with no DAC override;
EIO is deliberately injected at os.scandir, not fabricated product output.
"""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_share_completed_backups as sharing_fixtures


FIELDS = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size',
          'st_mtime_ns', 'st_nlink', 'st_ctime_ns')


def fingerprint(root, fixture_chmod_directory=None):
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.stat()
        row = {key: getattr(info, key) for key in FIELDS
               if key != 'st_ctime_ns' or path != fixture_chmod_directory}
        if path.is_file():
            row['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        result[path.relative_to(root).as_posix()] = row
    return result


def child_call():
    """Fresh process uses the actual borrowed open-file description."""
    args = json.loads(sys.argv[1])
    tool = sharing_fixtures.load_tool()
    target = Path(args['target'])
    scan = os.scandir
    hits = []
    injected = OSError(errno.EIO, 'INJECTED_SHARING_SCANDIR_EIO', str(target))

    def scanning(path):
        if Path(path) == target:
            hits.append(str(path))
            if args['kind'] == 'eio':
                raise injected
        return scan(path)

    error, report = None, None
    with patch.object(os, 'scandir', scanning), \
            patch.object(os, 'chmod', side_effect=AssertionError('no product chmod')), \
            patch.object(os, 'chown', side_effect=AssertionError('no product chown')), \
            patch.object(tool, '_intent', wraps=tool._intent) as intent:
        try:
            report = tool.share(Path(args['active']), Path(args['cold']),
                operator_lock=Path(args['operator']), operator_lock_fd=args['fd'],
                receipt_dir=Path(args['receipt']), legacy_manifest_sha256=args['legacy_sha'],
                offline=True, apply=True)
        except Exception as caught:
            error = caught
        intent_calls = intent.call_count
    try:
        info = os.fstat(args['fd'])
        borrowed_fd = [info.st_dev, info.st_ino]
    except OSError:
        borrowed_fd = None
    value = dict(error_type=type(error).__name__ if error else None,
        errno=getattr(error, 'errno', None), same_injected_error=error is injected,
        report=report, fault_scans=hits, intent_calls=intent_calls, borrowed_fd=borrowed_fd,
        sharing_file=str(Path(tool.__file__).resolve()),
        helper_file=str(Path(tool.su.__file__).resolve()),
        sharing_sha256=hashlib.sha256(Path(tool.__file__).read_bytes()).hexdigest(),
        helper_sha256=hashlib.sha256(Path(tool.su.__file__).read_bytes()).hexdigest(),
        cap_eff=next(line.split(':', 1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines()
                     if line.startswith('CapEff:')))
    print(json.dumps(value, sort_keys=True), flush=True)
    raise SystemExit(1 if error else 0)


@unittest.skipUnless(sys.platform == 'linux', 'native Linux enumeration and inherited flock required')
class SharingEnumerationIntegrationTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        fixture = sharing_fixtures.SharingTests('runTest')
        fixture.setUp()
        try:
            # Include a real historical physical manifest; only synthetic DBs are read.
            legacy = {'version': 1, 'files': {}}
            for path in fixture.paths[2:]:
                info = path.stat()
                legacy['files'][path.relative_to(fixture.cold).as_posix()] = {
                    'kind': 'independent_copy', 'dev': info.st_dev, 'inode': info.st_ino,
                    'size': info.st_size, 'mode': stat.S_IMODE(info.st_mode),
                    'uid': info.st_uid, 'gid': info.st_gid, 'mtime_ns': info.st_mtime_ns,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            fixture.legacy = fixture.cold.parent / 'data-manifest.json'
            fixture.legacy.write_text(json.dumps(legacy), encoding='utf-8')
            fixture.legacy_sha = hashlib.sha256(fixture.legacy.read_bytes()).hexdigest()
            yield fixture
        finally:
            fixture.doCleanups()

    def invoke(self, fixture, held, kind, target):
        args = dict(active=str(fixture.active), cold=str(fixture.cold),
                    operator=str(fixture.operator), receipt=str(fixture.receipt_dir),
                    legacy_sha=fixture.legacy_sha, fd=held.fileno(), kind=kind, target=str(target))
        return subprocess.run([sys.executable, '-B', '-S', '-c',
            'from test_sharing_enumeration_integration import child_call; child_call()', json.dumps(args)],
            pass_fds=(held.fileno(),), capture_output=True, text=True, timeout=30)

    def fault_case(self, kind, root_name, location):
        import fcntl
        with self.fixture() as fixture:
            root = getattr(fixture, root_name)
            hidden = root / 'unreadable-nonartifact'
            hidden.mkdir()
            (hidden / 'retained.sqlite3-wal').write_bytes(b'synthetic nonempty WAL' if location == 'wal' else b'')
            target = hidden if location == 'wal' else root / 'backups'
            before = fingerprint(fixture.base, target if kind == 'eacces' else None)
            original_mode = stat.S_IMODE(target.stat().st_mode)
            native_errno = None
            if kind == 'eacces':
                target.chmod(0)
                try:
                    with self.assertRaises(PermissionError) as denial:
                        list(target.iterdir())
                    native_errno = denial.exception.errno
                    self.assertEqual(native_errno, errno.EACCES)
                except BaseException:
                    target.chmod(original_mode)
                    raise
            denied_stat = {key: getattr(target.stat(), key) for key in FIELDS}
            try:
                with fixture.operator.open('r+b') as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(fixture.competing_lock_modes(), {'SH': 23, 'EX': 23})
                    result = self.invoke(fixture, held, kind, target)
                    self.assertEqual(result.stderr, '')
                    observed = json.loads(result.stdout)
                    observed.update(kind=kind, root=root_name, location=location,
                                    child_exit=result.returncode, native_errno=native_errno)
                    # Check ownership even on the RED path; a caller EX must survive.
                    observed['caller_modes'] = fixture.competing_lock_modes()
                    observed['caller_fd'] = [os.fstat(held.fileno()).st_dev, os.fstat(held.fileno()).st_ino]
                    observed['denied_directory_unchanged_during_call'] = (
                        {key: getattr(target.stat(), key) for key in FIELDS} == denied_stat)
                    with self.subTest(check='caller_EX_and_borrowed_FD'):
                        self.assertEqual(observed['caller_modes'], {'SH': 23, 'EX': 23})
                        self.assertEqual(observed['borrowed_fd'], list(sharing_fixtures.inode(fixture.operator)))
                        self.assertEqual(observed['caller_fd'], observed['borrowed_fd'])
                    with self.subTest(check='native_fault_and_original_exception'):
                        self.assertEqual(int(observed['cap_eff'], 16), 0)
                        self.assertTrue(observed['fault_scans'])
                        self.assertEqual(result.returncode, 1, result.stdout)
                        self.assertEqual(observed['errno'], errno.EACCES if kind == 'eacces' else errno.EIO)
                        self.assertEqual(observed['error_type'], 'PermissionError' if kind == 'eacces' else 'OSError')
                        if kind == 'eio':
                            self.assertTrue(observed['same_injected_error'])
                    with self.subTest(check='before_intent_and_no_permission_repair'):
                        self.assertEqual(observed['intent_calls'], 0)
                        self.assertTrue(observed['denied_directory_unchanged_during_call'])
                        self.assertEqual(list(fixture.receipt_dir.iterdir()), [])
            finally:
                if kind == 'eacces':
                    target.chmod(original_mode)  # fixture teardown only, after product returned
            after = fingerprint(fixture.base, target if kind == 'eacces' else None)
            observed.update(before=before, after=after,
                caller_modes_after_close=fixture.competing_lock_modes(),
                fixture_chmod_ctime_excluded=kind == 'eacces',
                atime_excluded_because_reads_can_change_atime=True)
            print('SHARING_ENUMERATION_EVIDENCE ' + json.dumps(observed, sort_keys=True))
            with self.subTest(check='all_paths_bytes_inodes_metadata_and_no_receipts'):
                self.assertEqual(after, before)
                self.assertEqual(list(fixture.receipt_dir.iterdir()), [])
            self.assertEqual(observed['caller_modes_after_close'], {'SH': 0, 'EX': 0})

    def test_inherited_ex_real_eacces_refuses_before_intent_without_mutation(self):
        for root in ('active', 'cold'):
            with self.subTest(root=root):
                self.fault_case('eacces', root, 'wal')

    def test_inherited_ex_injected_eio_refuses_wal_and_backup_enumeration(self):
        for root, location in (('active', 'wal'), ('cold', 'backups')):
            with self.subTest(root=root, location=location):
                self.fault_case('eio', root, location)

    def test_readable_inherited_ex_control_publishes_preserving_content_and_caller_lock(self):
        import fcntl
        with self.fixture() as fixture:
            for root in (fixture.active, fixture.cold):
                hidden = root / 'unreadable-nonartifact'
                hidden.mkdir()
                (hidden / 'retained.sqlite3-wal').write_bytes(b'')
                (hidden / 'retained').write_bytes(b'readable control')
            before = fingerprint(fixture.base)
            with fixture.operator.open('r+b') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.invoke(fixture, held, 'readable', fixture.cold / 'backups')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
                observed = json.loads(result.stdout)
                self.assertIsNone(observed['error_type'])
                self.assertTrue(observed['fault_scans'])
                self.assertEqual(int(observed['cap_eff'], 16), 0)
                self.assertEqual(observed['intent_calls'], 1)
                self.assertEqual(observed['report']['replaced_paths'], 2)
                self.assertEqual(observed['report']['reserve_bytes'], 512 * 1024**2)
                self.assertTrue(observed['report']['legacy_physical_independence_superseded'])
                self.assertEqual(fixture.competing_lock_modes(), {'SH': 23, 'EX': 23})
                self.assertEqual(observed['borrowed_fd'], list(sharing_fixtures.inode(fixture.operator)))
                self.assertEqual(os.fstat(held.fileno()).st_ino, fixture.operator.stat().st_ino)
            after = fingerprint(fixture.base)
            for name, row in before.items():
                if 'sha256' not in row:
                    continue
                self.assertEqual(after[name]['sha256'], row['sha256'])
                for key in ('st_mode', 'st_uid', 'st_gid', 'st_size', 'st_mtime_ns'):
                    self.assertEqual(after[name][key], row[key])
                if '/backups/' not in '/' + name:
                    self.assertEqual(after[name], row)
            for target in fixture.paths[2:]:
                source = fixture.active / 'backups' / target.name
                self.assertEqual(sharing_fixtures.inode(source), sharing_fixtures.inode(target))
                self.assertEqual(source.stat().st_ino, before[source.relative_to(fixture.base).as_posix()]['st_ino'])
            intent = (fixture.receipt_dir / 'intent.json').read_bytes()
            complete = json.loads((fixture.receipt_dir / 'complete.json').read_bytes())
            self.assertTrue(complete['ok'])
            self.assertEqual(complete['intent_sha256'], hashlib.sha256(intent).hexdigest())
            self.assertEqual(fixture.competing_lock_modes(), {'SH': 0, 'EX': 0})
            print('SHARING_ENUMERATION_EVIDENCE ' + json.dumps(dict(kind='readable',
                child_exit=result.returncode, observed=observed, before=before, after=after), sort_keys=True))


if __name__ == '__main__':
    unittest.main()
