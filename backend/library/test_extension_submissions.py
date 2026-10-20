"""Firefox submissions exercised through the authenticated HTTP contract."""
import json
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client

from runtime.models import RuntimeState
from runtime.testing import force_credential_login
from .models import LibrarySettings
from .worker import run_one
from tests.run_ai_acceptance_provider import Handler


class ExtensionSubmissionTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        user = get_user_model().objects.create_user("owner")
        force_credential_login(self.client, user)

    def submit(self, key="first-click", **fields):
        return self.client.post("/api/v1/extension/submissions",
            data=json.dumps({"url": "https://example.org/article", "title": "An article", **fields}),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY=key)

    def json_request(self, method, path, data):
        return getattr(self.client, method)("/api/v1/" + path,
            data=json.dumps(data), content_type="application/json")

    def configure_ai(self, handler=Handler):
        provider = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        response = self.json_request("post", "ai-profiles", {
            "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
            "model": "protocol-fixture", "api_key": "ui-fixture-key"})
        self.assertEqual(response.status_code, 201, response.content)
        profile_id = response.json()["data"]["id"]
        self.json_request("post", f"ai-profiles/{profile_id}/test", {})
        self.assertTrue(run_one())
        return provider

    def test_click_saves_a_retrievable_starred_bookmark_and_replays_the_submission(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.content)
        saved = response.json()["data"]
        bookmark = self.client.get(f'/api/v1/bookmarks/{saved["bookmark_id"]}').json()["data"]
        self.assertEqual(bookmark["url"], "https://example.org/article")
        self.assertEqual(bookmark["title"], "An article")
        self.assertTrue(bookmark["is_starred"])
        self.assertIsNone(bookmark["folder_id"])
        self.assertEqual(self.submit().json()["data"], saved)
        status = self.client.get(saved["status_url"])
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["data"]["submission_id"], saved["submission_id"])
        duplicate = self.submit(key="second-click").json()["data"]
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["state"], "already_saved")
        self.assertEqual(duplicate["bookmark_id"], saved["bookmark_id"])

    def test_worker_automatically_files_only_the_extension_submission(self):
        self.configure_ai()
        saved = self.submit().json()["data"]
        with patch("capture.LauncherClient.capture", return_value={
                "title": "An article", "description": "Public page", "text_excerpt": "Art techniques"}):
            self.assertTrue(run_one())
        # The next job must classify the enriched page, independently of snapshots.
        self.assertTrue(run_one())
        status = self.client.get(saved["status_url"]).json()["data"]
        self.assertEqual(status["state"], "classified", status)
        self.assertEqual(status["folder_path"], "AI 协议验收 / 待校对")
        bookmark = self.client.get(f'/api/v1/bookmarks/{saved["bookmark_id"]}').json()["data"]
        self.assertEqual(bookmark["folder_path"], "AI 协议验收 / 待校对")
        proposals = self.client.get("/api/v1/classification/proposals", {
            "bookmark_id": saved["bookmark_id"], "status": "pending"}).json()["data"]
        self.assertEqual(proposals, [])

    def test_missing_ai_preserves_the_bookmark_and_can_resume_after_configuration(self):
        saved = self.submit().json()["data"]
        with patch("capture.LauncherClient.capture", return_value={"title": "An article"}):
            run_one()
        status = self.client.get(saved["status_url"]).json()["data"]
        self.assertEqual(status["state"], "needs_attention")
        self.assertEqual(status["error_code"], "AI_UNCONFIGURED")
        self.configure_ai()
        response = self.client.post(saved["status_url"] + "/retry",
            data=json.dumps({"revision": status["revision"]}), content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="retry-after-configuration")
        self.assertEqual(response.status_code, 202, response.content)
        run_one()
        self.assertEqual(self.client.get(saved["status_url"]).json()["data"]["state"], "classified")
        self.assertEqual(self.client.get("/api/v1/bookmarks").json()["page"]["total"], 1)

    def test_manual_folder_change_prevents_a_late_automatic_move(self):
        self.configure_ai()
        saved = self.submit().json()["data"]
        with patch("capture.LauncherClient.capture", return_value={"title": "An article"}):
            run_one()
        folder = self.json_request("post", "folders", {"name": "My choice"}).json()["data"]
        bookmark = self.client.get(f'/api/v1/bookmarks/{saved["bookmark_id"]}').json()["data"]
        self.json_request("patch", f'bookmarks/{saved["bookmark_id"]}', {
            "revision": bookmark["revision"], "folder_id": folder["id"]})
        run_one()
        status = self.client.get(saved["status_url"]).json()["data"]
        self.assertEqual(status["state"], "needs_attention")
        self.assertEqual(status["error_code"], "SUBMISSION_STALE")
        self.assertEqual(self.client.get(f'/api/v1/bookmarks/{saved["bookmark_id"]}').json()["data"]["folder_id"], folder["id"])
        response = self.client.post(saved["status_url"] + "/retry", data=json.dumps({"revision": status["revision"]}),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY="cannot-overwrite-manual")
        self.assertEqual(response.status_code, 409)

    def test_untrusted_submission_and_uncredentialed_writes_are_rejected(self):
        anonymous = Client()
        self.assertEqual(anonymous.post("/api/v1/extension/submissions", data="{}",
            content_type="application/json").status_code, 401)
        csrf_client = Client(enforce_csrf_checks=True)
        force_credential_login(csrf_client, get_user_model().objects.get(username="owner"))
        self.assertEqual(csrf_client.post("/api/v1/extension/submissions", data="{}",
            content_type="application/json").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/extension/submissions", data="{}",
            content_type="application/json").json()["error"]["code"], "IDEMPOTENCY_REQUIRED")
        self.assertEqual(self.submit(url="file:///private.txt").status_code, 400)
        self.assertEqual(self.submit(auto_apply=True).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/bookmarks").json()["page"]["total"], 0)

    def test_duplicate_preserves_existing_manual_fields_and_no_new_jobs(self):
        folder = self.json_request("post", "folders", {"name": "Hand-picked"}).json()["data"]
        original = self.json_request("post", "bookmarks", {"url": "https://example.org/article",
            "title": "My title", "folder_id": folder["id"], "notes": "My notes", "is_starred": False}).json()["data"]
        before = self.client.get("/api/v1/job-groups").json()["page"]["total"]
        duplicate = self.submit().json()["data"]
        self.assertEqual(duplicate["state"], "already_saved")
        bookmark = self.client.get(f'/api/v1/bookmarks/{original["id"]}').json()["data"]
        self.assertEqual((bookmark["title"], bookmark["notes"], bookmark["folder_id"], bookmark["is_starred"]),
            ("My title", "My notes", folder["id"], False))
        self.assertEqual(self.client.get("/api/v1/job-groups").json()["page"]["total"], before)

    def test_failed_metadata_still_classifies_using_the_submitted_title_and_url(self):
        from capture.models import CaptureError
        self.configure_ai()
        saved = self.submit().json()["data"]
        with patch("capture.LauncherClient.capture", side_effect=CaptureError("access_denied", "Fixture denial")):
            run_one()
        run_one()
        status = self.client.get(saved["status_url"]).json()["data"]
        self.assertEqual(status["state"], "classified", status)
        self.assertEqual(status["metadata_error_code"], "CAPTURE_ACCESS_DENIED")

    def test_original_web_classification_still_needs_human_acceptance(self):
        self.configure_ai()
        with self.captureOnCommitCallbacks(using="library", execute=True):
            bookmark = self.json_request("post", "bookmarks", {
                "url": "https://example.org/web-added", "snapshot_enabled": False}).json()["data"]
        with patch("capture.LauncherClient.capture", return_value={"title": "Web-added"}):
            run_one()
        run_one()
        unchanged = self.client.get(f'/api/v1/bookmarks/{bookmark["id"]}').json()["data"]
        self.assertIsNone(unchanged["folder_id"])
        proposals = self.client.get("/api/v1/classification/proposals", {
            "bookmark_id": bookmark["id"], "status": "pending"}).json()["data"]
        self.assertEqual(len(proposals), 1, self.client.get("/api/v1/job-groups").json())

    def test_task_window_retry_recovers_the_same_submission_after_provider_failure(self):
        class FlakyProvider(Handler):
            unavailable = False

            def do_POST(self):
                if self.unavailable:
                    self.send_error(500)
                else:
                    super().do_POST()

        self.configure_ai(FlakyProvider)
        FlakyProvider.unavailable = True
        saved = self.submit().json()["data"]
        with patch("capture.LauncherClient.capture", return_value={"title": "An article"}):
            run_one()
        run_one()
        status = self.client.get(saved["status_url"]).json()["data"]
        self.assertEqual(status["state"], "needs_attention")
        group = self.client.get(f'/api/v1/job-groups/{saved["group_id"]}').json()["data"]
        failed_job = next(row for row in group["jobs"] if row["state"] == "failed")
        FlakyProvider.unavailable = False
        response = self.json_request("post", f'jobs/{failed_job["id"]}/retry', {})
        self.assertEqual(response.status_code, 202, response.content)
        run_one()
        self.assertEqual(self.client.get(saved["status_url"]).json()["data"]["state"], "classified")

    def test_manual_classification_supersedes_automatic_filing_before_metadata_finishes(self):
        self.configure_ai()
        saved = self.submit().json()["data"]
        response = self.json_request("post", "classification/batches", {"bookmark_ids": [saved["bookmark_id"]]})
        self.assertEqual(response.status_code, 202)
        run_one()
        with patch("capture.LauncherClient.capture", return_value={"title": "An article"}):
            run_one()
        status = self.client.get(saved["status_url"]).json()["data"]
        self.assertEqual((status["state"], status["error_code"]), ("needs_attention", "TASK_SUPERSEDED"))
        self.assertIsNone(self.client.get(f'/api/v1/bookmarks/{saved["bookmark_id"]}').json()["data"]["folder_id"])

    def test_manual_classification_also_withdraws_authority_from_a_blocked_submission(self):
        saved = self.submit().json()["data"]
        with patch("capture.LauncherClient.capture", return_value={"title": "An article"}):
            run_one()
        self.configure_ai()
        self.json_request("post", "classification/batches", {"bookmark_ids": [saved["bookmark_id"]]})
        run_one()
        status = self.client.get(saved["status_url"]).json()["data"]
        response = self.client.post(saved["status_url"] + "/retry", data=json.dumps({"revision": status["revision"]}),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY="old-blocked-authority")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(status["error_code"], "TASK_SUPERSEDED")
