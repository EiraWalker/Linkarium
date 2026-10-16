"""Publication budget integration without a browser or production library."""
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from runtime.models import RuntimeState
from . import catalog, queue
from .errors import ApiError
from .models import Artifact, CaptureState, LibrarySettings, SnapshotVersion
from .worker import publish_snapshot


class CaptureArtifactBudgetTests(TestCase):
    databases = {'default', 'library'}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)

    def claim(self):
        bookmark, _ = catalog.create_bookmark({'url': 'https://large.example/', 'snapshot_enabled': True})
        queue.request_refresh([bookmark.id])
        job, attempt = queue.claim_next()
        return bookmark, job, attempt

    def write_html(self, path, size):
        head = b'<!doctype html><html><body>Large archived page<!--'
        tail = b'--></body></html>'
        chunk = b' ' * (1024 * 1024)
        digest = hashlib.sha256()
        with path.open('wb') as destination:
            destination.write(head); digest.update(head)
            remaining = size - len(head) - len(tail)
            while remaining:
                part = chunk[:min(remaining, len(chunk))]
                destination.write(part); digest.update(part)
                remaining -= len(part)
            destination.write(tail); digest.update(tail)
        return digest.hexdigest()

    def test_archive_above_64_mib_is_published_with_current_version(self):
        bookmark, job, attempt = self.claim()
        size = 65 * 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'capture.html'
            digest = self.write_html(source, size)
            with patch('library.artifacts.generation_root', return_value=root / 'generation'):
                version = publish_snapshot(job, attempt, {'artifact_path': source, 'viewport_width': 1366,
                    'viewport_height': 768, 'title': 'Large archive', 'final_url': bookmark.canonical_url})
            target = root / 'generation' / f'artifacts/{digest[:2]}/{digest}.html'
            self.assertEqual(target.stat().st_size, size)
            with target.open('rb') as stream:
                self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), digest)
            self.assertEqual(version.artifact_id, digest)
            self.assertEqual(Artifact.objects.get(pk=digest).byte_size, size)
            self.assertEqual(CaptureState.objects.get(bookmark=bookmark, variant=job.variant).current_version_id, version.id)
            job.refresh_from_db()
            self.assertEqual(job.state, 'succeeded')

    def test_archive_above_96_mib_is_rejected_before_publication(self):
        _, job, attempt = self.claim()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'oversized.html'
            with source.open('wb') as stream:
                stream.truncate(96 * 1024 * 1024 + 1)
            with self.assertRaises(ApiError) as raised:
                publish_snapshot(job, attempt, {'artifact_path': source})
            self.assertEqual(raised.exception.code, 'INVALID_ARTIFACT')
        self.assertFalse(SnapshotVersion.objects.exists())
        self.assertFalse(Artifact.objects.exists())
        job.refresh_from_db()
        self.assertEqual(job.state, 'running')
