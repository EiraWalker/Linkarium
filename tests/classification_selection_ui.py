"""Real all-page review approval in an isolated library; no external AI calls."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = ROOT / "output/classification-selection-data"
tempfile.tempdir = str(ROOT / "output/test-temp")
os.environ["TEMP"] = os.environ["TMP"] = tempfile.tempdir
os.environ["BOOKMARK_DATA_ROOT"] = str(TEST_DATA)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools/browsers")
sys.path.insert(0, str(ROOT / "backend"))
import django
django.setup()
from config.paths import DATA_ROOT
from library.models import Bookmark, ClassificationBatch, ClassificationProposal, Folder, JobGroup, LibrarySettings
from playwright.sync_api import sync_playwright, expect


def database_call(fn):
    # Playwright's synchronous facade owns an event loop in this thread.
    def run():
        from django.db import connections
        try:
            return fn()
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run).result()


def main():
    assert DATA_ROOT.resolve() == TEST_DATA.resolve()
    stamp = f"AllPages{time.time_ns()}"
    revision = LibrarySettings.objects.get(pk=1).tree_revision
    group = JobGroup.objects.create(kind="classification", requested_count=121)
    batch = ClassificationBatch.objects.create(job_group=group, tree_revision=revision)
    sites = Bookmark.objects.bulk_create([Bookmark(original_url=f"https://review.example.org/{stamp}/{n}",
        canonical_url=f"https://review.example.org/{stamp}/{n}", domain="review.example.org",
        source_title=f"Review fixture {n}", snapshot_enabled=False) for n in range(122)])
    proposals = ClassificationProposal.objects.bulk_create([ClassificationProposal(batch=batch, bookmark=site,
        expected_classification_revision=1, expected_tree_revision=revision,
        target_path={"parent_folder_id": None, "new_segments": [stamp, "Reviewed"]}) for site in sites[:121]])
    stale = ClassificationProposal.objects.create(batch=batch, bookmark=sites[0],
        expected_classification_revision=1, expected_tree_revision=revision, status="stale")
    outside_batch = ClassificationBatch.objects.create(tree_revision=revision)
    outside = ClassificationProposal.objects.create(batch=outside_batch, bookmark=sites[-1],
        expected_classification_revision=1, expected_tree_revision=revision, status="stale")
    errors, requests = [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 960})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.post_data_json) if request.url.endswith("/classification/accept") else None)
            page.goto("http://127.0.0.1:18767/library")
            page.locator('input[type="password"]').fill((TEST_DATA / "runtime/acceptance-password").read_text())
            page.get_by_role("button", name="进入收藏库", exact=True).click()
            page.get_by_role("button", name="后台任务", exact=True).click()
            tasks = page.get_by_role("dialog", name="后台任务", exact=True)
            expect(tasks.get_by_label("任务批次").locator(f'option[value="{group.id}"]')).to_have_count(1)
            tasks.get_by_label("任务批次").select_option(str(group.id))
            tasks.get_by_role("button", name=re.compile(r"^校对分类")).click()
            review = page.get_by_role("dialog", name="分类校对", exact=True)
            choices = review.get_by_role("checkbox", name=re.compile(r"^选择建议 "))
            all_box = review.get_by_role("checkbox", name="全选所有页", exact=True)
            current = review.get_by_role("checkbox", name="选择当前页", exact=True)
            expect(choices).to_have_count(48)
            current.click()
            expect(review.get_by_text("已选 48 项", exact=True)).to_be_visible()
            assert all_box.evaluate("el => el.indeterminate")
            all_box.click()
            expect(review.get_by_text("已选 121 项", exact=True)).to_be_visible()
            expect(all_box).to_be_checked()
            review.get_by_role("button", name="下一页", exact=True).click()
            expect(current).to_be_checked()
            expect(all_box).to_be_checked()
            assert choices.evaluate_all("els => els.every(el => el.checked)")
            choices.first.click()
            expect(review.get_by_text("已选 120 项", exact=True)).to_be_visible()
            assert all_box.evaluate("el => el.indeterminate")
            all_box.click()
            expect(review.get_by_text("已选 121 项", exact=True)).to_be_visible()
            all_box.click()
            expect(review.get_by_text("已选 121 项", exact=True)).not_to_be_visible()
            expect(review.get_by_role("button", name="全部批准", exact=True)).not_to_be_visible()
            all_box.click()
            review.get_by_label("分类状态").select_option("stale")
            expect(all_box).to_be_disabled()
            expect(all_box).not_to_be_checked()
            review.get_by_label("分类状态").select_option("pending")
            expect(choices).to_have_count(48)
            expect(all_box).not_to_be_checked()
            all_box.click()
            expect(review.get_by_text("已选 121 项", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            output = ROOT / "output/playwright"
            output.mkdir(exist_ok=True, parents=True)
            page.screenshot(path=str(output / "classification-selection-mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 960})
            # A stale suggestion on a page never visited must block the entire
            # approval. The server cannot commit only the visible first page.
            database_call(lambda: Bookmark.objects.filter(pk=sites[120].id).update(classification_revision=2))
            review.get_by_role("button", name="全部批准", exact=True).click()
            expect(review.get_by_text("部分方案已失效，请重新校对", exact=True)).to_be_visible()
            assert not database_call(lambda: ClassificationProposal.objects.filter(batch=batch, status="accepted").exists())
            assert not database_call(lambda: Folder.objects.filter(name=stamp).exists())
            assert not database_call(lambda: Bookmark.objects.filter(pk__in=[site.id for site in sites]).exclude(folder_id=None).exists())
            database_call(lambda: Bookmark.objects.filter(pk=sites[120].id).update(classification_revision=1))
            review.get_by_role("button", name="刷新列表", exact=True).click()
            all_box.click()
            expect(review.get_by_text("已选 121 项", exact=True)).to_be_visible()
            review.get_by_role("button", name="全部批准", exact=True).click()
            expect(review.get_by_text("暂无待确认的分类建议", exact=True)).to_be_visible(timeout=10000)
            assert database_call(lambda: ClassificationProposal.objects.filter(batch=batch, status="accepted").count()) == 121
            assert database_call(lambda: Bookmark.objects.filter(pk__in=[site.id for site in sites[:121]], folder__name="Reviewed").count()) == 121
            database_call(lambda: (sites[-1].refresh_from_db(), outside.refresh_from_db()))
            assert sites[-1].folder_id is None and outside.status == "stale"
            assert len(requests) == 2 and all(len(body["items"]) == 121 for body in requests)
            assert not errors, errors
            report = {"all_pages_121": True, "selection_preserved_between_pages": True,
                      "indeterminate_and_deselect": True, "status_change_clears_selection": True,
                      "stale_unvisited_page_blocks_everything": True, "single_atomic_approval": True,
                      "outside_scope_unchanged": True, "mobile_overflow": False, "page_errors": errors}
            (output / "classification-selection-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            browser.close()
            print(json.dumps(report))
    finally:
        Bookmark.objects.filter(pk__in=[site.id for site in sites]).delete()
        ClassificationBatch.objects.filter(pk__in=[batch.id, outside_batch.id]).delete()
        group.delete()
        Folder.objects.filter(name=stamp, parent_id=None).delete()


if __name__ == "__main__":
    main()
