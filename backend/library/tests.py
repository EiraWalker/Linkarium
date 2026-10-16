import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from config.paths import generation_root
from runtime.models import RuntimeState, MaintenanceOperation
from . import catalog, queue
from .errors import ApiError
from .models import *


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CatalogTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        get_user_model().objects.create_user("owner", password="test-only-secret")
        self.client.force_login(get_user_model().objects.get(username="owner"))

    def request_json(self, method, path, data=None, **extra):
        return getattr(self.client, method)("/api/v1/" + path, data=json.dumps(data or {}), content_type="application/json", **extra)

    def add(self, url="https://example.org", **fields):
        return catalog.create_bookmark({"url": url, "snapshot_enabled": False, **fields})[0]

    def review_fixture(self, count=121):
        group = JobGroup.objects.create(kind="classification", requested_count=count)
        batch = ClassificationBatch.objects.create(job_group=group, tree_revision=1)
        sites = Bookmark.objects.bulk_create([Bookmark(original_url=f"https://review.example.org/{n}", canonical_url=f"https://review.example.org/{n}", domain="review.example.org", snapshot_enabled=False) for n in range(count)])
        proposals = ClassificationProposal.objects.bulk_create([ClassificationProposal(batch=batch, bookmark=site,
            expected_classification_revision=1, expected_tree_revision=1,
            target_path={"parent_folder_id": None, "new_segments": ["全页批准", "网站"]}) for site in sites])
        return group, sites, proposals

    def test_review_selection_covers_all_pages_and_keeps_scope_and_status(self):
        group, sites, proposals = self.review_fixture()
        outside = self.add("https://outside.example.org")
        other = ClassificationBatch.objects.create(tree_revision=1)
        ClassificationProposal.objects.create(batch=other, bookmark=outside, expected_classification_revision=1, expected_tree_revision=1)
        ClassificationProposal.objects.create(batch=proposals[0].batch, bookmark=sites[0], expected_classification_revision=1, expected_tree_revision=1, status="stale")
        path = f"/api/v1/classification/proposals?status=pending&group_id={group.id}"
        regular = self.client.get(path).json()
        self.assertEqual((len(regular["data"]), regular["page"]["total"]), (48, 121))
        selected = self.client.get(path + "&selection=true&page=2&page_size=1").json()["data"]
        self.assertEqual(len(selected), 121)
        self.assertTrue(all(set(row) == {"id", "revision"} for row in selected))
        self.assertEqual({row["id"] for row in selected}, {str(p.id) for p in proposals})
        imported = ImportBatch.objects.create(classification_group=group)
        self.assertEqual(self.client.get(f"/api/v1/classification/proposals?status=pending&import_id={imported.id}&selection=true").json()["data"], selected)
        one = self.client.get(path + f"&bookmark_id={sites[0].id}&selection=true").json()["data"]
        self.assertEqual(one, [{"id": str(proposals[0].id), "revision": 1}])

    def test_all_pages_approval_exceeds_100_and_is_idempotent(self):
        group, sites, proposals = self.review_fixture()
        outside = self.add("https://outside.example.org")
        other = ClassificationBatch.objects.create(tree_revision=1)
        pending = ClassificationProposal.objects.create(batch=other, bookmark=outside, expected_classification_revision=1, expected_tree_revision=1)
        selected = self.client.get(f"/api/v1/classification/proposals?status=pending&group_id={group.id}&selection=true").json()["data"]
        body = {"items": selected, "tree_revision": 1}
        response = self.request_json("post", "classification/accept", body, HTTP_IDEMPOTENCY_KEY="approve-all-pages")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["accepted"], 121)
        self.assertEqual(Bookmark.objects.filter(pk__in=[s.id for s in sites], folder__name="网站").count(), 121)
        self.assertEqual(Folder.objects.count(), 2)
        self.assertEqual(ClassificationProposal.objects.filter(status="accepted").count(), 121)
        outside.refresh_from_db(); pending.refresh_from_db()
        self.assertIsNone(outside.folder_id)
        self.assertEqual(pending.status, "pending")
        repeated = self.request_json("post", "classification/accept", body, HTTP_IDEMPOTENCY_KEY="approve-all-pages")
        self.assertEqual(repeated.json(), response.json())
        self.assertEqual(Folder.objects.count(), 2)

    def test_all_pages_stale_item_rejects_everything_and_limit_is_1000(self):
        group, sites, proposals = self.review_fixture()
        selected = [{"id": str(p.id), "revision": 1} for p in proposals]
        Bookmark.objects.filter(pk=sites[-1].id).update(classification_revision=2)
        response = self.request_json("post", "classification/accept", {"items": selected, "tree_revision": 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "PROPOSAL_STALE")
        self.assertEqual(Folder.objects.count(), 0)
        self.assertFalse(Bookmark.objects.exclude(folder_id=None).exists())
        self.assertEqual(ClassificationProposal.objects.filter(status="pending").count(), 121)
        oversized = [{"id": str(uuid.uuid4()), "revision": 1} for _ in range(1001)]
        response = self.request_json("post", "classification/accept", {"items": oversized, "tree_revision": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_BATCH")

    def test_task_catalog_restores_active_and_completed_groups_and_review_counts(self):
        site = self.add()
        active = JobGroup.objects.create(kind="classification", requested_count=1)
        job = queue.enqueue("classification", payload={"bookmark_ids": [str(site.id)]}, group=active)
        batch = ClassificationBatch.objects.create(job_group=active, tree_revision=1)
        ClassificationProposal.objects.create(batch=batch, bookmark=site, expected_classification_revision=site.classification_revision, expected_tree_revision=1, target_path={"existing_folder_id": None})
        completed = JobGroup.objects.create(kind="refresh", requested_count=1)
        done = queue.enqueue("capture", site.id, group=completed)
        Job.objects.filter(pk=done.id).update(state="succeeded")
        response = self.client.get("/api/v1/job-groups?page_size=1")
        self.assertEqual(response.status_code, 200)
        first = response.json()
        self.assertEqual(first["page"]["total"], 2)
        self.assertEqual(first["data"]["active_count"], 1)
        self.assertEqual(first["data"]["pending_proposals"], 1)
        summary = first["data"]["groups"][0]
        self.assertEqual(summary["id"], str(active.id))
        self.assertEqual(summary["state"], "queued")
        self.assertEqual(summary["pending_count"], 1)
        self.assertNotIn("jobs", summary)
        second = self.client.get("/api/v1/job-groups?page=2&page_size=1").json()
        self.assertEqual(second["data"]["groups"][0]["id"], str(completed.id))
        self.assertEqual(second["data"]["groups"][0]["state"], "succeeded")
        self.assertEqual(first["data"]["current_group"]["id"], str(active.id))
        self.assertEqual(second["data"]["current_group"]["id"], str(active.id))
        # A new client/device sees the same durable results and task IDs.
        other = Client()
        other.force_login(get_user_model().objects.get(username="owner"))
        self.assertEqual(other.get("/api/v1/job-groups").json()["data"]["pending_proposals"], 1)
        self.assertEqual(other.get(f"/api/v1/classification/proposals?group_id={active.id}").json()["page"]["total"], 1)
        self.assertEqual(Client().get("/api/v1/job-groups").status_code, 401)

    def test_task_summary_tracks_newest_active_request_across_history_pages(self):
        older = JobGroup.objects.create(kind="refresh", requested_count=1)
        old_job = queue.enqueue("capture", group=older)
        Job.objects.filter(pk=old_job.id).update(state="running")
        current = JobGroup.objects.create(kind="classification", requested_count=482)
        new_job = queue.enqueue("classification", group=current)
        page = self.client.get("/api/v1/job-groups?page_size=1").json()
        self.assertEqual(page["data"]["groups"][0]["id"], str(older.id))
        self.assertEqual(page["data"]["current_group"]["id"], str(current.id))
        second = self.client.get("/api/v1/job-groups?page=2&page_size=1").json()
        self.assertEqual(second["data"]["current_group"], page["data"]["current_group"])
        Job.objects.filter(pk__in=[old_job.id, new_job.id]).update(state="succeeded")
        result = self.client.get("/api/v1/job-groups").json()["data"]
        self.assertEqual(result["current_group"]["id"], str(current.id))
        self.assertEqual(result["current_group"]["state"], "succeeded")

    def test_catalog_completion_revision_changes_on_success_and_retryable_error(self):
        bookmark = self.add()
        group = JobGroup.objects.create(kind="classification", requested_count=2)
        queue.enqueue("classification", payload={"bookmark_ids": [str(bookmark.id)]}, group=group)
        def version():
            return self.client.get("/api/v1/job-groups").json()["data"]["completion_revision"]
        self.assertEqual(version(), 0)
        job, attempt = queue.claim_next()
        self.assertEqual(version(), 0)
        queue.finish(job, attempt, error_code="AI_TIMEOUT", retryable=True)
        self.assertEqual(version(), 1)
        self.assertEqual(queue.group_status(group)["completed"], 0)
        queue.retry_job(job.id)
        self.assertEqual(version(), 1)
        job, attempt = queue.claim_next()
        queue.finish(job, attempt, result={"proposal_count": 1})
        self.assertEqual(version(), 2)
        # Every history page carries the global revision, not only visible jobs.
        self.assertEqual(self.client.get("/api/v1/job-groups?page=2&page_size=1").json()["data"]["completion_revision"], 2)

    def test_first_capture_failure_and_other_device_queue_are_distinct_in_catalog(self):
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id])
        job, attempt = queue.claim_next()
        queue.finish(job, attempt, error_code="CAPTURE_BLOCKED", retryable=False)
        failed = self.client.get(f"/api/v1/bookmarks?variant={job.variant}").json()["data"][0]
        self.assertIsNone(failed["preview"])
        self.assertEqual(failed["capture_status"], {"state": "failed", "last_error_code": "CAPTURE_BLOCKED"})
        other_variant = "mobile" if job.variant == "desktop" else "desktop"
        queued = self.client.get(f"/api/v1/bookmarks?variant={other_variant}").json()["data"][0]
        self.assertEqual(queued["capture_status"]["state"], "queued")
        catalog.update_bookmark(bookmark.id, {"revision": bookmark.revision, "snapshot_enabled": False})
        disabled = self.client.get(f"/api/v1/bookmarks?variant={job.variant}").json()["data"][0]
        self.assertEqual(disabled["capture_status"]["state"], "disabled")

    def test_capture_staging_cleanup_keeps_published_library_files(self):
        from .worker import cleanup_capture_work
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id])
        _, attempt = queue.claim_next()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stage = root / "work" / "capture" / str(attempt.id)
            stage.mkdir(parents=True)
            (stage / "artifact.html").write_text("temporary archive", encoding="utf-8")
            published = root / "generations" / "initial" / "artifact.html"
            published.parent.mkdir(parents=True)
            published.write_text("published archive", encoding="utf-8")
            with patch("library.worker.DATA_ROOT", root):
                cleanup_capture_work(attempt)
            self.assertFalse(stage.exists())
            self.assertEqual(published.read_text(encoding="utf-8"), "published archive")

    def test_normalized_identity_keeps_spa_query_and_fragment(self):
        first = self.add("HTTPS://ExAmPle.org:443")
        second, created = catalog.create_bookmark({"url": "https://example.org/", "notes": "must not overwrite"})
        self.assertFalse(created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.notes, "")
        self.assertNotEqual(self.add("https://example.org/#one").id, self.add("https://example.org/#two").id)
        with self.assertRaises(ApiError):
            self.add("https://name:password@example.org")

    def test_folder_unicode_names_cycles_and_subtree_deletion(self):
        parent = catalog.create_folder({"name": "工具"})
        child = catalog.create_folder({"name": "开发", "parent_id": str(parent.id)})
        website = self.add(folder_id=str(child.id))
        with self.assertRaises(ApiError):
            catalog.update_folder(parent.id, {"revision": parent.revision, "parent_id": str(child.id)})
        catalog.create_folder({"name": "Ａ"})
        with self.assertRaises(ApiError):
            catalog.create_folder({"name": "a"})
        preview = catalog.deletion_preview("folder", [parent.id])
        catalog.delete_catalog("folder", [parent.id], preview["preview_token"])
        self.assertFalse(Bookmark.objects.filter(pk=website.id).exists())
        self.assertFalse(Folder.objects.filter(pk=child.id).exists())

    def test_star_does_not_delete_or_invalidate_capture(self):
        bookmark = self.add()
        initial_capture = bookmark.capture_epoch
        bookmark = catalog.update_bookmark(bookmark.id, {"revision": bookmark.revision, "is_starred": True})
        bookmark = catalog.update_bookmark(bookmark.id, {"revision": bookmark.revision, "is_starred": False})
        self.assertFalse(bookmark.is_starred)
        self.assertEqual(bookmark.capture_epoch, initial_capture)
        self.assertTrue(Bookmark.objects.filter(pk=bookmark.id).exists())

    def test_stale_revision_cannot_overwrite_another_device(self):
        bookmark = self.add()
        self.request_json("patch", f"bookmarks/{bookmark.id}", {"revision": 1, "notes": "desktop"})
        response = self.request_json("patch", f"bookmarks/{bookmark.id}", {"revision": 1, "notes": "phone"})
        self.assertEqual(response.status_code, 409)
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.notes, "desktop")

    def test_url_change_discards_old_metadata_even_when_snapshots_disabled(self):
        bookmark = self.add()
        PageMetadata.objects.create(bookmark=bookmark, title="Old", description="Old description", text_excerpt="Old body", fetched_at=timezone.now(), source_url=bookmark.canonical_url, content_hash="a" * 64)
        bookmark.source_title = "Old"
        bookmark.save()
        with self.captureOnCommitCallbacks(using="library", execute=True):
            bookmark = catalog.update_bookmark(bookmark.id, {"revision": 1, "url": "https://example.net/new"})
        self.assertFalse(PageMetadata.objects.filter(bookmark=bookmark).exists())
        self.assertEqual(bookmark.source_title, "")
        self.assertTrue(Job.objects.filter(kind="metadata", target_id=bookmark.id, input_revision=bookmark.capture_epoch).exists())

    def test_changed_delete_preview_does_not_delete_new_children(self):
        folder = catalog.create_folder({"name": "资料"})
        self.add(folder_id=str(folder.id))
        preview = catalog.deletion_preview("folder", [folder.id])
        self.add("https://example.org/new", folder_id=str(folder.id))
        with self.assertRaises(ApiError) as raised:
            catalog.delete_catalog("folder", [folder.id], preview["preview_token"])
        self.assertEqual(raised.exception.code, "PREVIEW_STALE")
        self.assertEqual(Bookmark.objects.count(), 2)

    def test_partial_search_returns_only_sites_with_folder_ancestors(self):
        folder = catalog.create_folder({"name": "设计灵感"})
        child = catalog.create_folder({"name": "字体", "parent_id": folder.id})
        match = self.add("https://fonts.example.org", folder_id=child.id)
        self.add("https://unrelated.example.org")
        response = self.client.get("/api/v1/bookmarks?q=设计&fields=folder")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["id"] for r in response.json()["data"]], [str(match.id)])
        response = self.client.get("/api/v1/bookmarks?fields=")
        self.assertEqual(response.status_code, 400)

    def test_bulk_write_is_atomic_when_one_revision_is_stale(self):
        first = self.add()
        second = self.add("https://example.net")
        tag = catalog.create_tag({"name": "参考"})
        response = self.request_json("post", "bookmarks/bulk", {"action": "add_tags", "items": [{"id": str(first.id), "revision": 1}, {"id": str(second.id), "revision": 0}], "tag_ids": [str(tag.id)]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(first.tags.count(), 0)

    def test_idempotency_same_key_different_request_is_rejected(self):
        response = self.request_json("post", "bookmarks", {"url": "https://example.org", "snapshot_enabled": False}, HTTP_IDEMPOTENCY_KEY="same-add")
        self.assertEqual(response.status_code, 201)
        again = self.request_json("post", "bookmarks", {"url": "https://example.org", "snapshot_enabled": False}, HTTP_IDEMPOTENCY_KEY="same-add")
        self.assertEqual(response.json(), again.json())
        changed = self.request_json("post", "bookmarks", {"url": "https://example.net", "snapshot_enabled": False}, HTTP_IDEMPOTENCY_KEY="same-add")
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(Bookmark.objects.count(), 1)

    def selection_fixture(self, count=121):
        folder = catalog.create_folder({"name": "跨页选择"})
        child = catalog.create_folder({"name": "子目录", "parent_id": folder.id})
        bookmarks = Bookmark.objects.bulk_create([
            Bookmark(original_url=f"https://selection.example.org/{n}",
                     canonical_url=f"https://selection.example.org/{n}",
                     domain="selection.example.org", source_title=f"Selection {n}",
                     snapshot_enabled=False, folder=child, is_starred=True)
            for n in range(count)
        ])
        outside = self.add("https://outside.example.org")
        return folder, bookmarks, outside

    def test_selection_returns_all_pages_with_current_filters(self):
        folder, bookmarks, outside = self.selection_fixture()
        tag = catalog.create_tag({"name": "选择范围"})
        BookmarkTag.objects.bulk_create([BookmarkTag(bookmark=b, tag=tag) for b in bookmarks])
        filters = {"scope": "folder", "folder_id": str(folder.id), "include_descendants": "true",
                   "q": "Selection", "fields": "title", "starred": "true", "tag_ids": str(tag.id)}
        page = self.client.get("/api/v1/bookmarks", {**filters, "page_size": 48}).json()
        self.assertEqual(len(page["data"]), 48)
        self.assertEqual(page["page"]["total"], 121)
        response = self.client.get("/api/v1/bookmarks", {**filters, "selection": "true", "page": 3})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual({row["id"] for row in data}, {str(b.id) for b in bookmarks})
        self.assertTrue(all(set(row) == {"id", "revision"} for row in data))
        self.assertNotIn(str(outside.id), {row["id"] for row in data})

    def test_cross_page_bulk_move_tags_and_delete(self):
        _, bookmarks, outside = self.selection_fixture()
        target = catalog.create_folder({"name": "目标目录"})
        tag = catalog.create_tag({"name": "全部页"})
        def items():
            return [{"id": str(b.id), "revision": b.revision} for b in Bookmark.objects.exclude(pk=outside.id)]
        for action, extra in [("move", {"folder_id": str(target.id)}),
                              ("add_tags", {"tag_ids": [str(tag.id)]}),
                              ("remove_tags", {"tag_ids": [str(tag.id)]})]:
            response = self.request_json("post", "bookmarks/bulk", {"action": action, "items": items(), **extra})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(Bookmark.objects.filter(folder=target).count(), 121)
            self.assertEqual(BookmarkTag.objects.filter(tag=tag).count(), 121 if action == "add_tags" else 0)
        preview = self.request_json("post", "bookmarks/delete-preview", {"ids": [str(b.id) for b in bookmarks]})
        self.assertEqual(preview.status_code, 200, preview.content)
        response = self.request_json("post", "bookmarks/bulk", {
            "action": "delete", "items": items(), "preview_token": preview.json()["data"]["preview_token"]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(list(Bookmark.objects.values_list("pk", flat=True)), [outside.id])

    def test_cross_page_stale_revision_rolls_back_and_over_limit_rejected(self):
        _, bookmarks, _ = self.selection_fixture()
        target = catalog.create_folder({"name": "目标目录"})
        items = [{"id": str(b.id), "revision": b.revision} for b in bookmarks]
        items[-1]["revision"] = 0
        response = self.request_json("post", "bookmarks/bulk", {
            "action": "move", "items": items, "folder_id": str(target.id)})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Bookmark.objects.filter(folder=target).exists())
        oversized = [{"id": str(uuid.uuid4()), "revision": 1} for _ in range(1001)]
        response = self.request_json("post", "bookmarks/bulk", {"action": "move", "items": oversized})
        self.assertEqual(response.status_code, 400)
        response = self.request_json("post", "bookmarks/delete-preview", {"ids": [b["id"] for b in oversized]})
        self.assertEqual(response.status_code, 400)

    def test_duplicate_refresh_reuses_device_tasks(self):
        bookmark = self.add(snapshot_enabled=True)
        first = queue.request_refresh([bookmark.id])
        second = queue.request_refresh([bookmark.id])
        self.assertEqual(Job.objects.filter(kind="capture").count(), 2)
        self.assertEqual(set(first.members.values_list("job_id", flat=True)), set(second.members.values_list("job_id", flat=True)))

    def test_all_sites_refresh_works_without_physical_folders_and_deduplicates(self):
        enabled = [self.add(f"https://example.org/all-{n}", snapshot_enabled=True) for n in range(2)]
        self.add("https://example.org/disabled")
        queue.request_refresh([enabled[0].id])
        preview = self.request_json("post", "folders/all/refresh-preview")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.json()["data"], {"total": 3, "eligible": 2, "disabled": 1, "active": 2})
        for _ in range(2):
            response = self.request_json("post", "folders/all/refresh")
            self.assertEqual(response.status_code, 202, response.content)
            group = JobGroup.objects.get(pk=response.json()["data"]["group_id"])
            self.assertEqual(group.source, "all")
            self.assertEqual(group.members.count(), 4)
        self.assertEqual(Job.objects.filter(kind="capture").count(), 4)
        self.assertEqual(Folder.objects.count(), 0)
        for method in ("patch", "delete"):
            self.assertEqual(self.request_json(method, "folders/all", {"revision": 1}).status_code, 400)

    def test_folder_refresh_stays_scoped_to_descendants(self):
        parent = catalog.create_folder({"name": "范围"})
        child = catalog.create_folder({"name": "子目录", "parent_id": parent.id})
        inside = self.add("https://example.org/inside", folder_id=child.id, snapshot_enabled=True)
        self.add("https://example.org/outside", snapshot_enabled=True)
        response = self.request_json("post", f"folders/{parent.id}/refresh")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(set(Job.objects.filter(kind="capture").values_list("target_id", flat=True)), {inside.id})
        self.assertEqual(Job.objects.filter(kind="capture").count(), 2)

    def test_all_sites_ai_classification_includes_every_page_and_unclassified(self):
        folder, bookmarks, outside = self.selection_fixture()
        profile = AIProfile.objects.create(base_url="https://ai.example.org", model="fixture")
        LibrarySettings.objects.filter(pk=1).update(active_ai_profile_id=profile.id)
        for target, expected in [("all", {str(b.id) for b in bookmarks} | {str(outside.id)}),
                                 (str(folder.id), {str(b.id) for b in bookmarks})]:
            with patch("library.classification.verified_client"):
                response = self.request_json("post", "classification/batches", {"folder_id": target})
            self.assertEqual(response.status_code, 202, response.content)
            group = JobGroup.objects.get(pk=response.json()["data"]["group_id"])
            self.assertEqual(group.requested_count, len(expected))
            status = queue.group_status(group)
            self.assertEqual(status["state"], "queued")
            self.assertEqual(status["website_total"], len(expected))
            self.assertEqual(status["queued_sites"], len(expected))
            self.assertEqual(status["website_completed"], 0)
            self.assertEqual(sum(row["website_count"] for row in status["jobs"]), len(expected))
            queued = {identity for job in Job.objects.filter(memberships__group=group) for identity in job.payload["bookmark_ids"]}
            self.assertEqual(queued, expected)
        self.assertEqual(Folder.objects.count(), 2)

    def test_selected_ai_classification_includes_classified_and_unclassified_without_moving(self):
        folder, bookmarks, outside = self.selection_fixture()
        selected = [str(b.id) for b in bookmarks[:-1]] + [str(outside.id)]
        original_folders = dict(Bookmark.objects.values_list("id", "folder_id"))
        profile = AIProfile.objects.create(base_url="https://ai.example.org", model="fixture")
        LibrarySettings.objects.filter(pk=1).update(active_ai_profile_id=profile.id)
        with patch("library.classification.verified_client"):
            response = self.request_json("post", "classification/batches", {"bookmark_ids": selected})
        self.assertEqual(response.status_code, 202, response.content)
        group = JobGroup.objects.get(pk=response.json()["data"]["group_id"])
        self.assertEqual(group.requested_count, 121)
        queued = {identity for job in Job.objects.filter(memberships__group=group) for identity in job.payload["bookmark_ids"]}
        self.assertEqual(queued, set(selected))
        self.assertNotIn(str(bookmarks[-1].id), queued)
        self.assertEqual(dict(Bookmark.objects.values_list("id", "folder_id")), original_folders)
        outside.refresh_from_db()
        self.assertIsNone(outside.folder_id)

    def test_all_sites_ai_requires_configured_service(self):
        self.add()
        response = self.request_json("post", "classification/batches", {"folder_id": "all"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "AI_UNCONFIGURED")
        self.assertFalse(Job.objects.filter(kind="classification").exists())

    def test_classification_progress_distinguishes_sites_batches_and_retries(self):
        _, bookmarks, outside = self.selection_fixture(21)
        profile = AIProfile.objects.create(base_url="https://ai.example.org", model="fixture")
        with patch("library.classification.verified_client"):
            group = queue.request_classification([b.id for b in bookmarks] + [outside.id], profile.id)
        jobs = list(Job.objects.filter(memberships__group=group).order_by("created_at"))
        Job.objects.filter(pk=jobs[0].id).update(state="succeeded")
        Job.objects.filter(pk=jobs[1].id).update(state="retry_wait", error_code="AI_TIMEOUT")
        status = queue.group_status(group)
        self.assertEqual((status["state"], status["completed"], status["total"]), ("queued", 1, 3))
        self.assertEqual((status["website_completed"], status["website_total"], status["succeeded_sites"]), (10, 22, 10))
        self.assertEqual((status["queued_sites"], status["retrying_sites"]), (2, 10))
        Job.objects.filter(pk=jobs[2].id).update(state="failed")
        status = queue.group_status(group)
        self.assertEqual((status["state"], status["website_completed"], status["failed_sites"]), ("retry_wait", 12, 2))

    def test_classification_retry_replaces_failed_group_member_and_deduplicates(self):
        bookmark = self.add()
        profile = AIProfile.objects.create(base_url="https://ai.example.org", model="fixture")
        with patch("library.classification.verified_client"):
            group = queue.request_classification([bookmark.id], profile.id)
        old = group.members.get().job
        Job.objects.filter(pk=old.id).update(state="failed", error_code="AI_INVALID_RESULT")
        first = self.request_json("post", f"jobs/{old.id}/retry")
        self.assertEqual(first.status_code, 202, first.content)
        new_id = first.json()["data"]["job_id"]
        again = self.request_json("post", f"jobs/{old.id}/retry")
        self.assertEqual(again.json()["data"]["job_id"], new_id)
        self.assertEqual(group.members.count(), 1)
        self.assertEqual(str(group.members.get().job_id), new_id)
        self.assertEqual(queue.group_status(group)["state"], "queued")
        Job.objects.filter(pk=new_id).update(state="succeeded", active_key=None)
        status = queue.group_status(group)
        self.assertEqual((status["state"], status["website_completed"], status["succeeded_sites"]), ("succeeded", 1, 1))

    def test_retry_wait_can_be_retried_immediately_without_duplicating_work(self):
        bookmark = self.add()
        group = JobGroup.objects.create(kind="classification", requested_count=1)
        job = queue.enqueue("classification", priority=50, payload={"bookmark_ids": [str(bookmark.id)]}, group=group)
        shared = JobGroup.objects.create(kind="classification", requested_count=1)
        JobGroupMember.objects.create(group=shared, job=job)
        job, attempt = queue.claim_next()
        queue.finish(job, attempt, error_code="AI_TIMEOUT", retryable=True)
        job.refresh_from_db()
        self.assertEqual(job.state, "retry_wait")
        self.assertGreater(job.available_at, timezone.now())
        response = self.request_json("post", f"jobs/{job.id}/retry")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["data"], {"job_id": str(job.id), "state": "queued"})
        job.refresh_from_db()
        self.assertLessEqual(job.available_at, timezone.now())
        self.assertEqual((job.error_code, job.priority, job.attempt_count, job.active_attempt_id), ("", 60, 1, None))
        repeated = self.request_json("post", f"jobs/{job.id}/retry")
        self.assertEqual(repeated.json()["data"]["job_id"], str(job.id))
        self.assertEqual(Job.objects.filter(kind="classification").count(), 1)
        self.assertEqual(JobGroupMember.objects.filter(job=job).count(), 2)
        queue.finish(job, attempt, result={"late": True})
        job.refresh_from_db()
        self.assertEqual(job.state, "queued")
        claimed, next_attempt = queue.claim_next()
        self.assertEqual((claimed.id, next_attempt.attempt_no), (job.id, 2))
        self.assertNotEqual(next_attempt.id, attempt.id)
        response = self.request_json("post", f"jobs/{job.id}/retry")
        self.assertEqual(response.status_code, 409)

    def test_retry_wait_revalidates_disabled_capture(self):
        bookmark = self.add(snapshot_enabled=True)
        job = queue.enqueue("capture", bookmark.id, "desktop", active_key=f"capture:{bookmark.id}:desktop:1")
        Job.objects.filter(pk=job.id).update(state="retry_wait", error_code="CAPTURE_TIMEOUT")
        Bookmark.objects.filter(pk=bookmark.id).update(snapshot_enabled=False)
        response = self.request_json("post", f"jobs/{job.id}/retry")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "SNAPSHOT_DISABLED")
        job.refresh_from_db()
        self.assertEqual(job.state, "retry_wait")

    def test_manual_refresh_promotes_and_expedites_existing_retry(self):
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id], source="scheduled", priority=10)
        job = Job.objects.filter(target_id=bookmark.id, variant="desktop").get()
        Job.objects.filter(pk=job.id).update(state="retry_wait", available_at=timezone.now()+timedelta(minutes=5))
        queue.request_refresh([bookmark.id], priority=60)
        job.refresh_from_db()
        self.assertEqual(job.priority, 60)
        self.assertLessEqual(job.available_at, timezone.now())
        Job.objects.filter(pk=job.id).update(available_at=timezone.now()+timedelta(minutes=5))
        queue.request_refresh([bookmark.id], priority=60)
        job.refresh_from_db()
        self.assertLessEqual(job.available_at, timezone.now())
        self.assertEqual(Job.objects.filter(kind="capture").count(), 2)

    def test_late_attempt_completion_cannot_overwrite_retry_wait(self):
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id])
        job, attempt = queue.claim_next()
        queue.finish(job, attempt, error_code="CAPTURE_TIMEOUT", retryable=True)
        queue.finish(job, attempt, {"stale": True})
        job.refresh_from_db()
        self.assertEqual(job.state, "retry_wait")
        self.assertEqual(job.result, {})

    def test_preview_and_cleanup_refuse_orphan_path_escape(self):
        from .artifacts import artifact_path
        from .worker import cleanup_artifacts
        digest = "a" * 64
        Artifact.objects.create(sha256=digest, relative_path="../../state.sqlite3", byte_size=1)
        with self.assertRaises(ApiError):
            artifact_path(digest, "../../state.sqlite3")
        with self.assertRaises(ApiError):
            cleanup_artifacts()

    def test_refresh_group_distinguishes_sites_from_device_tasks(self):
        bookmark = self.add(snapshot_enabled=True)
        group = queue.request_refresh([bookmark.id])
        jobs = list(Job.objects.filter(target_id=bookmark.id, kind="capture"))
        Job.objects.filter(pk=jobs[0].id).update(state="succeeded", active_key=None)
        Job.objects.filter(pk=jobs[1].id).update(state="failed", active_key=None)
        status = queue.group_status(group)
        self.assertEqual(status["completed"], 2)
        self.assertEqual(status["completed_sites"], 1)
        self.assertEqual(status["partial_sites"], 1)
        self.assertEqual(status["state"], "partial")

    def test_attempt_retry_has_new_uuid_and_cancelled_result_cannot_publish(self):
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id])
        job, attempt = queue.claim_next()
        self.assertTrue(queue.execution_valid(job, attempt))
        queue.finish(job, attempt, error_code="CAPTURE_TIMEOUT", retryable=True)
        Job.objects.filter(pk=job.id).update(available_at=timezone.now()-timedelta(seconds=1), priority=100)
        retried, new_attempt = queue.claim_next()
        self.assertEqual(retried.id, job.id)
        self.assertNotEqual(attempt.id, new_attempt.id)
        self.assertFalse(queue.execution_valid(job, attempt))
        Job.objects.filter(pk=job.id).update(state="cancelled", active_key=None)
        self.assertFalse(queue.execution_valid(retried, new_attempt))

    def test_two_databases_have_no_crossed_business_auth_tables(self):
        from django.db import connections
        self.assertNotIn("auth_user", connections["library"].introspection.table_names())
        self.assertNotIn("library_bookmark", connections["default"].introspection.table_names())

    def test_old_preview_is_invalid_after_library_epoch_changes(self):
        bookmark = self.add()
        preview = catalog.deletion_preview("bookmark", [bookmark.id])
        RuntimeState.objects.filter(pk=1).update(library_epoch=2)
        with self.assertRaises(ApiError) as raised:
            catalog.delete_catalog("bookmark", [bookmark.id], preview["preview_token"])
        self.assertEqual(raised.exception.code, "PREVIEW_STALE")

    def test_restore_generic_and_dedicated_queries_return_stable_confirmation(self):
        operation = MaintenanceOperation.objects.create(kind="restore", state="awaiting_confirmation", package_sha256="a"*64, expected_library_epoch=1, validation_expires_at=timezone.now()+timedelta(hours=1))
        first = self.client.get(f"/api/v1/maintenance-operations/{operation.id}").json()["data"]
        second = self.client.get(f"/api/v1/restores/{operation.id}").json()["data"]
        self.assertTrue(first["validation_token"])
        self.assertEqual(first["validation_token"], second["validation_token"])

    def test_failed_capture_exhaustion_does_not_immediately_schedule_again(self):
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id])
        job, attempt = queue.claim_next()
        Job.objects.filter(pk=job.id).update(attempt_count=3)
        queue.finish(job, attempt, error_code="CAPTURE_TIMEOUT", retryable=True)
        capture = CaptureState.objects.get(bookmark=bookmark, variant=job.variant)
        self.assertGreater(capture.next_due_at, timezone.now()+timedelta(days=6))
        self.assertIsNone(capture.last_success_at)
        queue.schedule_due()
        self.assertEqual(Job.objects.filter(kind="capture", variant=job.variant, state__in=queue.ACTIVE).count(), 0)

    def test_snapshot_publication_idempotency_and_current_protection(self):
        from .worker import publish_snapshot
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id])
        job, attempt = queue.claim_next()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            artifact = directory / "captured.html"
            artifact.write_text("<!doctype html><html><body>Frozen content</body></html>", encoding="utf-8")
            result = {"artifact_path": artifact, "viewport_width": 1366, "viewport_height": 768, "title": "Frozen", "final_url": bookmark.canonical_url}
            with patch("library.worker.generation_root", return_value=directory / "generation"), patch("library.artifacts.generation_root", return_value=directory / "generation"):
                version = publish_snapshot(job, attempt, result)
                self.assertEqual(publish_snapshot(job, attempt, result).id, version.id)
                self.assertEqual(SnapshotVersion.objects.count(), 1)
                job.refresh_from_db()
                self.assertEqual(job.state, "succeeded")
                response = self.request_json("post", "snapshots/delete-preview", {"ids": [str(version.id)]})
                self.assertEqual(response.status_code, 409)
                with patch("config.api.generation_root", return_value=directory / "generation"), patch("library.artifacts.generation_root", return_value=directory / "generation"):
                    response = self.client.get(f"/api/v1/snapshots/{version.id}/preview")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("sandbox", response["Content-Security-Policy"])
                    self.assertIn("script-src 'none'", response["Content-Security-Policy"])
                    self.assertEqual(response["Cache-Control"], "private, no-store")
                    response.close()
                    head = self.client.head(f"/api/v1/snapshots/{version.id}/preview")
                    self.assertEqual(head.status_code, 200)
                    self.assertIn("sandbox", head["Content-Security-Policy"])
                    self.assertEqual(head["Content-Type"], "text/html; charset=utf-8")
                    self.assertEqual(head["Cache-Control"], "private, no-store")
                    self.assertEqual(b"".join(head.streaming_content), b"")
                    head.close()
                    self.assertEqual(Client().head(f"/api/v1/snapshots/{version.id}/preview").status_code, 401)
                    self.assertEqual(self.client.head(f"/api/v1/snapshots/{uuid.uuid4()}/preview").status_code, 404)
                bookmark = catalog.update_bookmark(bookmark.id, {"revision": 1, "snapshot_enabled": False})
                self.assertEqual(bookmark.snapshots.count(), 1)
                self.assertEqual(CaptureState.objects.get(bookmark=bookmark, variant=job.variant).current_version_id, version.id)

    def test_current_input_change_prevents_old_artifact_publication(self):
        from .worker import publish_snapshot
        bookmark = self.add(snapshot_enabled=True)
        queue.request_refresh([bookmark.id])
        job, attempt = queue.claim_next()
        catalog.update_bookmark(bookmark.id, {"revision": 1, "snapshot_enabled": False})
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "captured.html"
            artifact.write_text("<html><body>Old request</body></html>", encoding="utf-8")
            with patch("library.worker.generation_root", return_value=Path(directory) / "generation"), patch("library.artifacts.generation_root", return_value=Path(directory) / "generation"):
                with self.assertRaises(ApiError):
                    publish_snapshot(job, attempt, {"artifact_path": artifact, "viewport_width": 1366, "viewport_height": 768})
        self.assertEqual(SnapshotVersion.objects.count(), 0)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ImportWorkflowTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        user = get_user_model().objects.create_user("owner", password="test-only-secret")
        self.client.force_login(user)

    def imported(self, count=1):
        from .importing import preview_import, start_import
        from .worker import run_one
        html = '<!DOCTYPE NETSCAPE-Bookmark-file-1><DL>' + ''.join(
            f'<DT><A HREF="https://example.org/{i}">Website {i}</A>' for i in range(count)) + '</DL>'
        batch = start_import(preview_import(html.encode()))
        run_one()
        batch.refresh_from_db()
        return batch

    def finish_captures(self, fail=False):
        while Job.objects.filter(kind="capture", state="queued").exists():
            job, attempt = queue.claim_next()
            self.assertEqual(job.kind, "capture")
            queue.finish(job, attempt, error_code="CAPTURE_NETWORK" if fail else None)

    def ai(self):
        from types import SimpleNamespace
        profile = AIProfile.objects.create(name="Test AI", base_url="https://example.org/v1", model="test")
        LibrarySettings.objects.filter(pk=1).update(active_ai_profile_id=profile.id)
        def complete(messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            if data.get("task") == "plan_taxonomy":
                return {"categories": [{"path": ["Reviewed"], "description": "All test websites"}]}
            rows = data["websites"]
            return {"items": [{"bookmark_id": row["bookmark_id"], "target": {"parent_folder_id": None, "new_segments": ["Reviewed"]}, "reason": "test suggestion"} for row in rows]}
        return patch("library.classification.verified_client", return_value=(profile, SimpleNamespace(complete=complete)))

    def test_upload_immediately_starts_and_duplicate_upload_key_is_idempotent(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        html = b'<DL><A HREF="https://example.org/">Example</A><A HREF="https://example.org/">Duplicate</A><A HREF="file:///private">Invalid</A></DL>'
        def upload():
            return self.client.post("/api/v1/imports", {"file": SimpleUploadedFile("bookmarks.html", html)}, HTTP_IDEMPOTENCY_KEY="import-test")
        first, second = upload(), upload()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(ImportBatch.objects.count(), 1)
        self.assertEqual(Job.objects.filter(kind="import").count(), 1)
        self.assertEqual(first.json()["data"]["duplicate"], 1)
        self.assertEqual(first.json()["data"]["invalid"], 1)

    def test_records_do_not_report_completion_or_enqueue_metadata_or_ai(self):
        from .importing import serialize_import
        batch = self.imported(2)
        result = serialize_import(batch)
        self.assertEqual((batch.state, batch.stage), ("running", "captures"))
        self.assertEqual(result["capture_total"], 4)
        self.assertEqual(result["progress"], 10)
        self.assertFalse(result["review_ready"])
        self.assertFalse(Job.objects.filter(kind__in=("classification", "metadata")).exists())

    def test_ai_waits_for_both_devices_and_terminal_retries(self):
        from .importing import advance_imports
        batch = self.imported()
        with self.ai():
            job, attempt = queue.claim_next()
            queue.finish(job, attempt, error_code="CAPTURE_NETWORK", retryable=True)
            job, attempt = queue.claim_next()
            queue.finish(job, attempt)
            advance_imports()
            self.assertFalse(Job.objects.filter(kind="classification").exists())
            Job.objects.filter(state="retry_wait").update(available_at=timezone.now()-timedelta(seconds=1))
            job, attempt = queue.claim_next()
            queue.finish(job, attempt, error_code="CAPTURE_NETWORK", retryable=False)
            advance_imports()
            batch.refresh_from_db()
            self.assertEqual(batch.stage, "classification")
            self.assertEqual(Job.objects.get(kind="classification").max_attempts, 1)

    def test_ai_unconfigured_produces_manual_review_with_failure_labels(self):
        from .importing import advance_imports, serialize_import
        batch = self.imported()
        self.finish_captures(fail=True)
        advance_imports()
        advance_imports()
        batch.refresh_from_db()
        result = serialize_import(batch)
        self.assertEqual((batch.stage, batch.ai_status, result["progress"]), ("review", "skipped", 100))
        self.assertEqual(result["capture_failed_sites"], 1)
        self.assertEqual(ClassificationProposal.objects.count(), 1)
        rows = self.client.get(f"/api/v1/classification/proposals?import_id={batch.id}").json()["data"]
        self.assertTrue(rows[0]["manual"])
        self.assertTrue(rows[0]["capture_failed"])
        self.assertEqual(Folder.objects.count(), 0)

    def test_completed_import_is_not_restarted_and_notification_persists(self):
        from .importing import advance_imports, start_import
        batch = self.imported()
        self.finish_captures()
        advance_imports()
        self.assertEqual(len(self.client.get("/api/v1/imports").json()["data"]), 1)
        response = self.client.post(f"/api/v1/imports/{batch.id}/review-notified", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/imports").json()["data"], [])
        start_import(batch)
        self.assertEqual(Job.objects.filter(kind="import").count(), 1)

    def test_successful_ai_is_scoped_and_moves_only_after_review(self):
        from .importing import advance_imports
        from .worker import run_one
        from .classification import accept_proposals
        batch = self.imported(11)
        self.finish_captures()
        with self.ai():
            advance_imports()
            advance_imports()
            self.assertEqual(Job.objects.filter(kind="classification").count(), 2)
            run_one()
            run_one()
        batch.refresh_from_db()
        self.assertEqual((batch.stage, batch.ai_status), ("review", "succeeded"))
        self.assertEqual(Folder.objects.count(), 0)
        rows = self.client.get(f"/api/v1/classification/proposals?import_id={batch.id}").json()["data"]
        self.assertEqual(len(rows), 11)
        self.assertTrue(all(not row["manual"] for row in rows))
        for row in rows:
            proposal = ClassificationProposal.objects.get(pk=row["id"])
            accept_proposals([{"id": row["id"], "revision": proposal.revision}], catalog.library_settings().tree_revision)
        self.assertEqual(Bookmark.objects.filter(folder__name="Reviewed").count(), 11)
        self.assertEqual(Folder.objects.count(), 1)

    def test_ai_failure_preserves_partial_results_and_skips_remaining_calls(self):
        from .importing import advance_imports
        from .worker import run_one
        from .ai_transport import AIError
        batch = self.imported(21)
        self.finish_captures()
        with self.ai():
            advance_imports()
            run_one()
            with patch("library.classification.generate_proposals", side_effect=AIError("AI_TIMEOUT")) as call:
                run_one()
                run_one()
                self.assertEqual(call.call_count, 1)
        batch.refresh_from_db()
        self.assertEqual((batch.stage, batch.ai_status), ("review", "partial"))
        self.assertEqual(ClassificationProposal.objects.filter(batch__profile__isnull=False).count(), 10)
        self.assertEqual(ClassificationProposal.objects.filter(batch__profile__isnull=True).count(), 11)
        self.assertFalse(Job.objects.filter(kind="classification", state__in=queue.ACTIVE).exists())

    def test_import_preserves_existing_records_and_settings(self):
        existing = catalog.create_bookmark({"url": "https://example.org/0", "snapshot_enabled": False, "notes": "keep", "refresh_interval_seconds": 7200})[0]
        batch = self.imported(2)
        existing.refresh_from_db()
        self.assertEqual((existing.notes, existing.refresh_interval_seconds, existing.snapshot_enabled), ("keep", 7200, False))
        self.assertEqual(batch.duplicate, 1)
        self.assertEqual(batch.entries.filter(status="imported").count(), 1)

    def test_empty_import_completes_without_calling_ai(self):
        from .importing import advance_imports
        batch = self.imported(0)
        advance_imports()
        batch.refresh_from_db()
        self.assertEqual(batch.stage, "review")
        self.assertEqual(ClassificationProposal.objects.count(), 0)

    def test_cancel_queued_import_does_not_leave_a_permanent_progress_bar(self):
        from .importing import preview_import, start_import
        batch = start_import(preview_import(b'<DL><A HREF="https://example.org/">Example</A></DL>'))
        job = Job.objects.get(kind="import")
        response = self.client.post(f"/api/v1/jobs/{job.id}/cancel", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        batch.refresh_from_db()
        self.assertEqual(batch.state, "cancelled")
        self.assertEqual(self.client.get("/api/v1/imports").json()["data"], [])

    def test_failed_capture_still_reaches_ai_with_url_and_source_reference(self):
        from .importing import advance_imports
        from .worker import run_one
        from types import SimpleNamespace
        batch = self.imported()
        BookmarkSource.objects.filter(import_batch=batch).update(source_folder_path=["Browser", "Tools"])
        self.finish_captures(fail=True)
        profile = AIProfile.objects.create(name="AI", base_url="https://example.org/v1", model="test")
        LibrarySettings.objects.filter(pk=1).update(active_ai_profile_id=profile.id)
        def complete(messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            if data.get("task") == "plan_taxonomy":
                return {"categories": [{"path": ["Tools"], "description": "Public tools"}]}
            row = data["websites"][0]
            self.assertTrue(row["capture_failed"])
            self.assertFalse(row["metadata_available"])
            self.assertEqual(row["source_folder_paths"], [["Browser", "Tools"]])
            return {"items": [{"bookmark_id": row["bookmark_id"], "target": {"parent_folder_id": None, "new_segments": ["Tools"]}, "reason": "URL and source folder"}]}
        with patch("library.classification.verified_client", return_value=(profile, SimpleNamespace(complete=complete))):
            advance_imports()
            run_one()
        batch.refresh_from_db()
        self.assertEqual(batch.ai_status, "succeeded")
        self.assertIsNotNone(ClassificationProposal.objects.get().batch.profile_id)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TaxonomyTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)

    def test_batches_share_a_durable_plan_and_cannot_invent_other_paths(self):
        from types import SimpleNamespace
        from .classification import generate_proposals
        from .ai_transport import AIError
        sites = [catalog.create_bookmark({"url": f"https://art.example/{i}", "title": f"CG art {i}"}, schedule_initial=False)[0] for i in range(21)]
        profile = AIProfile.objects.create(base_url="https://example.org/v1", model="test")
        calls = []
        drift = False
        def complete(messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            calls.append(data)
            if data.get("task") == "plan_taxonomy":
                self.assertEqual(len(data["library_websites"]), 21)
                return {"categories": [{"path": ["设计与创作", "三维美术"], "description": "CG、Blender、建模与动画"}]}
            target = {"parent_folder_id": None, "new_segments": ["CG & Visual", "Blender社区"]} if drift else data["categories"][0]["target"]
            return {"items": [{"bookmark_id": r["bookmark_id"], "target": target, "reason": "CG art"} for r in data["websites"]]}
        with patch("library.classification.verified_client", return_value=(profile, SimpleNamespace(complete=complete))):
            generate_proposals([s.id for s in sites[:10]])
            generate_proposals([s.id for s in sites[10:20]])
            self.assertEqual(sum(c.get("task") == "plan_taxonomy" for c in calls), 1)
            self.assertEqual(ClassificationProposal.objects.filter(status="pending").count(), 20)
            self.assertEqual(Folder.objects.count(), 0)
            drift = True
            with self.assertRaises(AIError):
                generate_proposals([sites[20].id])
            self.assertEqual(ClassificationProposal.objects.filter(status="pending").count(), 20)

    def test_plan_rejects_duplicate_names_deep_paths_and_excessive_categories(self):
        from .taxonomy import validate_plan
        from .ai_transport import AIError
        cases = [
            [["CG与美术"], ["CG 与美术"]],
            [["设计", "三维", "Blender"]],
            [[str(i)] for i in range(9)],
            [["艺术", str(i)] for i in range(25)],
        ]
        for paths in cases:
            with self.subTest(paths=paths), self.assertRaises(AIError):
                validate_plan({"categories": [{"path": p, "description": "scope"} for p in paths]}, 482)

    def test_new_full_library_run_supersedes_old_work_and_late_results(self):
        from types import SimpleNamespace
        site = catalog.create_bookmark({"url": "https://example.org"}, schedule_initial=False)[0]
        profile = AIProfile.objects.create(base_url="https://example.org/v1", model="test")
        with patch("library.classification.verified_client", return_value=(profile, SimpleNamespace())):
            old_group = queue.request_classification([site.id], profile.id)
            job, attempt = queue.claim_next()
            batch = ClassificationBatch.objects.create(profile=profile, job_group=old_group, tree_revision=1)
            proposal = ClassificationProposal.objects.create(batch=batch, bookmark=site,
                expected_classification_revision=1, expected_tree_revision=1)
            new_group = queue.request_classification([site.id], profile.id)
        job.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(proposal.status, "stale")
        self.assertFalse(queue.execution_valid(job, attempt))
        self.assertEqual(new_group.members.count(), 1)
        self.assertEqual(Job.objects.filter(kind="classification", state__in=queue.ACTIVE).count(), 1)

    def test_accept_only_prunes_proven_ai_empty_ancestors(self):
        from .classification import accept_proposals
        ai_root = Folder.objects.create(name="CG 与美术", name_key="cg 与美术", created_by_ai=True)
        ai_child = Folder.objects.create(name="Blender社区", name_key="blender社区", parent=ai_root, created_by_ai=True)
        manual = Folder.objects.create(name="人工目录", name_key="人工目录")
        protected = Folder.objects.create(name="其他AI目录", name_key="其他ai目录", created_by_ai=True)
        profile = AIProfile.objects.create(base_url="https://example.org/v1", model="test")
        batch = ClassificationBatch.objects.create(profile=profile, tree_revision=1)
        sites = [catalog.create_bookmark({"url": f"https://example.org/{i}", "folder_id": f.id}, schedule_initial=False)[0] for i, f in enumerate([ai_child, manual, protected])]
        proposals = [ClassificationProposal.objects.create(batch=batch, bookmark=s,
            expected_classification_revision=s.classification_revision, expected_tree_revision=1,
            target_path={"parent_folder_id": None, "new_segments": ["设计与创作", "三维美术"]}) for s in sites]
        result = accept_proposals([{"id": str(p.id), "revision": p.revision} for p in proposals[:2]], 1)
        self.assertEqual(set(result["removed_folder_ids"]), {str(ai_root.id), str(ai_child.id)})
        self.assertTrue(Folder.objects.filter(pk=manual.id).exists())
        self.assertTrue(Folder.objects.filter(pk=protected.id).exists())
        self.assertEqual(Bookmark.objects.count(), 3)
        self.assertEqual(Bookmark.objects.filter(folder__name="三维美术").count(), 2)
        proposals[2].refresh_from_db()
        self.assertEqual(proposals[2].expected_tree_revision, result["tree_revision"])


@override_settings(BOOKMARK_AUTH_DEBUG=True,
    AUTHENTICATION_BACKENDS=["axes.backends.AxesStandaloneBackend", "django.contrib.auth.backends.ModelBackend", "runtime.authentication.PublicKeyBackend"],
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        get_user_model().objects.create_user("owner", password="test-only-secret")
        self.client = Client(enforce_csrf_checks=True)

    def test_csrf_password_only_login_and_protected_api(self):
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/session/login", data='{"password":"test-only-secret"}', content_type="application/json").status_code, 403)
        token = self.client.get("/api/v1/session").json()["data"]["csrf_token"]
        response = self.client.post("/api/v1/session/login", data='{"password":"test-only-secret"}', content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        new_token = self.client.get("/api/v1/session").json()["data"]["csrf_token"]
        self.assertNotEqual(new_token, token)
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)
        self.assertIn("HttpOnly", str(response.cookies["sessionid"]))
        self.client.post("/api/v1/session/logout", data="{}", content_type="application/json", HTTP_X_CSRFTOKEN=new_token)
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)

    def test_ip_lockout_cannot_be_bypassed_by_forwarded_header(self):
        token = self.client.get("/api/v1/session").json()["data"]["csrf_token"]
        for i in range(5):
            response = self.client.post("/api/v1/session/login", data='{"password":"wrong"}', content_type="application/json", HTTP_X_CSRFTOKEN=token, HTTP_X_FORWARDED_FOR=f"8.8.8.{i}")
        self.assertEqual(response.status_code, 429)
        response = self.client.post("/api/v1/session/login", data='{"password":"test-only-secret"}', content_type="application/json", HTTP_X_CSRFTOKEN=token, HTTP_X_FORWARDED_FOR="1.1.1.1")
        self.assertEqual(response.status_code, 429)
