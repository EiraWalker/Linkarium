"""Current capture errors and recovery batches are distinct from task history."""
from io import StringIO
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from library import queue
from library.errors import ApiError
from library.models import Bookmark, CaptureState, Job, JobAttempt, JobGroup, JobGroupMember, LibrarySettings
from library.worker import publish_snapshot
from runtime.models import RuntimeState


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CaptureFailureDisplayTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        self.client.force_login(get_user_model().objects.create_user("owner", password="fixture-only"))
        self.bookmark = Bookmark.objects.create(original_url="https://example.org/",
            canonical_url="https://example.org/", domain="example.org", snapshot_enabled=True)
        queue.enqueue("capture", self.bookmark.id, "desktop", priority=60,
            input_revision=self.bookmark.capture_epoch,
            active_key=f"capture:{self.bookmark.id}:desktop:{self.bookmark.capture_epoch}")
        self.job, self.attempt = queue.claim_next()
        queue.finish(self.job, self.attempt, error_code="CAPTURE_TIMEOUT", retryable=True)

    def assert_current_error(self, state, error):
        detail = self.client.get(f"/api/v1/bookmarks/{self.bookmark.id}").json()["data"]
        listing = self.client.get("/api/v1/bookmarks").json()["data"][0]
        self.assertEqual(detail["capture_status"], {"state": state, "last_error_code": error})
        self.assertEqual(listing["capture_status"], {"state": state, "last_error_code": error})
        self.assertEqual(detail["capture_states"][0]["state"], state)
        self.assertEqual(detail["capture_states"][0]["last_error_code"], error)

    def test_retry_masks_old_error_in_list_and_detail_but_preserves_diagnostics(self):
        response = self.client.post(f"/api/v1/jobs/{self.job.id}/retry", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 202)
        self.assert_current_error("queued", "")
        queue.claim_next()
        self.assert_current_error("running", "")
        self.assertEqual(CaptureState.objects.get(bookmark=self.bookmark).last_error_code, "CAPTURE_TIMEOUT")
        self.assertEqual(JobAttempt.objects.get(pk=self.attempt.id).error_code, "CAPTURE_TIMEOUT")

    def test_retry_wait_and_terminal_failure_still_expose_real_error(self):
        self.assert_current_error("retry_wait", "CAPTURE_TIMEOUT")
        queue.retry_job(self.job.id)
        job, attempt = queue.claim_next()
        queue.finish(job, attempt, error_code="CAPTURE_CHALLENGE", retryable=False)
        self.assert_current_error("failed", "CAPTURE_CHALLENGE")

    def test_success_clears_current_failure_but_preserves_attempt_history(self):
        queue.retry_job(self.job.id)
        job, attempt = queue.claim_next()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "source.html"
            artifact.write_text("<!doctype html><html><body>Preview fixture</body></html>", encoding="utf-8")
            result = {"artifact_path": artifact, "viewport_width": 1366, "viewport_height": 768,
                "title": "Fixture", "final_url": self.bookmark.canonical_url}
            with patch("library.worker.generation_root", return_value=root / "generation"), \
                    patch("library.artifacts.generation_root", return_value=root / "generation"):
                publish_snapshot(job, attempt, result)
        self.assert_current_error("ready", "")
        self.assertEqual(JobAttempt.objects.get(pk=self.attempt.id).error_code, "CAPTURE_TIMEOUT")
        self.assertEqual(CaptureState.objects.get(bookmark=self.bookmark).failure_count, 0)


class CaptureRecoveryQueueTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        self.first = self.bookmark("first")
        self.second = self.bookmark("second")
        self.disabled = self.bookmark("disabled", enabled=False)
        self.failed(self.first, "desktop")
        CaptureState.objects.create(bookmark=self.first, variant="mobile")
        self.failed(self.second, "desktop")
        self.failed(self.second, "mobile")
        self.failed(self.disabled, "desktop")
        self.old_group = JobGroup.objects.create(kind="refresh", source="manual", requested_count=1)
        old = queue.enqueue("capture", self.first.id, "desktop", group=self.old_group)
        job, attempt = queue.claim_next()
        self.assertEqual(old.id, job.id)
        queue.finish(job, attempt, error_code="CAPTURE_TIMEOUT", retryable=False)

    def bookmark(self, name, enabled=True):
        url = f"https://{name}.example.org/"
        return Bookmark.objects.create(original_url=url, canonical_url=url, domain=f"{name}.example.org",
            snapshot_enabled=enabled)

    def failed(self, bookmark, variant):
        return CaptureState.objects.create(bookmark=bookmark, variant=variant,
            last_error_code="CAPTURE_TIMEOUT", failure_count=1)

    def test_preview_is_read_only_and_counts_devices_separately_from_websites(self):
        before = list(CaptureState.objects.values())
        report = queue.retry_failed_captures()
        self.assertEqual((report["failed_devices"], report["failed_websites"]), (3, 2))
        self.assertEqual((report["to_queue_devices"], report["to_queue_websites"]), (3, 2))
        self.assertEqual(report["queued_devices"], 0)
        self.assertIsNone(report["group_id"])
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(JobGroup.objects.count(), 1)
        self.assertEqual(list(CaptureState.objects.values()), before)

    def test_execute_targets_only_failed_enabled_devices_and_preserves_history(self):
        old_members = list(self.old_group.members.values())
        old_jobs = list(Job.objects.values())
        old_attempts = list(JobAttempt.objects.values())
        failures = list(CaptureState.objects.values())
        report = queue.retry_failed_captures(execute=True)
        group = JobGroup.objects.get(pk=report["group_id"])
        self.assertEqual(report["queued_devices"], 3)
        self.assertEqual(group.requested_count, 2)
        self.assertEqual(group.source, "failed-capture-recovery")
        actual = set(group.members.values_list("job__target_id", "job__variant"))
        self.assertEqual(actual, {(self.first.id, "desktop"), (self.second.id, "desktop"), (self.second.id, "mobile")})
        self.assertEqual(list(self.old_group.members.values()), old_members)
        self.assertEqual(list(Job.objects.filter(id__in=[job["id"] for job in old_jobs]).values()), old_jobs)
        self.assertEqual(list(JobAttempt.objects.values()), old_attempts)
        self.assertEqual(list(CaptureState.objects.values()), failures)
        for job in Job.objects.filter(memberships__group=group):
            self.assertEqual((job.state, job.generation, job.library_epoch), ("queued", "initial", 1))
            self.assertEqual(job.active_key, f"capture:{job.target_id}:{job.variant}:{job.input_revision}")

    def test_repeated_execution_deduplicates_active_tasks_without_empty_groups(self):
        first = queue.retry_failed_captures(execute=True)
        group_count = JobGroup.objects.count()
        second = queue.retry_failed_captures(execute=True)
        self.assertEqual(second["already_active_devices"], first["queued_devices"])
        self.assertEqual(second["queued_devices"], 0)
        self.assertIsNone(second["group_id"])
        self.assertEqual(JobGroup.objects.count(), group_count)
        self.assertEqual(Job.objects.count(), 4)

    def test_empty_plan_never_creates_group(self):
        CaptureState.objects.all().update(last_error_code="")
        report = queue.retry_failed_captures(execute=True)
        self.assertEqual(report["to_queue_devices"], 0)
        self.assertIsNone(report["group_id"])
        self.assertEqual(JobGroup.objects.count(), 1)

    def test_maintenance_and_changed_runtime_identity_reject_without_side_effects(self):
        for options, maintenance, code in [
            ({}, True, "MAINTENANCE"),
            ({"expected_generation": "older"}, False, "ATTEMPT_EXPIRED"),
            ({"expected_library_epoch": 0}, False, "ATTEMPT_EXPIRED"),
        ]:
            with self.subTest(options=options, maintenance=maintenance):
                RuntimeState.objects.filter(pk=1).update(maintenance_mode=maintenance)
                with self.assertRaises(ApiError) as error:
                    queue.retry_failed_captures(execute=True, **options)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(JobGroup.objects.count(), 1)
                self.assertEqual(Job.objects.count(), 1)

    def test_stale_active_key_is_rejected_instead_of_reused(self):
        job = queue.enqueue("capture", self.first.id, "desktop",
            active_key=f"capture:{self.first.id}:desktop:{self.first.capture_epoch}")
        job.library_epoch = 0
        job.save(update_fields=["library_epoch"])
        with self.assertRaises(ApiError) as error:
            queue.retry_failed_captures(execute=True)
        self.assertEqual(error.exception.code, "ATTEMPT_EXPIRED")
        self.assertEqual(JobGroup.objects.count(), 1)

    def test_runtime_change_between_initial_read_and_transaction_is_rechecked(self):
        original = queue.transaction.atomic

        @contextmanager
        def switched_before_lock(using=None, **kwargs):
            if using == "default":
                RuntimeState.objects.filter(pk=1).update(library_epoch=2)
            with original(using=using, **kwargs):
                yield

        with patch("library.queue.transaction.atomic", side_effect=switched_before_lock):
            with self.assertRaises(ApiError) as error:
                queue.retry_failed_captures(execute=True)
        self.assertEqual(error.exception.code, "ATTEMPT_EXPIRED")
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(JobGroup.objects.count(), 1)

    def test_device_recovered_before_library_lock_is_not_queued(self):
        original = queue.transaction.atomic
        changed = False

        @contextmanager
        def recovered_before_lock(using=None, **kwargs):
            nonlocal changed
            if using == "library" and not changed:
                changed = True
                CaptureState.objects.filter(bookmark=self.first, variant="desktop").update(last_error_code="")
            with original(using=using, **kwargs):
                yield

        with patch("library.queue.transaction.atomic", side_effect=recovered_before_lock):
            report = queue.retry_failed_captures(execute=True)
        self.assertEqual((report["queued_devices"], report["to_queue_websites"]), (2, 1))
        self.assertFalse(Job.objects.filter(memberships__group_id=report["group_id"], target_id=self.first.id).exists())

    def test_enqueue_failure_rolls_back_entire_batch(self):
        original = queue.enqueue
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture interrupted")
            return original(*args, **kwargs)

        with patch("library.queue.enqueue", side_effect=interrupted), self.assertRaises(RuntimeError):
            queue.retry_failed_captures(execute=True)
        self.assertEqual(JobGroup.objects.count(), 1)
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(JobGroupMember.objects.count(), 1)

    def test_command_defaults_to_preview_and_execute_is_explicit(self):
        output = StringIO()
        call_command("retry_failed_captures", stdout=output)
        preview = json.loads(output.getvalue())
        self.assertFalse(preview["execute"])
        self.assertEqual(Job.objects.count(), 1)
        output = StringIO()
        call_command("retry_failed_captures", "--execute", "--expected-generation", "initial",
            "--expected-library-epoch", "1", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["queued_devices"], 3)
