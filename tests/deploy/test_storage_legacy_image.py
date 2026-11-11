"""Real IMAGE_CHECK on scratch file trees; NOT Docker/production acceptance.

Only /app path resolution is relocated in a fresh Python subprocess. The
operator's exact IMAGE_CHECK string, file walk, hashes and equality assertion
execute unchanged. Prepare's other Docker commands use the original protocol
fixture. Small synthetic pins replace the production baseline for these tests.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

import test_storage_upgrade as fixtures


# Do not replace/filter IMAGE_CHECK text or fabricate its verification response.
# Relocate constructors and relative_to('/app') only; all I/O and hashing is real.
# Preserve Linux relative-name spelling when the host subprocess is on Windows.
IMAGE_PROBE = '''import os,pathlib,sys
root=sys.argv[1]
def locate(value):
    text=os.fspath(value)
    return root+text[4:] if text=='/app' or text.startswith('/app/') else value
class ImagePath(type(pathlib.Path())):
    def relative_to(self, other, *args, **kwargs):
        return pathlib.PurePosixPath(super().relative_to(locate(other), *args, **kwargs).as_posix())
pathlib.Path=lambda *parts: ImagePath(*(locate(p) for p in parts))
exec(compile(sys.argv[2], '<IMAGE_CHECK>', 'exec'), {})
'''


class LegacyImageTests(unittest.TestCase):
    root: Path
    op: ModuleType
    new_tree: Path
    setUp = fixtures.SafetyTests.setUp
    release = fixtures.SafetyTests.release
    manifest = fixtures.SafetyTests.manifest
    source_data = fixtures.SafetyTests.source_data
    deployment = fixtures.SafetyTests.deployment

    def image_tree(self, release, name):
        root = self.root / name
        for relative in self.op.application_files(release):
            source = release / ('deploy/requirements.lock' if relative == 'requirements.lock' else relative)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        return root

    def probe(self, tree, arguments, **options):
        self.assertEqual(arguments[-2:], ['-c', self.op.IMAGE_CHECK])
        self.assertEqual(arguments[arguments.index('--network') + 1], 'none')
        self.assertEqual(arguments[arguments.index('--user') + 1], '10001:10001')
        self.assertEqual(arguments[arguments.index('--memory') + 1], '256m')
        self.assertEqual(arguments[arguments.index('--pids-limit') + 1], '64')
        self.assertIn('--read-only', arguments)
        self.assertIn('--pull=never', arguments)
        self.assertNotIn('--mount', arguments)
        self.assertNotIn('-v', arguments)
        result = subprocess.run([sys.executable, '-S', '-B', '-c', IMAGE_PROBE,
                                 str(tree), arguments[-1]], input=options['input'],
                                capture_output=True, timeout=30, check=False)
        self.probes.append({'image_id': arguments[-3], 'expected': json.loads(options['input']),
                            'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode:
            self.assertIn(b'AssertionError', result.stderr, result.stderr.decode())
            raise self.op.UpgradeError('DOCKER_COMMAND_FAILED')
        return result.stdout

    def pin_image_fixture(self, old, image_id):
        self.extra_path = 'frontend/dist/assets/index-D4nBYapU.js'
        self.extra_bytes = b'// synthetic retained public bundle; not production bytes\n'
        self.extra_sha = hashlib.sha256(self.extra_bytes).hexdigest()
        self.old_tree = self.image_tree(old, 'fake-image-old')
        self.extra = self.old_tree / self.extra_path
        self.extra.parent.mkdir(parents=True)
        self.extra.write_bytes(self.extra_bytes)
        self.baseline = {'image_id': image_id, 'release': str(old),
                         'version': 'v0.2.0', 'commit': '1' * 40,
                         'manifest_sha256': self.op.digest(old / 'SHA256SUMS'),
                         'files': {self.extra_path: self.extra_sha},
                         'provenance': {'evidence': 'synthetic-public-asset-fixture',
                                        'retained_releases': ['synthetic-v0.1.2'],
                                        'asset_bytes': len(self.extra_bytes)}}
        pin = patch.object(self.op, 'LEGACY_BASE_IMAGE', self.baseline, create=True)
        pin.start()
        self.addCleanup(pin.stop)
        self.probes, self.target_extra = [], False

    def pinned_deployment(self):
        args, fake, old, original = self.deployment()
        self.pin_image_fixture(old, fake.old_id)
        def docker(arguments, **options):
            if arguments[0] == 'run':
                fake.calls.append(list(arguments))
                tree = self.old_tree if arguments[-3] == fake.old_id else self.new_tree
                return self.probe(tree, arguments, **options)
            result = fake(arguments, **options)
            if arguments[0] == 'build':
                self.new_tree = self.image_tree(Path(options['cwd']), 'fake-image-new')
                if self.target_extra:
                    target = self.new_tree / self.extra_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(self.extra_bytes)
            return result
        protocol = patch.object(self.op, 'docker', side_effect=docker)
        protocol.start()
        self.addCleanup(protocol.stop)
        return args, fake, old, original

    def pinned_source(self):
        old = self.release('v0.2.0', '1' * 40)
        image_id = 'sha256:' + '1' * 64
        self.pin_image_fixture(old, image_id)
        journal = self.root / 'source-proof'
        journal.mkdir()
        protocol = patch.object(self.op, 'docker',
                                side_effect=lambda a, **kw: self.probe(self.old_tree, a, **kw))
        protocol.start()
        self.addCleanup(protocol.stop)
        return image_id, old, journal

    def assert_image_rejected(self, image_id, release, journal):
        with self.assertRaisesRegex(self.op.UpgradeError, '^DOCKER_COMMAND_FAILED$'):
            self.op.verify_image_source(image_id, release, base_journal=journal)
        self.assertEqual(self.probes[-1]['exit'], 1)
        self.assertIn(b'File "<IMAGE_CHECK>"', self.probes[-1]['stderr'])
        self.assertFalse((journal / 'base-source-proof.json').exists())

    @unittest.skipUnless(os.name == 'posix', 'prepare fixture uses a POSIX current symlink')
    def test_prepare_accepts_exact_declared_legacy_inventory_only_for_old_base(self):
        args, fake, old, original = self.pinned_deployment()
        self.extra.unlink()
        self.op.verify_image_source(fake.old_id, old)  # Positive control for real path/hash I/O.
        self.extra.write_bytes(self.extra_bytes)
        self.probes.clear()
        expected = {**self.op.application_files(old), self.extra_path: self.extra_sha}
        try:
            receipt = self.op.prepare(args)
        except self.op.UpgradeError as error:
            self.fail('Exact declared legacy base inventory was refused: ' + str(error) + '\n' +
                      self.probes[-1]['stderr'].decode())
        self.assertEqual([p['exit'] for p in self.probes], [0, 0])
        self.assertEqual(self.probes[0]['expected'], expected)
        self.assertEqual(receipt['old_source_sha256'],
                         hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest())
        self.assertEqual(self.probes[1]['expected'], self.op.application_files(Path(receipt['new_release'])))
        self.assertNotIn(self.extra_path, self.probes[1]['expected'])
        self.assertEqual((self.root / 'production.env').read_bytes(), original)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse(any('stop' in call or 'up' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'prepare fixture uses a POSIX current symlink')
    def test_prepare_records_complete_base_proof_and_explicit_supplement_provenance(self):
        args, fake, old, _ = self.pinned_deployment()
        receipt = self.op.prepare(args)
        journal = self.op.journal_path(args)
        path = journal / 'base-source-proof.json'
        self.assertTrue(path.is_file(), 'Complete checked base inventory/provenance must be retained')
        proof = json.loads(path.read_bytes())
        self.assertEqual(proof, {'image_id': fake.old_id, 'release': str(old),
                                'release_manifest_sha256': self.baseline['manifest_sha256'],
                                'source_sha256': receipt['old_source_sha256'],
                                'files': self.probes[0]['expected'],
                                'legacy_inventory': self.baseline})
        self.assertEqual(receipt['base_source_proof_sha256'], self.op.digest(path))
        self.assertEqual(json.loads((journal / 'prepared.json').read_bytes()), receipt)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.extra_bytes, path.read_bytes())

    def test_altered_legacy_asset_hash_is_rejected_even_with_same_path_and_size(self):
        image_id, release, journal = self.pinned_source()
        self.extra.write_bytes(b'X' + self.extra_bytes[1:])
        self.assertEqual(self.extra.stat().st_size, len(self.extra_bytes))
        self.assert_image_rejected(image_id, release, journal)
        self.assertEqual(self.probes[-1]['expected'][self.extra_path], self.extra_sha)

    def test_bound_base_source_proof_checks_complete_inventory_on_real_files(self):
        image_id, release, journal = self.pinned_source()
        proof_sha = self.op.verify_image_source(image_id, release, base_journal=journal)
        proof = json.loads((journal / 'base-source-proof.json').read_bytes())
        expected = {**self.op.application_files(release), self.extra_path: self.extra_sha}
        self.assertEqual(proof['files'], expected)
        self.assertEqual(proof['legacy_inventory'], self.baseline)
        self.assertEqual(proof_sha, hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest())
        self.assertEqual(self.probes[-1]['exit'], 0)

    def test_unexpected_second_extra_is_rejected_even_with_declared_asset_bytes(self):
        image_id, release, journal = self.pinned_source()
        (self.extra.parent / 'another-bundle.js').write_bytes(self.extra_bytes)
        self.assert_image_rejected(image_id, release, journal)
        self.assertEqual(self.probes[-1]['expected'],
                         {**self.op.application_files(release), self.extra_path: self.extra_sha})

    def test_missing_original_image_files_are_rejected(self):
        image_id, release, journal = self.pinned_source()
        for name in ('backend/supervisor.py', 'frontend/dist/index.html', 'LICENSE', 'requirements.lock'):
            with self.subTest(path=name):
                path = self.old_tree / name
                original = path.read_bytes()
                path.unlink()
                try:
                    self.assert_image_rejected(image_id, release, journal)
                    self.assertIn(name, self.probes[-1]['expected'])
                finally:
                    path.write_bytes(original)

    def test_changed_original_image_files_are_rejected(self):
        image_id, release, journal = self.pinned_source()
        for name in ('backend/supervisor.py', 'frontend/dist/index.html', 'LICENSE', 'requirements.lock'):
            with self.subTest(path=name):
                path = self.old_tree / name
                original = path.read_bytes()
                path.write_bytes(b'X' + original[1:])
                try:
                    self.assert_image_rejected(image_id, release, journal)
                    self.assertEqual(self.probes[-1]['expected'][name], hashlib.sha256(original).hexdigest())
                finally:
                    path.write_bytes(original)

    def test_declared_legacy_asset_is_required_not_optional(self):
        image_id, release, journal = self.pinned_source()
        self.extra.unlink()
        self.assert_image_rejected(image_id, release, journal)
        self.assertIn(self.extra_path, self.probes[-1]['expected'])

    def test_wrong_image_id_never_receives_the_legacy_supplement(self):
        _, release, journal = self.pinned_source()
        self.assert_image_rejected('sha256:' + '9' * 64, release, journal)
        self.assertNotIn(self.extra_path, self.probes[-1]['expected'])

    def test_other_base_image_with_exact_release_inventory_still_succeeds(self):
        _, release, journal = self.pinned_source()
        self.extra.unlink()
        proof_sha = self.op.verify_image_source('sha256:' + '9' * 64, release, base_journal=journal)
        proof = json.loads((journal / 'base-source-proof.json').read_bytes())
        self.assertIsNone(proof['legacy_inventory'])
        self.assertEqual(proof['files'], self.op.application_files(release))
        self.assertEqual(proof['source_sha256'], proof_sha)
        self.assertEqual(self.probes[-1]['exit'], 0)

    def test_same_image_and_bytes_at_wrong_release_path_are_rejected(self):
        image_id, release, journal = self.pinned_source()
        different = release.with_name('different-release-location')
        shutil.copytree(release, different)
        with self.assertRaisesRegex(self.op.UpgradeError, '^LEGACY_BASE_IDENTITY_MISMATCH$'):
            self.op.verify_image_source(image_id, different, base_journal=journal)
        self.assertEqual(self.probes, [])
        self.assertFalse((journal / 'base-source-proof.json').exists())

    def test_valid_but_changed_release_manifest_is_not_the_pinned_baseline(self):
        image_id, release, journal = self.pinned_source()
        path = release / 'SHA256SUMS'
        path.write_bytes(b''.join(reversed(path.read_bytes().splitlines(keepends=True))))
        self.op.verify_release(release, 'v0.2.0', '1' * 40)  # Still internally valid.
        with self.assertRaisesRegex(self.op.UpgradeError, '^LEGACY_BASE_IDENTITY_MISMATCH$'):
            self.op.verify_image_source(image_id, release, base_journal=journal)
        self.assertEqual(self.probes, [])

    def test_release_metadata_tampering_is_not_hidden_by_unchanged_manifest_bytes(self):
        image_id, release, journal = self.pinned_source()
        path = release / 'release.json'
        original = path.read_bytes()
        for key, value in (('version', 'v9.9.9'), ('commit', '9' * 40), ('git_tree', '9' * 40)):
            with self.subTest(field=key):
                metadata = json.loads(original)
                path.write_text(json.dumps({**metadata, key: value}))
                try:
                    with self.assertRaisesRegex(self.op.UpgradeError, 'RELEASE_METADATA_MISMATCH|RELEASE_FILE_HASH_MISMATCH'):
                        self.op.verify_image_source(image_id, release, base_journal=journal)
                finally:
                    path.write_bytes(original)
        self.assertEqual(self.probes, [])

    def test_changed_release_source_cannot_redefine_original_expected_hashes(self):
        image_id, release, journal = self.pinned_source()
        (release / 'backend/supervisor.py').write_bytes(b'changed source, not changed image')
        with self.assertRaisesRegex(self.op.UpgradeError, '^RELEASE_FILE_HASH_MISMATCH$'):
            self.op.verify_image_source(image_id, release, base_journal=journal)
        self.assertEqual(self.probes, [])

    def test_supplement_cannot_override_a_release_file(self):
        image_id, release, journal = self.pinned_source()
        target = release / self.extra_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b'current release owns this path with different content')
        self.manifest(release)
        self.baseline['manifest_sha256'] = self.op.digest(release / 'SHA256SUMS')
        with self.assertRaisesRegex(self.op.UpgradeError, '^LEGACY_BASE_FILE_CONFLICT$'):
            self.op.verify_image_source(image_id, release, base_journal=journal)
        self.assertEqual(self.probes, [])

    def test_target_check_with_same_pinned_image_and_release_is_still_strict(self):
        image_id, release, journal = self.pinned_source()
        with self.assertRaisesRegex(self.op.UpgradeError, '^DOCKER_COMMAND_FAILED$'):
            self.op.verify_image_source(image_id, release)
        self.assertEqual(self.probes[-1]['exit'], 1)
        self.assertNotIn(self.extra_path, self.probes[-1]['expected'])
        self.assertFalse((journal / 'base-source-proof.json').exists())

    @unittest.skipUnless(os.name == 'posix', 'prepare fixture uses a POSIX current symlink')
    def test_prepare_rejects_target_carrying_the_same_extra_after_valid_base_proof(self):
        args, fake, old, original = self.pinned_deployment()
        self.target_extra = True
        with self.assertRaisesRegex(self.op.UpgradeError, '^DOCKER_COMMAND_FAILED$'):
            self.op.prepare(args)
        self.assertEqual([p['exit'] for p in self.probes], [0, 1])
        self.assertNotIn(self.extra_path, self.probes[1]['expected'])
        journal = self.op.journal_path(args)
        self.assertTrue((journal / 'base-source-proof.json').is_file())
        self.assertFalse((journal / 'prepared.json').exists())
        self.assertEqual(json.loads((journal / 'phase.json').read_bytes()), {'phase': 'preparing'})
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual((self.root / 'production.env').read_bytes(), original)
        self.assertFalse(any('stop' in call or 'up' in call for call in fake.calls))

    @unittest.skipUnless(os.name == 'posix', 'real symlink fixture')
    def test_declared_asset_symlink_is_not_accepted_even_for_matching_content(self):
        image_id, release, journal = self.pinned_source()
        destination = self.root / 'retained-bundle.js'
        destination.write_bytes(self.extra_bytes)
        self.extra.unlink()
        self.extra.symlink_to(destination)
        self.assert_image_rejected(image_id, release, journal)

    @unittest.skipUnless(os.name == 'posix', 'prepare fixture uses a POSIX current symlink')
    def test_base_proof_write_failure_prevents_build_and_prepared_publication(self):
        args, fake, _, _ = self.pinned_deployment()
        write = self.op.write_json
        def fail_proof(path, value):
            if path.name == 'base-source-proof.json':
                raise OSError('synthetic proof write failure')
            return write(path, value)
        with patch.object(self.op, 'write_json', side_effect=fail_proof):
            with self.assertRaisesRegex(OSError, '^synthetic proof write failure$'):
                self.op.prepare(args)
        self.assertEqual([p['exit'] for p in self.probes], [0])
        self.assertFalse(any(call[0] == 'build' or 'stop' in call or 'up' in call for call in fake.calls))
        self.assertFalse((self.op.journal_path(args) / 'prepared.json').exists())

    def test_production_baseline_has_only_the_exact_reviewed_identity_and_public_asset(self):
        self.assertEqual(self.op.LEGACY_BASE_IMAGE, {
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
        })


if __name__ == '__main__':
    unittest.main(verbosity=2)
