"""The user-facing restore path opts into bounded compact extraction."""
import gzip
import shutil
from unittest.mock import patch

from django.test import TransactionTestCase

from runtime.maintenance import create_restore_validation, process_operation, validate_candidate
from runtime.models import RuntimeState
from tests.portability import test_artifact_storage_portability as fixtures


class CompactRestoreOperationTests(TransactionTestCase):
    databases = {"default"}

    def test_restore_preview_uses_compact_candidate_without_changing_active_generation(self):
        fixture = fixtures.ArtifactStoragePortabilityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        data = fixture.root / "runtime"
        active = data / "generations" / "initial"
        shutil.copytree(fixture.generation, active)
        self.enterContext(patch("config.paths.DATA_ROOT", data))
        RuntimeState.objects.using("default").get_or_create(pk=1)
        operation = create_restore_validation(fixture.legacy_package().read_bytes())
        self.assertTrue(process_operation(operation.id, stop_services=lambda: self.fail("preview must not stop services"),
                                          start_services=lambda: None, ready_services=lambda: True))
        operation.refresh_from_db()
        self.assertEqual(operation.state, "awaiting_confirmation")
        candidate = data / "generations" / operation.target_generation
        canonical = candidate / fixture.name
        self.assertFalse(canonical.exists())
        self.assertEqual(gzip.decompress(canonical.with_suffix(".html.gz").read_bytes()), fixture.payload)
        self.assertEqual(validate_candidate(candidate, expected_schema=active / "library.sqlite3")["counts"]["snapshots"], 2)
        self.assertEqual(RuntimeState.objects.using("default").get(pk=1).active_generation, "initial")
        self.assertEqual(gzip.decompress((active / fixture.name).with_suffix(".html.gz").read_bytes()), fixture.payload)
