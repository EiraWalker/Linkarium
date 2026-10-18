"""Exercise the real legacy import data migration on isolated test databases."""
from contextlib import closing
from datetime import timedelta
import hashlib
import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class LegacyImportBudgetMigrationTests(TransactionTestCase):
    databases = {"default", "library"}
    before = ("library", "0003_classificationtaxonomy_folder_created_by_ai")
    after = ("library", "0004_import_execution_budgets")

    def setUp(self):
        super().setUp()
        self.connection = connections["library"]
        executor = MigrationExecutor(self.connection)
        executor.migrate([self.before])
        self.old = executor.loader.project_state([self.before]).apps
        self.temp = tempfile.TemporaryDirectory(prefix="import-migration-")
        self.root = Path(self.temp.name)
        self.old.get_model("library", "LibrarySettings").objects.using("library").create(pk=1)
        from runtime.models import RuntimeState
        RuntimeState.objects.using("default").get_or_create(pk=1)

    def tearDown(self):
        try:
            MigrationExecutor(self.connection).migrate([self.after])
        finally:
            self.temp.cleanup()
            super().tearDown()

    def model(self, name, apps=None):
        return (apps or self.old).get_model("library", name)

    def create(self, name, **fields):
        return self.model(name).objects.using("library").create(**fields)

    def migrate(self):
        executor = MigrationExecutor(self.connection)
        executor.migrate([self.after])
        self.new = executor.loader.project_state([self.after]).apps

    def snapshot(self, name, apps=None):
        return list(self.model(name, apps).objects.using("library").order_by("pk").values())

    def job(self, *, kind="capture", target=None, state="queued", group=None, payload=None):
        now = timezone.now()
        value = self.create("Job", kind=kind, target_id=target, state=state,
            available_at=now, generation="initial", library_epoch=1,
            variant="mobile" if kind == "capture" else "", active_key="fixture:" + uuid.uuid4().hex,
            payload=payload or {})
        attempt = None
        if state == "running":
            attempt = self.create("JobAttempt", job_id=value.id, attempt_no=1, heartbeat_at=now,
                lease_expires_at=now + timedelta(minutes=3), generation="initial", library_epoch=1)
            self.model("Job").objects.using("library").filter(pk=value.pk).update(active_attempt_id=attempt.id)
        if group:
            self.create("JobGroupMember", group_id=group.id, job_id=value.id, bookmark_id=target)
        return value, attempt

    def legacy_batch(self, *, state="running", stage="captures", active=True):
        identity = uuid.uuid4().hex
        capture = self.create("JobGroup", kind="refresh", source="import", requested_count=2)
        classification = self.create("JobGroup", kind="classification", source="import", requested_count=2)
        batch = self.create("ImportBatch", state=state, stage=stage, total=3, valid=2, invalid=1,
            capture_group_id=capture.id, classification_group_id=classification.id,
            ai_status="running" if active else "succeeded", review_notified=not active)
        bookmark = self.create("Bookmark", original_url=f"https://existing-{identity}.example/",
            canonical_url=f"https://existing-{identity}.example/", domain=f"existing-{identity}.example",
            notes="retain saved notes", title_override="retain owner title", is_starred=True)
        self.create("ImportEntry", batch_id=batch.id, url=bookmark.canonical_url,
            title="original imported title", original_folder_path=["Retained", "Folder"], status="imported", bookmark_id=bookmark.id)
        pending = self.create("ImportEntry", batch_id=batch.id, url=f"https://pending-{identity}.example/",
            title="pending imported title", original_folder_path=["Pending"], status="pending")
        self.create("ImportEntry", batch_id=batch.id, url="javascript:void(0)", status="invalid", error_code="INVALID_URL")
        self.create("BookmarkSource", bookmark_id=bookmark.id, import_batch_id=batch.id,
            source_folder_path=["Retained", "Folder"], source_title="original imported title")
        saved_job, _ = self.job(target=bookmark.id, state="succeeded", group=capture)
        now = timezone.now()
        saved_attempt = self.create("JobAttempt", job_id=saved_job.id, attempt_no=1, state="succeeded",
            heartbeat_at=now, lease_expires_at=now, finished_at=now, generation="initial", library_epoch=1)
        payload = b"<!doctype html><title>retained snapshot</title>" + identity.encode()
        digest = hashlib.sha256(payload).hexdigest()
        relative = f"artifacts/{digest[:2]}/{digest}.html"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        artifact = self.create("Artifact", sha256=digest, relative_path=relative, byte_size=len(payload))
        snapshot = self.create("SnapshotVersion", bookmark_id=bookmark.id, variant="desktop", artifact_id=artifact.pk,
            capture_attempt_id=saved_attempt.id, captured_at=now, source_url=bookmark.canonical_url,
            final_url=bookmark.canonical_url, viewport_width=1440, viewport_height=900, capture_epoch=1)
        self.create("CaptureState", bookmark_id=bookmark.id, variant="desktop", current_version_id=snapshot.id)
        old_classification = self.create("ClassificationBatch", job_group_id=classification.id, tree_revision=1)
        self.create("ClassificationProposal", batch_id=old_classification.id, bookmark_id=bookmark.id,
            expected_classification_revision=1, expected_tree_revision=1,
            target_path={"new_segments": ["Saved review"]}, reason="retain previous proposal")
        owned, attempts = [], []
        if active:
            specs = (
                {"kind": "import", "target": batch.id, "state": "queued"},
                {"target": bookmark.id, "state": "running", "group": capture},
                {"kind": "metadata", "target": bookmark.id, "state": "retry_wait", "group": capture},
                {"kind": "classification", "state": "running", "group": classification},
                # Planning/classification jobs can be owned only by JSON payload.
                {"kind": "classification", "state": "queued", "payload": {"import_id": str(batch.id), "phase": "planning"}},
                {"kind": "classification", "state": "running", "payload": {"import_id": batch.id.hex}},
                {"kind": "classification", "state": "retry_wait", "payload": {"group_id": str(classification.id)}},
            )
            for fields in specs:
                job, attempt = self.job(**fields)
                owned.append(job.id)
                if attempt:
                    attempts.append(attempt.id)
        return {"batch": batch, "bookmark": bookmark, "pending": pending, "snapshot": snapshot,
                "capture": capture, "classification": classification, "jobs": owned, "attempts": attempts,
                "saved_job": saved_job, "saved_attempt": saved_attempt, "path": path, "sha256": digest}

    def assert_preview_and_fenced(self, fixture):
        batch = self.model("ImportBatch", self.new).objects.using("library").get(pk=fixture["batch"].id)
        self.assertEqual((batch.state, batch.stage), ("previewed", "records"))
        self.assertEqual((batch.capture_group_id, batch.classification_group_id), (None, None))
        self.assertEqual((batch.confirmation_digest, batch.execution_authorization, batch.capture_budget, batch.ai_budget), ("", "", 0, 0))
        self.assertEqual(batch.ai_error_code, "IMPORT_CONFIRMATION_REQUIRED")
        for job in self.model("Job", self.new).objects.using("library").filter(pk__in=fixture["jobs"]):
            self.assertEqual((job.state, job.active_key, job.active_attempt_id), ("cancelled", None, None))
            self.assertEqual(job.error_code, "IMPORT_CONFIRMATION_REQUIRED")
        for attempt in self.model("JobAttempt", self.new).objects.using("library").filter(pk__in=fixture["attempts"]):
            self.assertEqual(attempt.state, "cancelled")
            self.assertIsNotNone(attempt.finished_at)
            self.assertLessEqual(attempt.lease_expires_at, timezone.now())
        self.assertEqual(hashlib.sha256(fixture["path"].read_bytes()).hexdigest(), fixture["sha256"])

    def test_queued_running_and_every_capture_classification_phase_are_reconfirmed(self):
        fixtures = [self.legacy_batch(state=state, stage=stage)
            for state in ("queued", "running")
            for stage in ("records", "captures", "classification", "waiting_tasks", "classifying")]
        unchanged = {name: self.snapshot(name) for name in (
            "Bookmark", "BookmarkSource", "ImportEntry", "Artifact", "SnapshotVersion", "CaptureState", "ClassificationProposal")}
        unrelated, unrelated_attempt = self.job(kind="metadata", state="running")
        self.migrate()
        for fixture in fixtures:
            with self.subTest(state=fixture["batch"].state, stage=fixture["batch"].stage):
                self.assert_preview_and_fenced(fixture)
        for name, rows in unchanged.items():
            self.assertEqual(self.snapshot(name, self.new), rows)
        current = self.model("Job", self.new).objects.using("library").get(pk=unrelated.id)
        self.assertEqual((current.state, current.active_attempt_id), ("running", unrelated_attempt.id))
        self.assertEqual(self.model("JobAttempt", self.new).objects.using("library").get(pk=unrelated_attempt.id).state, "running")

    def test_failed_cancelled_partial_imports_and_legacy_state_aliases_fence_children(self):
        fixtures = [self.legacy_batch(state=state, stage="waiting_tasks" if state != "classifying" else "classification")
            for state in ("failed", "cancelled", "waiting_tasks", "classifying")]
        self.migrate()
        for fixture in fixtures:
            with self.subTest(state=fixture["batch"].state):
                self.assert_preview_and_fenced(fixture)

    def test_completed_review_and_historical_snapshot_attempts_are_preserved(self):
        fixture = self.legacy_batch(state="succeeded", stage="review", active=False)
        before = {name: self.snapshot(name) for name in ("Job", "JobAttempt", "Bookmark", "Artifact", "SnapshotVersion", "ClassificationProposal")}
        self.migrate()
        batch = self.model("ImportBatch", self.new).objects.using("library").get(pk=fixture["batch"].id)
        self.assertEqual((batch.state, batch.stage, batch.capture_group_id, batch.classification_group_id),
            ("succeeded", "review", fixture["capture"].id, fixture["classification"].id))
        self.assertTrue(batch.review_notified)
        for name, rows in before.items():
            self.assertEqual(self.snapshot(name, self.new), rows)

    def test_partial_batch_requires_fresh_confirmation_then_resumes_missing_work(self):
        fixture = self.legacy_batch(state="running", stage="captures")
        old_snapshot = self.snapshot("SnapshotVersion")
        old_proposals = self.snapshot("ClassificationProposal")
        self.migrate()
        from library.errors import ApiError
        from library.importing import execute_import, import_plan, serialize_import, start_import
        from library.models import ImportBatch, ImportEntry, Job, SnapshotVersion
        batch = ImportBatch.objects.get(pk=fixture["batch"].id)
        preview = serialize_import(batch)
        self.assertEqual((preview["state"], preview["imported"], preview["pending"]), ("previewed", 1, 1))
        with self.assertRaises(ApiError) as caught:
            execute_import(batch.id)
        self.assertEqual(caught.exception.code, "IMPORT_CONFIRMATION_REQUIRED")
        plan = import_plan(batch)
        self.assertEqual((plan["entries"], plan["captures"]), (2, 4))
        start_import(batch, plan["confirmation_token"])
        result = execute_import(batch.id)
        self.assertEqual((result.state, result.stage), ("running", "captures"))
        self.assertNotEqual(result.capture_group_id, fixture["capture"].id)
        self.assertEqual(ImportEntry.objects.get(pk=fixture["pending"].id).status, "imported")
        self.assertEqual(self.snapshot("SnapshotVersion", self.new), old_snapshot)
        self.assertEqual(self.snapshot("ClassificationProposal", self.new), old_proposals)
        self.assertEqual(SnapshotVersion.objects.get(pk=fixture["snapshot"].id).artifact_id, fixture["sha256"])
        jobs = Job.objects.filter(kind="capture", memberships__group_id=result.capture_group_id, state="queued")
        self.assertEqual(jobs.count(), 3)  # Existing desktop stays, mobile + new site's two variants run.
        self.assertFalse(jobs.filter(target_id=fixture["bookmark"].id, variant="desktop").exists())
        self.assertEqual(hashlib.sha256(fixture["path"].read_bytes()).hexdigest(), fixture["sha256"])

    def test_data_migration_is_idempotent_after_fencing(self):
        self.legacy_batch(state="running", stage="classifying")
        self.migrate()
        before = {name: self.snapshot(name, self.new) for name in ("ImportBatch", "Job", "JobAttempt", "SnapshotVersion")}
        migration = importlib.import_module("library.migrations.0004_import_execution_budgets")
        migration.require_legacy_confirmation(self.new, SimpleNamespace(connection=self.connection))
        for name, rows in before.items():
            self.assertEqual(self.snapshot(name, self.new), rows)
