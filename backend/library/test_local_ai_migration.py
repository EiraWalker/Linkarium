"""Upgrade the deployed schema without reassigning historical model work."""
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class DeviceExecutionMigrationTests(TransactionTestCase):
    databases = {"default", "library"}

    def test_deployed_schema_upgrade_preserves_data_and_fences_unowned_model_jobs(self):
        connection = connections["library"]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes("library")
        before = [("library", "0005_extension_submission")]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            apps.get_model("library", "LibrarySettings").objects.using("library").create(pk=1)
            bookmark = apps.get_model("library", "Bookmark").objects.using("library").create(
                original_url="https://example.org/kept", canonical_url="https://example.org/kept", domain="example.org", notes="kept")
            job = apps.get_model("library", "Job").objects.using("library").create(kind="classification", state="running",
                available_at=timezone.now(), generation="initial", library_epoch=1, payload={"bookmark_ids": [str(bookmark.id)]})
            batch = apps.get_model("library", "ClassificationBatch").objects.using("library").create(tree_revision=1)
            apps.get_model("library", "ClassificationProposal").objects.using("library").create(batch=batch, bookmark=bookmark,
                expected_classification_revision=1, expected_tree_revision=1, reason="historical suggestion")
            MigrationExecutor(connection).migrate(latest)
            from .models import Bookmark, ClassificationProposal, Job
            self.assertEqual(Bookmark.objects.get(pk=bookmark.id).notes, "kept")
            self.assertEqual(ClassificationProposal.objects.get().reason, "historical suggestion")
            upgraded = Job.objects.get(pk=job.id)
            self.assertEqual((upgraded.state, upgraded.error_code), ("failed", "LOCAL_EXECUTOR_REQUIRED"))
            self.assertIsNone(upgraded.execution_device_id)
        finally:
            MigrationExecutor(connection).migrate(latest)
