"""Explicit code rollback: synthetic SQLite/files, fake Docker and readiness.

Reuse unchanged operator fixtures. POSIX cases use real symlinks and flock;
these are not real-Docker or production acceptance tests.
"""
from contextlib import closing, contextmanager
import io
import json
import os
from pathlib import Path
import sqlite3
from types import ModuleType
import unittest
from unittest.mock import patch

import test_storage_upgrade as fixtures


class Fixture(unittest.TestCase):
    root: Path
    op: ModuleType  # setUp loads the actual module
    setUp = fixtures.SafetyTests.setUp
    release = fixtures.SafetyTests.release
    manifest = fixtures.SafetyTests.manifest
    source_data = fixtures.SafetyTests.source_data
    deployment = fixtures.SafetyTests.deployment

    def active(self, raw=True):
        args, fake, old, original = self.deployment()
        # This rollback target is raw-compatible. The original fixture also
        # tests unchanged gzip checkpoints; explicit old-code rollback must not.
        for path in (self.root / 'data').rglob('*.html.gz'):
            if raw:
                path.unlink()
        fake.code_environment = True
        fake.runtime_env = fake.effective_env(fake.old_id)
        self.op.prepare(args)
        ready = patch.object(self.op, 'loopback_ready', return_value=True)
        ready.start()
        self.addCleanup(ready.stop)
        self.op.activate(args)
        fake.calls.clear()
        self.args, self.fake, self.old, self.original = args, fake, old, original
        self.journal = self.op.journal_path(args)
        self.data = self.root / 'data'
        return args

    def invoke(self, extra=()):
        output, errors = io.StringIO(), io.StringIO()
        argv = ['rollback', '--version', self.args.version, '--commit', self.args.commit,
                '--expected-base', self.args.expected_base, *extra]
        previous = os.umask(0o077)
        try:
            with patch.object(self.op, 'require_host'), patch('sys.stdout', output), patch('sys.stderr', errors):
                try:
                    code = self.op.main(argv)
                except SystemExit as error:
                    code = error.code
        finally:
            os.umask(previous)
        self.output = output.getvalue()
        self.cli_errors = errors.getvalue()
        self.events = [json.loads(line) for line in self.output.splitlines()]
        return code

    def newer_data(self):
        with closing(sqlite3.connect(self.data / 'state.sqlite3')) as db:
            db.execute('DELETE FROM auth_user')  # Revoked since cold checkpoint.
            db.execute("INSERT INTO django_session VALUES(2,'SYNTHETIC_CURRENT_SESSION')")
            db.commit()
        with closing(sqlite3.connect(self.data / 'generations/initial/library.sqlite3')) as db:
            db.execute('INSERT INTO library_bookmark VALUES(2)')
            db.commit()

    def data_bytes(self):
        return {p.relative_to(self.data).as_posix(): p.read_bytes()
                for p in self.data.rglob('*') if p.is_file()}

    def phase(self):
        return json.loads((self.journal / 'phase.json').read_bytes())['phase']


class RollbackCLITests(Fixture):
    def test_cli_accepts_exact_target_and_expected_base_without_package(self):
        args = self.op.parse_args(['rollback', '--version', 'v0.2.1', '--commit', '2' * 40,
                                   '--expected-base', '1' * 40])
        self.assertEqual((args.phase, args.version, args.commit, args.expected_base),
                         ('rollback', 'v0.2.1', '2' * 40, '1' * 40))
        self.assertIsNone(args.package)

    def test_cli_still_refuses_package_overrides_and_malformed_identity(self):
        argv = ['rollback', '--version', 'v0.2.1', '--commit', '2' * 40, '--expected-base', '1' * 40]
        for extra in (['--package', 'not-used'], ['--package-sha256', 'a' * 64],
                      ['--base', 'not-used'], ['--commit', '2' * 39], ['--expected-base', 'A' * 40]):
            with self.subTest(extra=extra), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.op.parse_args(argv + extra)
            self.assertEqual(caught.exception.code, 2)


@unittest.skipUnless(os.name == 'posix', 'real POSIX current symlink/supervisor locks; run on native Linux scratch')
class ExplicitRollbackTests(Fixture):
    def assert_refused_before_stop(self, reason):
        before = self.data_bytes()
        current = (self.root / 'current').readlink()
        environment = (self.root / 'production.env').read_bytes()
        self.assertEqual(self.invoke(), 1, self.output + self.cli_errors)
        self.assertRegex(self.events[-1]['code'], reason)
        self.assertFalse(any('stop' in call or 'up' in call for call in self.fake.calls))
        self.assertEqual((self.root / 'current').readlink(), current)
        self.assertEqual((self.root / 'production.env').read_bytes(), environment)
        self.assertEqual(self.data_bytes(), before)
        self.assertTrue(self.fake.running)

    def test_unsupported_or_ambiguous_phase_is_refused_before_stop(self):
        self.active()
        for phase in ('preparing', 'prepared', 'stopping', 'cold_complete', 'starting',
                      'code_rolled_back', 'rolling_back', 'manual_recovery_required', 'unknown', None):
            with self.subTest(phase=phase):
                self.op.write_json(self.journal / 'phase.json', {'phase': phase})
                self.assert_refused_before_stop('PHASE_NOT_ACTIVE')
                self.assertEqual(self.phase(), phase)

    def test_activation_receipt_bindings_required_before_stop(self):
        self.active()
        path = self.journal / 'activation-receipt.json'
        activation = json.loads(path.read_bytes())
        for key in ('current', 'image_id', 'revision', 'metadata', 'runtime_config_sha256',
                    'ready', 'identity_continuity', 'schema_and_storage_unchanged',
                    'business_counts_unchanged', 'cold_checkpoint_verified', 'cold_checkpoint'):
            with self.subTest(field=key):
                self.op.write_json(path, {**activation, key: None})
                self.assert_refused_before_stop('ACTIVATION_RECEIPT')
        path.unlink()
        self.assert_refused_before_stop('FileNotFoundError')

    def test_runtime_drift_is_refused_before_stop(self):
        self.active()
        self.fake.runtime_env.append('BOOKMARK_AUTH_DEBUG=SYNTHETIC_DRIFT_SECRET')
        self.assert_refused_before_stop('RUNTIME_CONFIG')
        self.assertNotIn('SYNTHETIC_DRIFT_SECRET', self.output)

    def test_old_release_drift_is_refused_before_stop(self):
        self.active()
        (self.old / 'backend/supervisor.py').write_bytes(b'changed old source')
        self.manifest(self.old)
        self.assert_refused_before_stop('MANIFEST_DRIFT')

    def test_new_release_drift_is_refused_before_stop(self):
        self.active()
        target = (self.root / 'current').resolve()
        (target / 'backend/supervisor.py').write_bytes(b'changed active source')
        self.manifest(target)
        self.assert_refused_before_stop('MANIFEST_DRIFT')

    def test_active_image_drift_is_refused_before_stop(self):
        self.active()
        different = 'sha256:' + '3' * 64
        self.fake.images[different] = self.fake.image(different, self.args.version, self.args.commit)
        self.fake.current_id = different
        self.assert_refused_before_stop('IMAGE_DRIFT')

    def test_pinned_overlay_drift_is_refused_before_stop(self):
        self.active()
        self.op.write_json(self.journal / 'new-image.compose.json', {'services': {'app': {'image': self.fake.old_id}}})
        self.assert_refused_before_stop('PINNED_OVERLAY_DRIFT')

    def test_active_environment_must_not_already_be_old(self):
        self.active()
        (self.root / 'production.env').write_bytes(self.original)
        self.assert_refused_before_stop('CONFIG|ENV')

    def test_compressed_library_is_refused_even_if_guard_was_already_gzip(self):
        self.active(raw=False)
        self.assert_refused_before_stop('SCHEMA_OR_COMPRESSION')

    def test_new_gzip_representation_is_refused_before_stop_without_decoding(self):
        import gzip
        self.active()
        raw = next(self.data.rglob('*.html'))
        raw.with_suffix('.html.gz').write_bytes(gzip.compress(raw.read_bytes(), mtime=0))
        raw.unlink()
        self.assert_refused_before_stop('SCHEMA_OR_COMPRESSION')
        self.assertFalse(raw.exists())

    def test_schema_drift_is_refused_before_stop(self):
        self.active()
        with closing(sqlite3.connect(self.data / 'generations/initial/library.sqlite3')) as db:
            db.execute('ALTER TABLE library_bookmark ADD COLUMN changed')
            db.commit()
        self.assert_refused_before_stop('SCHEMA_OR_COMPRESSION')

    def test_nonidle_maintenance_or_jobs_are_refused_before_stop(self):
        self.active()
        cases = [
            ('state.sqlite3', 'UPDATE runtime_runtimestate SET maintenance_mode=1', 'UPDATE runtime_runtimestate SET maintenance_mode=0'),
            ('state.sqlite3', "INSERT INTO runtime_maintenanceoperation VALUES('backup','running','{}')", "DELETE FROM runtime_maintenanceoperation WHERE state='running'"),
            ('generations/initial/library.sqlite3', "INSERT INTO library_job VALUES('queued')", 'DELETE FROM library_job'),
        ]
        for name, change, undo in cases:
            with self.subTest(change=change), closing(sqlite3.connect(self.data / name)) as db:
                db.execute(change)
                db.commit()
                self.assert_refused_before_stop('INSTANCE_NOT_IDLE')
                db.execute(undo)
                db.commit()

    def assert_failed_after_stop(self, reason):
        self.assertEqual(self.invoke(), 1, self.output + self.cli_errors)
        self.assertRegex(self.events[-1]['code'], reason)
        self.assertEqual(self.phase(), 'manual_recovery_required')
        self.assertFalse((self.journal / 'rollback-receipt.json').exists())
        self.assertFalse(any(event['event'] == 'complete' for event in self.events))

    def test_compression_between_preflight_and_lock_fails_without_old_restart(self):
        import gzip
        self.active()
        before_env = (self.root / 'production.env').read_bytes()
        def drift(arguments, **kwargs):
            result = self.fake(arguments, **kwargs)
            if 'stop' in arguments:
                raw = next(self.data.rglob('*.html'))
                raw.with_suffix('.html.gz').write_bytes(gzip.compress(raw.read_bytes(), mtime=0))
                raw.unlink()
            return result
        with patch.object(self.op, 'docker', side_effect=drift):
            self.assert_failed_after_stop('SCHEMA_OR_COMPRESSION')
        self.assertFalse(self.fake.running)
        self.assertFalse(any('up' in call for call in self.fake.calls))
        self.assertEqual((self.root / 'production.env').read_bytes(), before_env)
        self.assertEqual(self.fake.current_id, self.fake.new_id)
        self.assertTrue(list(self.data.rglob('*.html.gz')))
        self.assertFalse(list(self.data.rglob('*.html')))

    def test_old_restart_failure_retains_current_databases_and_non_success(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        def fail_restart(image_id):
            if image_id == self.fake.old_id:
                self.fake.running = False
                raise RuntimeError('SYNTHETIC_PRIVATE_RESTART_ERROR')
        self.fake.on_up = fail_restart
        self.assert_failed_after_stop('RuntimeError')
        self.assertEqual(self.data_bytes(), before)
        self.assertNotIn('SYNTHETIC_PRIVATE_RESTART_ERROR', self.output)

    def test_post_restart_health_failure_never_reports_success(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        with patch.object(self.op, 'loopback_ready', side_effect=self.op.UpgradeError('LOOPBACK_NOT_READY')):
            self.assert_failed_after_stop('LOOPBACK_NOT_READY')
        self.assertEqual(self.data_bytes(), before)

    def test_fresh_counts_and_identity_are_captured_locked_after_last_write(self):
        self.active()
        lock, write = self.op.file_lock, self.op.write_json
        held, recorded = [], []
        @contextmanager
        def tracked_lock(path, **kwargs):
            with lock(path, **kwargs):
                held.append(path.name)
                try:
                    yield
                finally:
                    held.pop()
        def last_write(arguments, **kwargs):
            result = self.fake(arguments, **kwargs)
            if 'stop' in arguments:
                self.newer_data()
            return result
        def observe(path, value):
            if path.name in ('rollback-counts-before.json', 'rollback-identity-before.json'):
                self.assertIn('supervisor.lock', held)
                self.assertFalse(self.fake.running)
                recorded.append(path.name)
            write(path, value)
        with patch.object(self.op, 'file_lock', tracked_lock), patch.object(self.op, 'docker', side_effect=last_write), patch.object(self.op, 'write_json', side_effect=observe):
            self.assertEqual(self.invoke(), 0, self.output)
        self.assertCountEqual(recorded, ['rollback-counts-before.json', 'rollback-identity-before.json'])
        result = self.events[-1]
        self.assertTrue(result['business_counts_unchanged'])
        self.assertTrue(result['identity_continuity'])
        self.assertEqual(result['counts']['bookmarks'], 2)
        self.assertEqual(result['identity_counts']['auth_user'], 0)
        self.assertEqual(result['identity_counts']['django_session'], 1)
        self.assertNotIn('SYNTHETIC_CURRENT_SESSION', self.output)
        self.assertEqual(json.loads((self.journal / 'rollback-receipt.json').read_bytes())['counts'], result['counts'])

    def test_old_start_business_loss_fails_without_restoring_current_data(self):
        self.active()
        self.newer_data()
        def lose_data(image_id):
            with closing(sqlite3.connect(self.data / 'generations/initial/library.sqlite3')) as db:
                db.execute('DELETE FROM library_bookmark')
                db.commit()
        self.fake.on_up = lose_data
        self.assert_failed_after_stop('COUNTS_CONTINUITY_FAILED')
        self.assertEqual(self.op.idle_counts(self.data)['bookmarks'], 0)
        self.assertEqual(self.op.idle_counts(self.journal / 'data')['bookmarks'], 1)

    def test_old_start_identity_drift_fails_without_restoring_revoked_credentials(self):
        self.active()
        self.newer_data()
        def revoke_session(image_id):
            with closing(sqlite3.connect(self.data / 'state.sqlite3')) as db:
                db.execute('DELETE FROM django_session')
                db.commit()
        self.fake.on_up = revoke_session
        self.assert_failed_after_stop('IDENTITY_CONTINUITY_FAILED')
        self.assertEqual(self.op.identity(self.data)['tables']['auth_user']['count'], 0)
        self.assertEqual(self.op.identity(self.data)['tables']['django_session']['count'], 0)
        self.assertEqual(self.op.identity(self.journal / 'data')['tables']['auth_user']['count'], 1)

    def test_runtime_drift_during_preflight_scan_is_refused_before_stop(self):
        self.active()
        guard = self.op.rollback_guard
        def drift(*args, **kwargs):
            result = guard(*args, **kwargs)
            if kwargs.get('immutable') is False:
                self.fake.runtime_env.append('BOOKMARK_AUTH_DEBUG=SYNTHETIC_SCAN_DRIFT')
            return result
        with patch.object(self.op, 'rollback_guard', side_effect=drift):
            self.assert_refused_before_stop('RUNTIME_CONFIG')

    def test_environment_drift_during_stop_is_not_overwritten(self):
        self.active()
        before = self.data_bytes()
        changed = (self.root / 'production.env').read_bytes() + b'# SYNTHETIC_EXTERNAL_CHANGE\n'
        def drift(arguments, **kwargs):
            result = self.fake(arguments, **kwargs)
            if 'stop' in arguments:
                (self.root / 'production.env').write_bytes(changed)
            return result
        with patch.object(self.op, 'docker', side_effect=drift):
            self.assert_failed_after_stop('ACTIVE_ENV_DRIFT')
        self.assertEqual((self.root / 'production.env').read_bytes(), changed)
        self.assertEqual(self.data_bytes(), before)
        self.assertFalse(any('up' in call for call in self.fake.calls))
        self.assertEqual((self.root / 'current').resolve().name, 'Linkarium-v0.2.1-deploy')

    def test_prepared_old_metadata_must_bind_expected_base_before_stop(self):
        self.active()
        path = self.journal / 'prepared.json'
        receipt = json.loads(path.read_bytes())
        receipt['old_metadata']['commit'] = '3' * 40
        self.op.write_json(path, receipt)
        self.assert_refused_before_stop('PREPARED_OLD_METADATA_DRIFT')

    def test_prepared_port_drift_is_refused_before_stop(self):
        self.active()
        path = self.journal / 'prepared.json'
        receipt = json.loads(path.read_bytes())
        receipt['port'] = 9000
        self.op.write_json(path, receipt)
        self.assert_refused_before_stop('PREPARED_PORT_DRIFT')

    def test_post_start_environment_byte_drift_is_not_success(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        def drift(image_id):
            (self.root / 'production.env').write_bytes(self.original + b'# changed by external writer\n')
        self.fake.on_up = drift
        self.assert_failed_after_stop('ROLLBACK_ENV_DRIFT')
        self.assertEqual(self.data_bytes(), before)
        self.assertNotEqual((self.root / 'production.env').read_bytes(), self.original)

    def test_post_start_current_pointer_drift_is_not_success(self):
        self.active()
        target = (self.root / 'current').resolve()
        self.fake.on_up = lambda image_id: self.op.set_current(target)
        self.assert_failed_after_stop('CURRENT_BASE_CHANGED|ROLLBACK_CURRENT_DRIFT')
        self.assertEqual((self.root / 'current').resolve(), target)

    def test_post_start_schema_drift_is_not_success(self):
        self.active()
        def drift(image_id):
            with closing(sqlite3.connect(self.data / 'generations/initial/library.sqlite3')) as db:
                db.execute('ALTER TABLE library_bookmark ADD COLUMN changed')
                db.commit()
        self.fake.on_up = drift
        self.assert_failed_after_stop('SCHEMA_OR_COMPRESSION')
        with closing(sqlite3.connect(self.data / 'generations/initial/library.sqlite3')) as db:
            self.assertIn('changed', [row[1] for row in db.execute('PRAGMA table_info(library_bookmark)')])

    def test_post_start_old_overlay_drift_is_not_success(self):
        self.active()
        def drift(image_id):
            self.op.write_json(self.journal / 'old-image.compose.json', {'services': {'app': {'image': image_id, 'pull_policy': 'always'}}})
        self.fake.on_up = drift
        self.assert_failed_after_stop('PINNED_OVERLAY_DRIFT')

    def test_missing_current_identity_key_is_refused_before_stop(self):
        self.active()
        (self.data / 'runtime/secret-key').unlink()
        self.assert_refused_before_stop('IDENTITY_SECRET_KEY_MISSING')

    def test_operator_lock_cleanup_failure_does_not_publish_complete(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        lock = self.op.file_lock
        @contextmanager
        def cleanup_failure(path, **kwargs):
            with lock(path, **kwargs):
                yield
            if path.name == 'v012-operator.lock':
                raise OSError('SYNTHETIC_PRIVATE_LOCK_CLEANUP')
        with patch.object(self.op, 'file_lock', cleanup_failure):
            self.assertEqual(self.invoke(), 1, self.output)
        self.assertEqual(self.phase(), 'manual_recovery_required')
        self.assertFalse(any(event['event'] == 'complete' for event in self.events))
        self.assertNotIn('SYNTHETIC_PRIVATE_LOCK_CLEANUP', self.output)
        self.assertEqual(self.data_bytes(), before)

    def test_old_image_configuration_drift_is_refused_before_stop(self):
        self.active()
        self.fake.images[self.fake.old_id]['env'].append('BOOKMARK_AUTH_DEBUG=SYNTHETIC_OLD_IMAGE_DRIFT')
        self.assert_refused_before_stop('CONFIG')

    def test_expected_base_argument_drift_is_refused_before_stop(self):
        self.active()
        self.args.expected_base = '3' * 40
        self.assert_refused_before_stop('PREPARED_ARGUMENT_DRIFT')

    def test_actual_operator_lock_conflict_refuses_before_stop(self):
        self.active()
        with self.op.file_lock(self.root / 'ops/v012-operator.lock', create=True):
            self.assert_refused_before_stop('WRITER_OR_OPERATOR_LOCKED')

    def test_actual_cross_process_supervisor_lock_conflict_never_starts_old_code(self):
        import subprocess
        import sys
        self.active()
        self.newer_data()
        before = self.data_bytes()
        code = "import fcntl,sys; f=open(sys.argv[1],'r+b'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read(1)"
        child = subprocess.Popen([sys.executable, '-S', '-B', '-c', code, str(self.data / 'runtime/supervisor.lock')],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertIsNotNone(child.stdout)
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline(), b'locked\n')
            self.assert_failed_after_stop('WRITER_OR_OPERATOR_LOCKED')
        finally:
            stdout, stderr = child.communicate(b'x', timeout=15)
        self.assertEqual((child.returncode, stdout, stderr), (0, b'', b''))
        self.assertEqual(self.data_bytes(), before)
        self.assertFalse(any('up' in call for call in self.fake.calls))

    def test_supervisor_lock_cleanup_failure_keeps_non_success_and_current_data(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        lock = self.op.file_lock
        @contextmanager
        def cleanup_failure(path, **kwargs):
            with lock(path, **kwargs):
                yield
            if path.name == 'supervisor.lock':
                raise OSError('SYNTHETIC_LOCK_CLEANUP')
        with patch.object(self.op, 'file_lock', cleanup_failure):
            self.assert_failed_after_stop('OSError')
        self.assertEqual(self.data_bytes(), before)
        self.assertFalse(any('up' in call for call in self.fake.calls))

    def test_new_maintenance_activity_during_stop_is_not_ignored(self):
        self.active()
        def drift(arguments, **kwargs):
            result = self.fake(arguments, **kwargs)
            if 'stop' in arguments:
                with closing(sqlite3.connect(self.data / 'generations/initial/library.sqlite3')) as db:
                    db.execute("INSERT INTO library_job VALUES('queued')")
                    db.commit()
            return result
        with patch.object(self.op, 'docker', side_effect=drift):
            self.assert_failed_after_stop('INSTANCE_NOT_IDLE')
        self.assertEqual(self.op.idle_counts(self.data)['active_jobs'], 1)
        self.assertFalse(any('up' in call for call in self.fake.calls))

    def test_wal_after_stop_is_preserved_and_blocks_immutable_guard(self):
        self.active()
        wal = self.data / 'state.sqlite3-wal'
        def drift(arguments, **kwargs):
            result = self.fake(arguments, **kwargs)
            if 'stop' in arguments:
                wal.write_bytes(b'SYNTHETIC_UNCHECKPOINTED_WAL')
            return result
        with patch.object(self.op, 'docker', side_effect=drift):
            self.assert_failed_after_stop('WAL_OR_JOURNAL')
        self.assertEqual(wal.read_bytes(), b'SYNTHETIC_UNCHECKPOINTED_WAL')
        self.assertFalse(any('up' in call for call in self.fake.calls))

    def test_success_phase_write_failure_is_a_failed_outcome_not_a_retry(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        write = self.op.write_json
        def fail_phase(path, value):
            if path.name == 'phase.json' and value.get('phase') == 'code_rolled_back':
                raise OSError('SYNTHETIC_PHASE_IO_FAILURE')
            write(path, value)
        with patch.object(self.op, 'write_json', side_effect=fail_phase):
            self.assertEqual(self.invoke(), 1, self.output)
        self.assertEqual(self.phase(), 'manual_recovery_required')
        self.assertFalse(any(event['event'] == 'complete' for event in self.events))
        self.assertEqual(self.data_bytes(), before)
        # A persisted receipt is not acceptance when phase/finalization failed.
        self.assertTrue((self.journal / 'rollback-receipt.json').is_file())
        self.fake.calls.clear()
        self.assert_refused_before_stop('PHASE_NOT_ACTIVE')

    def test_stopped_container_port_detachment_does_not_block_code_rollback(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        detached = []
        def stop_network(arguments, **kwargs):
            result = self.fake(arguments, **kwargs)
            if 'stop' in arguments:
                # Moby releases the network and clears NetworkSettings.Ports.
                # Configured HostConfig.PortBindings remain unchanged.
                self.fake.runtime_ports = {}
                detached.append(True)
            return result
        with patch.object(self.op, 'docker', side_effect=stop_network):
            self.assertEqual(self.invoke(), 0, self.output)
        self.assertTrue(detached)
        self.assertEqual(self.data_bytes(), before)
        self.assertTrue(self.fake.running)
        self.assertEqual(self.fake.current_id, self.fake.old_id)
        self.assertEqual(self.fake.runtime_ports, self.fake.ports)
        self.assertEqual(self.phase(), 'code_rolled_back')

    def test_missing_running_port_mapping_still_refuses_before_stop(self):
        self.active()
        self.fake.runtime_ports = {}
        self.assert_refused_before_stop('RUNTIME_CONFIG_MISMATCH')

    def test_cli_rolls_back_active_code_without_rewinding_newer_data(self):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        self.assertEqual(self.invoke(), 0, self.output + self.cli_errors)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertEqual((self.root / 'production.env').read_bytes(), self.original)
        self.assertEqual(self.fake.current_id, self.fake.old_id)
        self.assertTrue(self.fake.running)
        self.assertEqual(self.data_bytes(), before)
        self.assertEqual(self.phase(), 'code_rolled_back')
        self.assertEqual(self.op.idle_counts(self.data)['bookmarks'], 2)
        self.assertEqual(self.op.idle_counts(self.journal / 'data')['bookmarks'], 1)
        self.assertNotIn('SYNTHETIC_CURRENT_SESSION', self.output)
        self.assertNotIn('opaque-$bytes', self.output)
        self.assertFalse(any(call[0] in ('build', 'run', 'pull') for call in self.fake.calls))

    def assert_late_binding_drift_refused(self, change, reason):
        self.active()
        self.newer_data()
        before = self.data_bytes()
        before_identity = self.op.identity(self.data)
        before_counts = self.op.idle_counts(self.data)
        self.assertNotEqual(before_identity, self.op.identity(self.journal / 'data'))
        self.assertNotEqual(before_counts, self.op.idle_counts(self.journal / 'data'))
        write, injected = self.op.write_json, []
        def publish(path, value):
            write(path, value)
            if path == self.journal / 'phase.json' and value.get('phase') == 'rolling_back':
                self.assertTrue(self.fake.running)
                self.assertFalse(any('stop' in call for call in self.fake.calls))
                change()
                injected.append(((self.root / 'current').readlink(),
                                 (self.root / 'production.env').read_bytes()))
        with patch.object(self.op, 'write_json', side_effect=publish):
            code = self.invoke()
        self.assertEqual(len(injected), 1)
        self.boundary_observation = {
            'cli_exit': code, 'events': self.events, 'phase': self.phase(),
            'stop_calls': [call for call in self.fake.calls if 'stop' in call],
            'up_calls': [call for call in self.fake.calls if 'up' in call],
            'data_unchanged': self.data_bytes() == before,
            'identity_unchanged': self.op.identity(self.data) == before_identity,
            'counts_unchanged': self.op.idle_counts(self.data) == before_counts,
            'external_current_preserved': (self.root / 'current').readlink() == injected[0][0],
            'external_env_preserved': (self.root / 'production.env').read_bytes() == injected[0][1],
        }
        self.assertEqual(code, 1, self.output + self.cli_errors)
        self.assertRegex(self.events[-1]['code'], reason)
        self.assertEqual(self.phase(), 'manual_recovery_required')
        self.assertFalse(any(event['event'] == 'complete' for event in self.events))
        self.assertFalse((self.journal / 'rollback-receipt.json').exists())
        for key in ('data_unchanged', 'identity_unchanged', 'counts_unchanged',
                    'external_current_preserved', 'external_env_preserved'):
            self.assertTrue(self.boundary_observation[key], key)
        self.assertEqual(self.boundary_observation['up_calls'], [])
        self.assertEqual(self.boundary_observation['stop_calls'], [],
                         'Observed wrong active binding must be refused before any stop')
        self.assertTrue(self.fake.running)
        self.assertEqual(self.fake.current_id, self.fake.new_id)

    def test_current_reverted_to_old_after_preflight_is_refused_before_stop(self):
        self.assert_late_binding_drift_refused(lambda: self.op.set_current(self.old),
                                              'ROLLBACK_CURRENT_DRIFT|CURRENT_BASE_CHANGED')

    def test_env_reverted_to_original_after_preflight_is_refused_before_stop(self):
        self.assert_late_binding_drift_refused(
            lambda: (self.root / 'production.env').write_bytes(self.original),
            'ROLLBACK_LATEST_ENV_CHANGED|ACTIVE_ENV_DRIFT')


if __name__ == '__main__':
    unittest.main(verbosity=2)
