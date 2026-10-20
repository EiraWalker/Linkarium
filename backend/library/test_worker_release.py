"""Run with Django's test runner; no database or launcher service is required."""
import os
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('DJANGO_SETTINGS_MODULE'), 'Requires Django test runner settings')
class WorkerSpoolReleaseTests(unittest.TestCase):
    def exercise(self, kind, publish_error=None, release_error=None):
        from django.test import override_settings
        from library import worker
        from capture import LauncherClient
        job = SimpleNamespace(id='job', kind=kind, variant='desktop', generation='test', library_epoch=1,
                              payload={}, target_id='bookmark')
        attempt = SimpleNamespace(id='attempt', attempt_no=1)
        events = []

        def publish(*arguments):
            if publish_error:
                raise publish_error
            events.append('published')
            worker.queue.finish(job, attempt)

        def release(*arguments):
            events.append('released')
            if release_error:
                raise release_error

        with override_settings(BOOKMARK_CAPTURE_MODE='isolated', BOOKMARK_CAPTURE_SOCKET='/fixture'), \
                patch('library.importing.advance_imports'), \
                patch('library.submissions.advance_submissions'), \
                patch.object(worker.queue, 'claim_next', return_value=(job, attempt)), \
                patch.object(worker.queue, 'finish') as finish, \
                patch.object(worker, 'heartbeat'), \
                patch.object(worker.performance, 'enabled', return_value=False), \
                patch.object(worker, 'capture_page', return_value=object()), \
                patch.object(worker, 'publish_snapshot', side_effect=publish), \
                patch.object(worker, 'publish_metadata', side_effect=publish), \
                patch.object(LauncherClient, 'release', side_effect=release) as operation:
            self.assertTrue(worker.run_one())
            operation.assert_called_once_with('attempt', 'test', 1)
            return events, list(finish.call_args_list)

    def test_snapshot_and_metadata_release_after_durable_publication(self):
        for kind in ('capture', 'metadata'):
            with self.subTest(kind=kind):
                events, finish = self.exercise(kind)
                self.assertEqual(events, ['published', 'released'])
                self.assertEqual(len(finish), 1)
                self.assertNotIn('error_code', finish[0].kwargs)

    def test_failed_publication_also_releases_staging(self):
        for kind in ('capture', 'metadata'):
            with self.subTest(kind=kind):
                events, finish = self.exercise(kind, publish_error=RuntimeError('publication failed'))
                self.assertEqual(events, ['released'])
                self.assertEqual(finish[0].kwargs['error_code'], 'TASK_FAILED')

    def test_release_outage_preserves_success_and_defers_to_ttl_cleanup(self):
        events, finish = self.exercise('capture', release_error=OSError('launcher unavailable'))
        self.assertEqual(events, ['published', 'released'])
        self.assertEqual(len(finish), 1)
        self.assertNotIn('error_code', finish[0].kwargs)


@unittest.skipUnless(os.environ.get('DJANGO_SETTINGS_MODULE'), 'Requires Django test runner settings')
class SnapshotStagingFailureTests(unittest.TestCase):
    def test_copy_and_fsync_errors_remove_only_the_attempt_owned_temporary(self):
        from library import worker
        for stage in ('copy', 'fsync'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'captured.html'
                source.write_bytes(b'<html>verified capture bytes</html>')
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                destination = root / 'artifacts' / (digest + '.html')
                other = root / 'permanent-existing.html'
                other.write_bytes(b'keep this published archive')
                attempt = SimpleNamespace(id='attempt')
                job = SimpleNamespace(generation='test')
                def partial_copy(incoming, outgoing):
                    outgoing.write(b'partial')
                    raise OSError('Controlled disk write failure')
                original_copy = worker.shutil.copyfileobj
                original_fsync = worker.os.fsync
                with patch.object(worker.SnapshotVersion.objects, 'filter') as previous, \
                        patch.object(worker, 'artifact_path', return_value=destination), \
                        patch.object(worker.shutil, 'copyfileobj', side_effect=partial_copy if stage == 'copy' else original_copy), \
                        patch.object(worker.os, 'fsync', side_effect=OSError('Controlled fsync failure') if stage == 'fsync' else original_fsync):
                    previous.return_value.first.return_value = None
                    with self.assertRaises(OSError):
                        worker.publish_snapshot(job, attempt, {'artifact_path': str(source)})
                self.assertEqual(list(destination.parent.glob('*.tmp')), [])
                self.assertFalse(destination.exists())
                self.assertEqual(source.read_bytes(), b'<html>verified capture bytes</html>')
                self.assertEqual(other.read_bytes(), b'keep this published archive')

    def test_failure_to_create_temporary_does_not_remove_an_existing_file(self):
        from library import worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'capture.html'
            source.write_bytes(b'capture')
            destination = root / 'archive.html'
            existing = destination.with_suffix('.attempt.tmp')
            existing.write_bytes(b'existing staging bytes')
            with patch.object(worker.SnapshotVersion.objects, 'filter') as previous, \
                    patch.object(worker, 'artifact_path', return_value=destination):
                previous.return_value.first.return_value = None
                with self.assertRaises(FileExistsError):
                    worker.publish_snapshot(SimpleNamespace(generation='test'), SimpleNamespace(id='attempt'), {'artifact_path': str(source)})
            self.assertEqual(existing.read_bytes(), b'existing staging bytes')
