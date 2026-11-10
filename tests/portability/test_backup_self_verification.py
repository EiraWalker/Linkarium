"""Private backup verification must not become an incomplete restore API."""
from contextlib import closing, contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from runtime import maintenance
from tests.portability import test_artifact_storage_portability as fixtures
from tests.portability import test_schema_upgrade as schema_fixtures


class BackupSelfVerificationTests(unittest.TestCase):
    root: Path
    generation: Path
    database: Path
    name: str
    payload: bytes
    # Reuse the existing public mixed-storage/SQLite fixture without inheriting
    # its tests or changing any prior assertion.
    setUp = fixtures.ArtifactStoragePortabilityTests.setUp
    add_artifact = fixtures.ArtifactStoragePortabilityTests.add_artifact
    legacy_package = fixtures.ArtifactStoragePortabilityTests.legacy_package
    quota = fixtures.ArtifactStoragePortabilityTests.quota

    def source_bytes(self):
        return {p.relative_to(self.generation).as_posix(): p.read_bytes()
                for p in self.generation.rglob('*') if p.is_file()}

    def local_worker(self, action, *, expected_schema=None, **request):
        self.assertIn(action, ('package', 'backup_verify'))
        kwargs = dict(expected_schema=expected_schema, expanded_limit=request['expanded_limit'],
                      compact_artifacts=request.get('compact_artifacts', False))
        if action == 'backup_verify':
            kwargs['verify_only'] = True
        return maintenance._manifest_summary(maintenance._validate_package(
            request['package'], request['candidate'], **kwargs))

    def test_backup_discards_verified_artifacts_before_database_without_gzip_publication(self):
        for index in range(4):
            self.add_artifact(bytes([65 + index]) * 1024**2, f'bulk-{index}', compressed=True)
        before = self.source_bytes()
        real_database = maintenance._validate_database
        real_extract = maintenance._extract_package_file
        roots, database_observations, live_stages = [], [], []
        extracted = []

        def worker(action, **kwargs):
            roots.append(Path(kwargs['candidate']))
            return self.local_worker(action, **kwargs)

        def extract(archive, info, item, target):
            real_extract(archive, info, item, target)
            extracted.append((info.filename, target))
            live_stages.append(len(list(roots[-1].rglob('source.html'))))

        def database(path, **kwargs):
            if roots:
                database_observations.append([
                    p.relative_to(roots[-1]).as_posix() for p in roots[-1].rglob('*')
                    if p.is_file() and p.name != 'library.sqlite3'])
                self.assertEqual(len(extracted), 6)
                self.assertTrue(Path(path).is_file())
            return real_database(path, **kwargs)

        from library.artifact_storage import write_artifact
        with patch.object(maintenance, '_isolated_validation', side_effect=worker), \
                patch.object(maintenance, '_validate_database', side_effect=database), \
                patch.object(maintenance, '_extract_package_file', side_effect=extract), \
                patch('library.artifact_storage.write_artifact', wraps=write_artifact) as publish:
            result = maintenance.create_backup(self.generation, self.root / 'backup.zip')
        self.assertEqual((database_observations, publish.call_count), ([[]], 0))
        self.assertEqual(max(live_stages), 1)
        self.assertEqual(result['manifest']['counts']['artifacts'], 5)
        self.assertEqual(self.source_bytes(), before)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_real_private_worker_preserves_mixed_original_bytes_and_complete_restore_defaults(self):
        payloads = {self.name: self.payload}
        for compressed in (False, True):
            payload = bytes([int(compressed)]) + b'\r\n\x00\xff\xfe<html>not reserialized</html>\r\n' * 4096
            payloads[self.add_artifact(payload, f'binary-{compressed}', compressed=compressed)] = payload
        orphan = hashlib.sha256(b'absent orphan').hexdigest()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('INSERT INTO library_artifact VALUES(?,?,?)',
                               (orphan, f'artifacts/{orphan[:2]}/{orphan}.html', 13))
            connection.commit()
        before = self.source_bytes()
        package = self.root / 'real-worker.zip'
        with patch.object(maintenance, 'validate_package', side_effect=AssertionError('not a restore candidate')), \
                patch.object(maintenance, '_validate_package', side_effect=AssertionError('must run in worker')):
            try:
                result = maintenance.create_backup(self.generation, package)
            except maintenance.MaintenanceError as error:
                self.fail(f'Private backup worker rejected valid mixed storage: {error.code}')
        with zipfile.ZipFile(package) as archive:
            for name, payload in payloads.items():
                self.assertEqual(archive.read(name), payload)
            self.assertEqual(set(archive.namelist()), {'manifest.json', 'library.sqlite3', *payloads})
        from library.artifact_storage import open_artifact
        for compact in (False, True):
            candidate = self.root / f'restored-{compact}'
            manifest = maintenance.validate_package(package, candidate, expected_schema=self.database,
                                                    compact_artifacts=compact)
            self.assertEqual(manifest, result['manifest'])
            self.assertEqual(maintenance.validate_candidate(candidate, expected_schema=self.database), manifest)
            for name, payload in payloads.items():
                if not compact:
                    self.assertEqual((candidate / name).read_bytes(), payload)
                with open_artifact(candidate / name, expected_size=len(payload)) as stream:
                    self.assertEqual(stream.read(), payload)
        self.assertEqual(self.source_bytes(), before)
        self.assertFalse(list(self.root.glob('verify-*')))
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_malformed_success_from_worker_never_publishes_backup(self):
        original = self.source_bytes()
        real_popen = subprocess.Popen
        children = []
        for index, output in enumerate((b'{"ok":true}', b'{"ok":true,"manifest":[]}',
                                       b'{"ok":true,"manifest":{}}')):
            with self.subTest(output=output):
                package = self.root / f'malformed-{index}.zip'

                def malformed_worker(command, **options):
                    script = ('from runtime.maintenance_worker import apply_limits; apply_limits(); '
                              'import sys; sys.stdin.buffer.read(); '
                              f'sys.stdout.buffer.write({output!r}); sys.stdout.buffer.flush()')
                    child = real_popen([sys.executable, '-X', 'utf8', '-c', script], **options)
                    children.append(child)
                    return child

                with patch.object(maintenance.subprocess, 'Popen', side_effect=malformed_worker):
                    with self.assertRaises(maintenance.MaintenanceError) as caught:
                        maintenance.create_backup(self.generation, package)
                self.assertEqual(caught.exception.code, 'RESTORE_RESOURCE_LIMIT')
                self.assertFalse(package.exists())
                self.assertFalse(list(self.root.glob('*.partial')))
                self.assertFalse(list(self.root.glob('verify-*')))
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual(self.source_bytes(), original)

    def rewrite_zip(self, package, mutate):
        with zipfile.ZipFile(package) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(contents['manifest.json'])
        mutate(contents, manifest)
        contents['manifest.json'] = json.dumps(manifest).encode()
        with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in contents.items():
                archive.writestr(name, content)

    def rewrite_database(self, package, sql):
        def mutate(contents, manifest):
            database = self.root / 'modified.sqlite3'
            database.write_bytes(contents['library.sqlite3'])
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(sql)
                connection.commit()
            contents['library.sqlite3'] = database.read_bytes()
            manifest['files']['library.sqlite3'] = {
                'size': len(contents['library.sqlite3']),
                'sha256': hashlib.sha256(contents['library.sqlite3']).hexdigest()}
        self.rewrite_zip(package, mutate)

    def patch_central(self, package, name, offset, fmt, change):
        with zipfile.ZipFile(package) as archive:
            position = archive.start_dir
        data = bytearray(package.read_bytes())
        while data[position:position + 4] == b'PK\x01\x02':
            n, extra, comment = struct.unpack_from('<HHH', data, position + 28)
            if data[position + 46:position + 46 + n].decode() == name:
                old = struct.unpack_from(fmt, data, position + offset)[0]
                struct.pack_into(fmt, data, position + offset, change(old))
                package.write_bytes(data)
                return
            position += 46 + n + extra + comment
        self.fail(f'Missing central directory entry: {name}')

    def assert_final_zip_rejected(self, mutate, code, *, expanded_limit=None):
        before = self.source_bytes()
        package = self.root / 'rejected.zip'
        real_worker = maintenance._isolated_validation
        actions = []

        def worker(action, **request):
            actions.append(action)
            self.assertEqual(action, 'backup_verify')
            self.assertEqual(Path(request['package']), package.with_name('rejected.zip.partial'))
            self.assertFalse(package.exists())
            # Opening/replacing the closed final ZIP, not the source/staging,
            # makes these fault cases exercise actual compressed output reads.
            mutate(Path(request['package']))
            if expanded_limit is not None:
                request['expanded_limit'] = expanded_limit
            return real_worker(action, **request)

        with patch.object(maintenance, '_isolated_validation', side_effect=worker):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(actions, ['backup_verify'])
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertFalse(list(self.root.glob('verify-*')))
        self.assertFalse(list(self.root.glob('backup-*')))
        self.assertEqual(self.source_bytes(), before)

    def test_final_zip_crc_is_read_not_just_source_or_directory(self):
        self.assert_final_zip_rejected(
            lambda p: self.patch_central(p, self.name, 16, '<I', lambda crc: crc ^ 1), 'BACKUP_INVALID')

    def test_final_zip_payload_sha_is_read_to_eof(self):
        def mutate(contents, manifest):
            contents[self.name] = self.payload[:-1] + bytes([self.payload[-1] ^ 1])
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, mutate), 'BACKUP_HASH_MISMATCH')

    def test_final_zip_manifest_size_is_checked(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, lambda c, m:
            m['files'][self.name].update(size=len(self.payload) + 1)), 'BACKUP_HASH_MISMATCH')

    def test_final_zip_manifest_boolean_size_is_rejected(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, lambda c, m:
            m['files'][self.name].update(size=True)), 'BACKUP_MANIFEST_INVALID')

    def test_final_zip_canonical_identity_is_checked(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, lambda c, m:
            m['files'][self.name].update(sha256='f' * 64)), 'BACKUP_ARTIFACT_INVALID')

    def test_final_zip_manifest_counts_are_cross_checked(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, lambda c, m:
            m['counts'].update(snapshots=1)), 'BACKUP_MANIFEST_INVALID')

    def test_final_zip_referenced_member_cannot_be_omitted(self):
        def mutate(contents, manifest):
            contents.pop(self.name)
            manifest['files'].pop(self.name)
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, mutate), 'BACKUP_MANIFEST_INVALID')

    def test_final_zip_unreferenced_extra_member_is_rejected(self):
        def mutate(contents, manifest):
            payload = b'not in database'
            sha = hashlib.sha256(payload).hexdigest()
            name = f'artifacts/{sha[:2]}/{sha}.html'
            contents[name] = payload
            manifest['files'][name] = {'sha256': sha, 'size': len(payload)}
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, mutate), 'BACKUP_MANIFEST_INVALID')

    def test_final_zip_path_traversal_is_rejected(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, lambda c, m:
            c.update({'../escape': b'no'})), 'BACKUP_UNSAFE_PATH')
        self.assertFalse((self.root / 'escape').exists())

    def test_final_zip_symlink_entry_is_rejected(self):
        self.assert_final_zip_rejected(lambda p: self.patch_central(
            p, self.name, 38, '<I', lambda _: 0o120777 << 16), 'BACKUP_UNSAFE_ENTRY')

    def test_final_zip_encrypted_entry_is_rejected(self):
        self.assert_final_zip_rejected(lambda p: self.patch_central(
            p, self.name, 8, '<H', lambda flags: flags | 1), 'BACKUP_UNSAFE_ENTRY')

    def test_final_zip_duplicate_member_is_rejected(self):
        def mutate(package):
            with self.assertWarns(UserWarning), zipfile.ZipFile(package, 'a') as archive:
                archive.writestr(self.name, self.payload)
        self.assert_final_zip_rejected(mutate, 'BACKUP_DUPLICATE_PATH')

    def test_final_zip_duplicate_json_key_is_rejected(self):
        def mutate(package):
            with zipfile.ZipFile(package) as archive:
                contents = {name: archive.read(name) for name in archive.namelist()}
            contents['manifest.json'] = b'{"format":"bookmarklib",' + contents['manifest.json'][1:]
            with zipfile.ZipFile(package, 'w') as archive:
                for name, value in contents.items():
                    archive.writestr(name, value)
        self.assert_final_zip_rejected(mutate, 'BACKUP_MANIFEST_INVALID')

    def test_final_zip_format_version_is_checked(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, lambda c, m:
            m.update(format_version=2)), 'BACKUP_VERSION_UNSUPPORTED')

    def test_final_zip_logical_expansion_limit_is_not_relaxed(self):
        self.assert_final_zip_rejected(lambda p: None, 'BACKUP_EXPANSION_LIMIT', expanded_limit=1)

    def test_final_zip_artifact_limit_is_not_relaxed(self):
        self.assert_final_zip_rejected(lambda p: self.patch_central(p, self.name, 24, '<I',
            lambda _: maintenance.MAX_ARTIFACT_BYTES + 1), 'BACKUP_ENTRY_TOO_LARGE')

    def test_final_zip_foreign_keys_still_validate_after_discard(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            "UPDATE library_snapshotversion SET artifact_id='missing'"), 'BACKUP_FOREIGN_KEY_INVALID')

    def test_final_zip_database_artifact_metadata_still_matches_manifest(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            'UPDATE library_artifact SET byte_size=byte_size+1'), 'BACKUP_ARTIFACT_INVALID')

    def test_final_zip_orphan_metadata_cannot_escape(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            "INSERT INTO library_artifact VALUES('" + 'f' * 64 + "','../escape',0)"), 'BACKUP_ARTIFACT_INVALID')

    def test_final_zip_schema_trigger_is_rejected(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            'CREATE TRIGGER evil AFTER INSERT ON library_bookmark BEGIN DELETE FROM library_bookmark; END;'),
            'BACKUP_SCHEMA_UNSAFE')

    def test_final_zip_unknown_schema_is_not_a_trusted_upgrade(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            'CREATE INDEX library_untrusted ON library_artifact(byte_size)'), 'BACKUP_SCHEMA_INCOMPATIBLE')

    def test_final_zip_database_header_corruption_is_rejected(self):
        def mutate(contents, manifest):
            contents['library.sqlite3'] = b'broken' + contents['library.sqlite3'][6:]
            manifest['files']['library.sqlite3']['sha256'] = hashlib.sha256(contents['library.sqlite3']).hexdigest()
        self.assert_final_zip_rejected(lambda p: self.rewrite_zip(p, mutate), 'BACKUP_INVALID')

    def test_final_zip_folder_cycle_is_rejected(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            "INSERT INTO library_folder VALUES('cycle','cycle')"), 'BACKUP_FOLDER_INVALID')

    def test_final_zip_capture_pointer_is_checked(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            "UPDATE library_capturestate SET bookmark_id='different'"), 'BACKUP_CURRENT_POINTER_INVALID')

    def test_final_zip_variant_is_checked(self):
        self.assert_final_zip_rejected(lambda p: self.rewrite_database(p,
            "UPDATE library_snapshotversion SET variant='tablet'; UPDATE library_capturestate SET variant='tablet';"),
            'BACKUP_VARIANT_INVALID')

    def test_database_first_legacy_order_retains_database_until_last_validation(self):
        package = self.legacy_package()
        candidate = self.root / 'private-owned-scratch'
        original_extract, original_database = maintenance._extract_package_file, maintenance._validate_database
        events, raw_paths = [], []

        def extract(archive, info, item, target):
            self.assertFalse(any(path.exists() for path in raw_paths))
            original_extract(archive, info, item, target)
            events.append(info.filename)
            if info.filename != 'library.sqlite3':
                raw_paths.append(target)
                self.assertTrue((candidate / 'library.sqlite3').exists())

        def database(path, **kwargs):
            self.assertEqual(events, ['library.sqlite3', self.name])
            self.assertFalse(any(p.exists() for p in raw_paths))
            events.append('validate_database')
            return original_database(path, **kwargs)

        with patch.object(maintenance, '_extract_package_file', side_effect=extract), \
                patch.object(maintenance, '_validate_database', side_effect=database):
            manifest = maintenance._validate_package(package, candidate, expected_schema=self.database, verify_only=True)
        self.assertEqual(events[-1], 'validate_database')
        self.assertEqual(maintenance._candidate_manifest(candidate), manifest)
        self.assertEqual({p.name for p in candidate.rglob('*') if p.is_file()}, {'library.sqlite3', 'manifest.json'})
        # Private verification scratch must not pass the public restore gate.
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_candidate(candidate, expected_schema=self.database)
        self.assertEqual(caught.exception.code, 'RESTORE_CANDIDATE_CHANGED')

    def test_private_raw_budget_does_not_relax_compact_restore_workspace(self):
        package = self.legacy_package()
        # Existing quota counts actual file writes. This fits raw staging, DB,
        # manifest and reserve, but not compact restore's publication workspace.
        with self.quota(maintenance.DISK_RESERVE_BYTES + 512 * 1024):
            maintenance._validate_package(package, self.root / 'private-budget',
                                          expected_schema=self.database, verify_only=True)
        with self.quota(maintenance.DISK_RESERVE_BYTES + 512 * 1024):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance._validate_package(package, self.root / 'restore-budget',
                                              expected_schema=self.database, compact_artifacts=True)
        self.assertEqual(caught.exception.code, 'INSUFFICIENT_DISK_SPACE')

    def test_package_action_cannot_enable_verify_only_with_untrusted_flag(self):
        candidate = self.root / 'forced-restore'
        maintenance._isolated_validation('package', package=str(self.legacy_package()), candidate=str(candidate),
            expanded_limit=maintenance.MAX_EXPANDED_BYTES, expected_schema=self.database, verify_only=True)
        self.assertEqual((candidate / self.name).read_bytes(), self.payload)
        maintenance.validate_candidate(candidate, expected_schema=self.database)
        with self.assertRaises(TypeError):
            maintenance.validate_package(self.legacy_package(), self.root / 'public-mode', verify_only=True)

    def test_worker_failure_and_malformed_output_clear_owned_scratch(self):
        original = self.source_bytes()
        real_popen = subprocess.Popen
        cases = [(b'', 1), (b'not JSON', 0), (b'\xff', 0), (b'[]', 0),
                 (b'{"ok":true}' + b' ' * 4096, 0), (b'{"ok":1}', 0),
                 (b'{"error":"RESTORE_LIMITS_UNAVAILABLE"}', 1)]
        for index, (output, status) in enumerate(cases):
            with self.subTest(index=index):
                children = []
                package = self.root / f'worker-rejection-{index}.zip'

                def worker(command, **options):
                    script = ('from runtime.maintenance_worker import apply_limits; apply_limits(); '
                        'import sys,json,pathlib; r=json.load(sys.stdin); '
                        'pathlib.Path(r["candidate"],"unfinished").write_bytes(b"partial"); '
                        f'sys.stdout.buffer.write({output!r}); sys.stdout.buffer.flush(); sys.exit({status})')
                    child = real_popen([sys.executable, '-X', 'utf8', '-c', script], **options)
                    children.append(child)
                    return child

                with patch.object(maintenance.subprocess, 'Popen', side_effect=worker), \
                        patch.object(maintenance, '_clear_candidate', wraps=maintenance._clear_candidate) as clear:
                    with self.assertRaises(maintenance.MaintenanceError) as caught:
                        maintenance.create_backup(self.generation, package)
                self.assertEqual(caught.exception.code,
                    'RESTORE_LIMITS_UNAVAILABLE' if index == len(cases) - 1 else 'RESTORE_RESOURCE_LIMIT')
                self.assertEqual(clear.call_count, 1)
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertFalse(package.exists())
                self.assertFalse(list(self.root.glob('*.partial')))
                self.assertFalse(list(self.root.glob('verify-*')))
        self.assertEqual(self.source_bytes(), original)

    def test_real_worker_wall_timeout_is_killed_reaped_and_not_published(self):
        real_popen, children = subprocess.Popen, []
        package = self.root / 'stalled.zip'
        before = self.source_bytes()

        def stalled(command, **options):
            script = ('from runtime.maintenance_worker import apply_limits; apply_limits(); '
                      'import sys,json,pathlib,time; r=json.load(sys.stdin); '
                      'pathlib.Path(r["candidate"],"unfinished").write_bytes(b"partial"); time.sleep(30)')
            child = real_popen([sys.executable, '-X', 'utf8', '-c', script], **options)
            children.append(child)
            return child

        with patch.object(maintenance.subprocess, 'Popen', side_effect=stalled), \
                patch('runtime.maintenance_worker.WALL_SECONDS', 0.5):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.code, 'RESTORE_RESOURCE_LIMIT')
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertFalse(list(self.root.glob('verify-*')))
        self.assertEqual(self.source_bytes(), before)

    def test_real_private_worker_cpu_exhaustion_prevents_publication(self):
        real_popen, children = subprocess.Popen, []
        before = self.source_bytes()
        package = self.root / 'cpu-exhausted.zip'

        def cpu_limited(command, **options):
            # Tighten only this child; keep real main/dispatch, 768 MiB memory,
            # and the existing parent deadline. No production limit is raised.
            script = '''
from runtime import maintenance_worker as worker
limit = worker.apply_limits
worker.apply_limits = lambda: limit(cpu_seconds=1)
from runtime import maintenance
real = maintenance._extract_package_file
def busy(*args, **kwargs):
    real(*args, **kwargs)
    while True: pass
maintenance._extract_package_file = busy
raise SystemExit(worker.main())
'''
            child = real_popen([sys.executable, '-X', 'utf8', '-c', script], **options)
            children.append(child)
            return child

        with patch.object(maintenance.subprocess, 'Popen', side_effect=cpu_limited):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.code, 'RESTORE_RESOURCE_LIMIT')
        self.assertTrue(all(child.poll() is not None and child.returncode != 0 for child in children))
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertFalse(list(self.root.glob('verify-*')))
        self.assertEqual(self.source_bytes(), before)

    def test_real_private_worker_memory_exhaustion_prevents_publication(self):
        real_popen = subprocess.Popen
        package = self.root / 'memory-exhausted.zip'

        def memory_limited(command, **options):
            # Existing 768 MiB boundary, real allocation failure inside the
            # same validator, not a manually constructed resource error result.
            script = '''
from runtime import maintenance_worker as worker
from runtime import maintenance
real = maintenance._extract_package_file
def allocate(*args, **kwargs):
    real(*args, **kwargs)
    bytearray(worker.MEMORY_BYTES * 2)
maintenance._extract_package_file = allocate
raise SystemExit(worker.main())
'''
            return real_popen([sys.executable, '-X', 'utf8', '-c', script], **options)

        before = self.source_bytes()
        with patch.object(maintenance.subprocess, 'Popen', side_effect=memory_limited):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.code, 'RESTORE_RESOURCE_LIMIT')
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertFalse(list(self.root.glob('verify-*')))
        self.assertEqual(self.source_bytes(), before)

    def test_cleanup_failure_after_success_prevents_publication(self):
        real_temporary = tempfile.TemporaryDirectory
        before = self.source_bytes()
        package = self.root / 'cleanup-failed.zip'
        paths = []

        @contextmanager
        def temporary(*args, **kwargs):
            with real_temporary(*args, **kwargs) as path:
                yield path
                if kwargs.get('prefix') == 'verify-':
                    paths.append(Path(path))
                    raise OSError(errno.EIO, 'injected verification cleanup failure')

        with patch.object(maintenance.tempfile, 'TemporaryDirectory', side_effect=temporary):
            with self.assertRaises(OSError) as caught:
                maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(len(paths), 1)
        self.assertFalse(any(path.exists() for path in paths))
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertEqual(self.source_bytes(), before)

    def test_failed_worker_cleanup_exception_still_cannot_publish(self):
        package = self.root / 'failed-cleanup.zip'
        before = self.source_bytes()

        def worker(action, **request):
            Path(request['candidate'], 'unfinished').write_bytes(b'partial')
            raise maintenance.MaintenanceError('BACKUP_INVALID')

        with patch.object(maintenance, '_isolated_validation', side_effect=worker), \
                patch.object(maintenance, '_clear_candidate', side_effect=OSError(errno.EIO, 'cleanup fault')):
            with self.assertRaises(OSError):
                maintenance.create_backup(self.generation, package)
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertFalse(list(self.root.glob('verify-*')))
        self.assertEqual(self.source_bytes(), before)

    def test_nonempty_verification_directory_is_rejected_before_worker(self):
        real_temporary = tempfile.TemporaryDirectory
        package = self.root / 'nonempty.zip'

        @contextmanager
        def temporary(*args, **kwargs):
            with real_temporary(*args, **kwargs) as path:
                if kwargs.get('prefix') == 'verify-':
                    Path(path, 'unexpected').write_bytes(b'not empty')
                yield path

        with patch.object(maintenance.tempfile, 'TemporaryDirectory', side_effect=temporary), \
                patch.object(maintenance, '_isolated_validation') as worker:
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        worker.assert_not_called()
        self.assertEqual(caught.exception.code, 'RESTORE_CANDIDATE_NOT_EMPTY')
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_raw_stage_cleanup_failure_prevents_publication(self):
        real_rmtree = shutil.rmtree
        failed = []
        before = self.source_bytes()
        package = self.root / 'raw-cleanup-failed.zip'

        def rmtree(path, *args, **kwargs):
            if Path(path).name.startswith('restore-') and not failed:
                failed.append(Path(path))
                raise OSError(errno.EIO, 'injected raw temporary cleanup error')
            return real_rmtree(path, *args, **kwargs)

        with patch.object(maintenance, '_isolated_validation', side_effect=self.local_worker), \
                patch.object(shutil, 'rmtree', side_effect=rmtree):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        self.assertEqual(caught.exception.code, 'BACKUP_INVALID')
        self.assertEqual(len(failed), 1)
        self.assertFalse(failed[0].exists())
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertFalse(list(self.root.glob('verify-*')))
        self.assertEqual(self.source_bytes(), before)

    def test_linked_verification_ancestor_is_rejected_including_windows_junction(self):
        outside = self.root / 'outside-ancestor'
        outside.mkdir()
        precious = outside / 'precious'
        precious.write_bytes(b'never traverse')
        link = self.root / 'ancestor'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(link.is_junction())
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(outside, target_is_directory=True)
            self.addCleanup(link.unlink)
        owned = link / 'empty'
        owned.mkdir()
        real_temporary = tempfile.TemporaryDirectory

        @contextmanager
        def temporary(*args, **kwargs):
            if kwargs.get('prefix') == 'verify-':
                yield str(owned)
            else:
                with real_temporary(*args, **kwargs) as path:
                    yield path

        package = self.root / 'ancestor.zip'
        with patch.object(maintenance.tempfile, 'TemporaryDirectory', side_effect=temporary), \
                patch.object(maintenance, '_isolated_validation') as worker:
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        worker.assert_not_called()
        self.assertEqual(caught.exception.code, 'BACKUP_UNSAFE_PATH')
        self.assertEqual(precious.read_bytes(), b'never traverse')
        self.assertEqual(list(owned.iterdir()), [])
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_linked_verification_directory_never_traverses_outside(self):
        real_temporary = tempfile.TemporaryDirectory
        outside = self.root / 'outside'
        outside.mkdir()
        precious = outside / 'precious'
        precious.write_bytes(b'keep source bytes')
        link = self.root / 'verify-link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows symlink creation privilege unavailable')
            raise
        self.addCleanup(link.unlink, missing_ok=True)

        @contextmanager
        def temporary(*args, **kwargs):
            if kwargs.get('prefix') == 'verify-':
                yield str(link)
            else:
                with real_temporary(*args, **kwargs) as path:
                    yield path

        package = self.root / 'linked.zip'
        with patch.object(maintenance.tempfile, 'TemporaryDirectory', side_effect=temporary), \
                patch.object(maintenance, '_isolated_validation') as worker:
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(self.generation, package)
        worker.assert_not_called()
        self.assertEqual(caught.exception.code, 'BACKUP_UNSAFE_PATH')
        self.assertEqual(precious.read_bytes(), b'keep source bytes')
        self.assertFalse(package.exists())
        self.assertFalse(list(self.root.glob('*.partial')))


class BackupSelfVerificationLegacyTests(unittest.TestCase):
    # Reuse the shipped old-schema migration+seed fixture, not a schema mock.
    root: Path
    expected: Path

    @classmethod
    def setUpClass(cls):
        schema_fixtures.PreviousSchemaTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        schema_fixtures.PreviousSchemaTests.tearDownClass.__func__(cls)
    run_command = classmethod(schema_fixtures.PreviousSchemaTests.run_command.__func__)
    old_library = schema_fixtures.PreviousSchemaTests.old_library

    def test_real_old_schema_upgrade_keeps_database_checks_without_retained_html(self):
        generation, fixture = self.old_library(self.root / 'previous', '0003_classificationtaxonomy_folder_created_by_ai')
        source = generation / 'library.sqlite3'
        original = source.read_bytes()
        package = self.root / 'previous.zip'
        maintenance.create_backup(generation, package)
        scratch = self.root / 'private-verification'
        summary = maintenance._isolated_validation('backup_verify', package=str(package), candidate=str(scratch),
            expected_schema=self.expected, expanded_limit=maintenance.MAX_EXPANDED_BYTES)
        manifest = maintenance._candidate_manifest(scratch)
        self.assertEqual(summary, maintenance._manifest_summary(manifest))
        self.assertEqual(maintenance.schema_signature(scratch / 'library.sqlite3'), maintenance.schema_signature(self.expected))
        self.assertEqual(manifest['files']['library.sqlite3']['sha256'], maintenance.digest(scratch / 'library.sqlite3'))
        self.assertFalse(list(scratch.rglob('*.html')))
        self.assertFalse(list(scratch.rglob('*.gz')))
        with closing(sqlite3.connect(scratch / 'library.sqlite3')) as connection:
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            self.assertEqual(connection.execute('SELECT notes FROM library_bookmark').fetchone(), ('retain previous backup 中文',))
            self.assertEqual(connection.execute('SELECT current_version_id FROM library_capturestate').fetchone(), (fixture['snapshot'],))
            self.assertEqual([row[0] for row in connection.execute(
                "SELECT name FROM django_migrations WHERE app='library' ORDER BY name")],
                list(schema_fixtures.TRUSTED_LIBRARY_MIGRATIONS))
        self.assertEqual(source.read_bytes(), original)
        # Public defaults still own a complete upgraded, confirmable candidate.
        for compact in (False, True):
            candidate = self.root / f'complete-{compact}'
            restored = maintenance.validate_package(package, candidate, expected_schema=self.expected, compact_artifacts=compact)
            self.assertEqual(maintenance.validate_candidate(candidate, expected_schema=self.expected), restored)
        self.assertEqual(source.read_bytes(), original)
