"""Local execution protocol through the authenticated application HTTP boundary."""
import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from runtime.models import RuntimeState
from runtime.testing import force_credential_login
from library.models import LibrarySettings


class LocalExecutionTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        force_credential_login(self.client, get_user_model().objects.create_user("owner"))
        self.device = str(uuid.uuid4())
        self.proof = "fixture-device-proof-" + "a" * 48

    def post(self, path, data, *, device=True):
        headers = {"HTTP_X_LINKARIUM_DEVICE": self.device, "HTTP_X_LINKARIUM_DEVICE_PROOF": self.proof} if device else {}
        return self.client.post("/api/v1/" + path, json.dumps(data), content_type="application/json", **headers)

    def register(self):
        response = self.post("local-ai/devices", {"id": self.device, "proof": self.proof, "name": "Firefox fixture"}, device=False)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    def start(self):
        self.register()
        bookmark = self.post("bookmarks", {"url": "https://example.org/recovery", "snapshot_enabled": False}).json()["data"]
        response = self.post("classification/batches", {"bookmark_ids": [bookmark["id"]]})
        self.assertEqual(response.status_code, 202, response.content)
        return bookmark, self.post("local-ai/claim", {}).json()["data"]

    def result(self, claim, response):
        return self.post("local-ai/result", {**claim, "response": response})

    def test_expired_lease_requires_same_device_reauthorization_and_reuses_checkpoint(self):
        from django.utils import timezone
        from datetime import timedelta
        from library.models import JobAttempt, ClassificationTaxonomy
        _, claim = self.start()
        raw = '{"categories":[{"path":["技术"],"description":"技术资料"}]}'
        JobAttempt.objects.filter(pk=claim["attempt_id"]).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.result(claim, raw).status_code, 409)
        resumed = self.post("local-ai/claim", {"resume_request_id": claim["request_id"]}).json()["data"]
        self.assertNotEqual(resumed["lease_token"], claim["lease_token"])
        self.assertEqual(resumed["prompt_digest"], claim["prompt_digest"])
        self.assertEqual(self.result(claim, raw).status_code, 409)
        self.assertEqual(self.result(resumed, raw).status_code, 200)
        self.assertEqual(ClassificationTaxonomy.objects.count(), 1)
        self.assertTrue(self.post("local-ai/claim", {"resume_request_id": claim["request_id"]}).json()["data"]["completed"])

    def test_cancelled_and_changed_library_cannot_accept_cached_results(self):
        from library.models import Job, ClassificationTaxonomy
        _, claim = self.start()
        raw = '{"categories":[{"path":["技术"],"description":"技术资料"}]}'
        Job.objects.filter(pk=claim["job_id"]).update(state="cancelled")
        self.assertEqual(self.result(claim, raw).status_code, 409)
        self.assertEqual(ClassificationTaxonomy.objects.count(), 0)
        RuntimeState.objects.filter(pk=1).update(library_epoch=2)
        self.assertEqual(self.result(claim, raw).status_code, 409)

    def test_classification_requires_a_device_and_server_credentials_cannot_enable_execution(self):
        bookmark = self.post("bookmarks", {"url": "https://example.org/saved"}, device=False)
        self.assertEqual(bookmark.status_code, 201)
        response = self.post("classification/batches", {"bookmark_ids": [bookmark.json()["data"]["id"]]}, device=False)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "LOCAL_DEVICE_REQUIRED")
        self.assertEqual(self.post("ai-profiles", {"api_key": "must-not-save"}, device=False).status_code, 409)
        from runtime.models import ProviderSecret
        self.assertEqual(ProviderSecret.objects.count(), 0)

    def test_database_failure_between_auto_filing_and_receipt_rolls_back_every_effect(self):
        from django.db import connections
        from library.models import Job, Folder, ClassificationProposal, ExtensionSubmission
        from library.submissions import advance_submissions
        self.register()
        response = self.client.post("/api/v1/extension/submissions", json.dumps({"url": "https://example.org/atomic"}),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY="atomic-save",
            HTTP_X_LINKARIUM_DEVICE=self.device, HTTP_X_LINKARIUM_DEVICE_PROOF=self.proof)
        self.assertEqual(response.status_code, 202, response.content)
        Job.objects.filter(kind__in=["metadata", "capture"]).update(state="succeeded", active_key=None)
        advance_submissions()
        claim = self.post("local-ai/claim", {}).json()["data"]
        self.assertEqual(self.result(claim, '{"categories":[{"path":["技术"],"description":"技术资料"}]}').status_code, 200)
        claim = self.post("local-ai/claim", {}).json()["data"]
        bookmark_id = response.json()["data"]["bookmark_id"]
        raw = json.dumps({"items": [{"bookmark_id": bookmark_id, "reason": "技术资料",
            "target": {"parent_folder_id": None, "new_segments": ["技术"]}}]})
        with connections["library"].cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER reject_finish BEFORE UPDATE OF state ON library_job WHEN NEW.state = 'succeeded' BEGIN SELECT RAISE(ABORT, 'fixture commit failure'); END")
        try:
            self.assertEqual(self.result(claim, raw).status_code, 409)
            self.assertEqual(Folder.objects.count(), 0)
            self.assertEqual(ClassificationProposal.objects.count(), 0)
            self.assertEqual(ExtensionSubmission.objects.get().state, "classifying")
        finally:
            with connections["library"].cursor() as cursor:
                cursor.execute("DROP TRIGGER reject_finish")
        self.assertEqual(self.result(claim, raw).status_code, 200)
        self.assertEqual(self.result(claim, raw).status_code, 200)
        self.assertEqual(Folder.objects.count(), 1)
        self.assertEqual(ClassificationProposal.objects.filter(status="accepted").count(), 1)

    def test_untrusted_json_and_changed_tree_never_create_a_proposal(self):
        from library.models import ClassificationProposal, ClassificationTaxonomy
        bookmark, claim = self.start()
        self.assertEqual(self.result(claim, '{"categories":[],"categories":[]}').status_code, 400)
        self.assertFalse(ClassificationTaxonomy.objects.exists())
        self.assertEqual(self.result(claim, '{"categories":[{"path":["技术"],"description":"技术资料"}]}').status_code, 200)
        claim = self.post("local-ai/claim", {}).json()["data"]
        raw = json.dumps({"items": [{"bookmark_id": bookmark["id"], "reason": "资料",
            "target": {"parent_folder_id": None, "new_segments": ["未经规划的分类"]}}]})
        self.assertEqual(self.result(claim, raw).status_code, 400)
        self.assertEqual(self.post("folders", {"name": "人工目录"}).status_code, 201)
        self.assertEqual(self.result(claim, raw).json()["error"]["code"], "PROPOSAL_STALE")
        self.assertFalse(ClassificationProposal.objects.exists())

    def test_material_collection_finishes_before_prompt_and_cannot_change_running_input(self):
        from unittest.mock import patch
        from library.worker import run_one
        from library import queue
        from library.models import Job
        self.register()
        with self.captureOnCommitCallbacks(using="library", execute=True):
            bookmark = self.post("bookmarks", {"url": "https://example.org/materials", "snapshot_enabled": False}).json()["data"]
        self.assertTrue(self.post("local-ai/claim", {}).json()["data"]["waiting"])
        self.assertEqual(Job.objects.get(kind="classification").execution_state, "waiting_materials")
        with patch("capture.LauncherClient.capture", return_value={"title": "Enriched title", "description": "Collected before inference"}), patch("capture.LauncherClient.release"):
            self.assertTrue(run_one())
        claim = self.post("local-ai/claim", {}).json()["data"]
        late = queue.enqueue("metadata", bookmark["id"], priority=90)
        self.assertIsNone(queue.claim_next(), "Cloud capture must not change a running device snapshot")
        self.assertEqual(self.result(claim, '{"categories":[{"path":["技术"],"description":"技术资料"}]}').status_code, 200)
        collected, attempt = queue.claim_next()
        self.assertEqual(collected.id, late.id)
        queue.finish(collected, attempt)
        claim = self.post("local-ai/claim", {}).json()["data"]
        row = json.loads(claim["messages"][-1]["content"])["websites"][0]
        self.assertTrue(row["metadata_available"])
        self.assertEqual(row["description"], "Collected before inference")

    def test_storage_failure_requires_explicit_original_device_resume(self):
        _, claim = self.start()
        response = self.post("local-ai/pause", {**claim, "error_code": "LOCAL_STORAGE_UNAVAILABLE"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.post("local-ai/claim", {}).json()["data"]["paused"])
        self.assertTrue(self.post("local-ai/claim", {"resume_request_id": claim["request_id"]}).json()["data"]["paused"])
        resumed = self.post("local-ai/claim", {"resume_request_id": claim["request_id"], "manual_resume": True, "has_response": True}).json()["data"]
        self.assertNotEqual(resumed["lease_token"], claim["lease_token"])
        self.assertEqual(self.result(resumed, '{"categories":[{"path":["技术"],"description":"技术资料"}]}').status_code, 200)

    def test_first_global_taxonomy_waits_for_an_unselected_bookmarks_running_capture(self):
        from library import queue
        from library.models import LocalAIRequest
        self.register()
        selected = self.post("bookmarks", {"url": "https://example.org/selected", "snapshot_enabled": False}).json()["data"]
        other = self.post("bookmarks", {"url": "https://example.org/unselected", "snapshot_enabled": False}).json()["data"]
        queue.enqueue("metadata", other["id"], priority=90)
        material, attempt = queue.claim_next()
        self.assertEqual(self.post("classification/batches", {"bookmark_ids": [selected["id"]]}).status_code, 202)
        self.assertTrue(self.post("local-ai/claim", {}).json()["data"]["waiting"])
        self.assertFalse(LocalAIRequest.objects.exists())
        queue.finish(material, attempt)
        claim = self.post("local-ai/claim", {}).json()["data"]
        self.assertEqual(claim["stage"], "taxonomy")

    def test_registered_device_needs_its_local_proof_and_never_receives_it_back(self):
        registered = self.register()
        self.assertEqual(registered["id"], self.device)
        self.assertNotIn(self.proof, json.dumps(registered))
        response = self.client.get("/api/v1/local-ai/device", HTTP_X_LINKARIUM_DEVICE=self.device,
            HTTP_X_LINKARIUM_DEVICE_PROOF=self.proof)
        self.assertEqual(response.status_code, 200, response.content)
        impostor = self.client.get("/api/v1/local-ai/device", HTTP_X_LINKARIUM_DEVICE=self.device,
            HTTP_X_LINKARIUM_DEVICE_PROOF="incorrect")
        self.assertEqual(impostor.status_code, 403)

    def test_device_executes_prompt_and_duplicate_result_creates_one_review(self):
        self.register()
        saved = self.post("bookmarks", {"url": "https://example.com/local", "title": "Local", "snapshot_enabled": False})
        bookmark_id = saved.json()["data"]["id"]
        started = self.post("classification/batches", {"bookmark_ids": [bookmark_id]})
        self.assertEqual(started.status_code, 202, started.content)
        from library.queue import claim_next
        # Server workers may fetch metadata; they can never claim model work.
        from library.models import Job, ClassificationProposal
        Job.objects.filter(kind="metadata").update(state="succeeded", active_key=None)
        self.assertIsNone(claim_next())
        claim = self.post("local-ai/claim", {}).json()["data"]
        self.assertEqual(claim["stage"], "taxonomy")
        def complete(request, result):
            return self.post("local-ai/result", {"request_id": request["request_id"],
                "attempt_id": request["attempt_id"], "lease_token": request["lease_token"],
                "prompt_digest": request["prompt_digest"], "response": json.dumps(result)})
        self.assertEqual(complete(claim, {"categories": [{"path": ["技术"], "description": "技术资源"}]}).status_code, 200)
        claim = self.post("local-ai/claim", {}).json()["data"]
        self.assertEqual(claim["stage"], "classification")
        result = {"items": [{"bookmark_id": bookmark_id,
            "target": {"parent_folder_id": None, "new_segments": ["技术"]}, "reason": "技术资料"}]}
        other = str(uuid.uuid4())
        original = self.device
        self.device = other
        self.register()
        self.assertIsNone(self.post("local-ai/claim", {}).json()["data"])
        self.assertEqual(complete(claim, result).status_code, 403)
        self.device = original
        accepted = complete(claim, result)
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(complete(claim, result).json(), accepted.json())
        self.assertEqual(ClassificationProposal.objects.filter(status="pending").count(), 1)
        self.assertIsNone(ClassificationProposal.objects.get().bookmark.folder_id)
        self.assertEqual(complete(claim, {"items": []}).status_code, 409)
