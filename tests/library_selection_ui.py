"""Cross-page selection acceptance against the isolated workflow server (18767)."""
import json
import os
import re
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = ROOT / "output" / "import-workflow-data"
os.environ["BOOKMARK_DATA_ROOT"] = str(TEST_DATA)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools" / "browsers")
sys.path.insert(0, str(ROOT / "backend"))
import django
django.setup()
from config.paths import DATA_ROOT
from library.models import Bookmark, BookmarkTag, Folder, Tag
from playwright.sync_api import sync_playwright, expect


def main():
    assert DATA_ROOT.resolve() == TEST_DATA.resolve(), "Only use the isolated test library"
    stamp = f"SelectionFixture{time.time_ns()}"
    folder = Folder.objects.create(name=stamp, name_key=stamp.lower())
    tag = Tag.objects.create(name=stamp, name_key=stamp.lower())
    sites = Bookmark.objects.bulk_create([
        Bookmark(original_url=f"https://selection.example.org/{stamp}/{n}",
                 canonical_url=f"https://selection.example.org/{stamp}/{n}",
                 domain="selection.example.org", source_title=f"{stamp} {n}",
                 folder=folder if n < 61 else None, snapshot_enabled=False)
        for n in range(121)
    ])
    outside_url = f"https://outside.example.org/{uuid.uuid4()}"
    outside = Bookmark.objects.create(original_url=outside_url,
        canonical_url=outside_url, domain="outside.example.org", snapshot_enabled=False)
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://127.0.0.1:18767")
            page.locator('input[type="password"]').fill((TEST_DATA / "runtime/acceptance-password").read_text())
            page.get_by_role("button", name="进入收藏库").click()
            identity = str(uuid.uuid4())
            group = {"id": identity, "group_id": identity, "kind": "classification", "state": "queued",
                     "requested_count": 121, "website_total": 121, "website_completed": 0,
                     "total": 13, "completed": 0, "queued_sites": 121, "running_sites": 0,
                     "retrying_sites": 0, "succeeded_sites": 0, "failed_sites": 0,
                     "counts": {"queued": 13}, "jobs": []}
            page.route("**/api/v1/classification/batches", lambda route: route.fulfill(status=202, json={"data": group}))
            page.route(f"**/api/v1/job-groups/{identity}*", lambda route: route.fulfill(json={"data": group}))
            def classify_selection(expected_ids):
                ai = page.get_by_role("button", name="AI分类", exact=True)
                expect(ai).to_have_count(1)
                expect(ai).to_be_enabled()
                with page.expect_response(lambda r: r.url.endswith("/classification/batches")) as pending:
                    ai.click()
                body = pending.value.request.post_data_json
                assert set(body["bookmark_ids"]) == expected_ids, body
                assert "folder_id" not in body, body
                tasks = page.get_by_role("dialog", name="后台任务", exact=True)
                expect(tasks).to_be_visible()
                expect(tasks.get_by_text("网站已处理 0 / 121", exact=False)).to_be_visible()
                tasks.get_by_role("button", name="关闭", exact=True).click()
            all_box = page.get_by_role("checkbox", name="全选", exact=True)
            expect(all_box).to_be_enabled()
            total = page.evaluate("async () => (await (await fetch('/api/v1/bookmarks')).json()).page.total")
            all_box.click()
            expect(page.get_by_text(f"已选择 {total} 项", exact=True)).to_be_visible()
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(all_box).to_be_enabled()
            expect(all_box).to_be_checked()
            cards = page.get_by_role("checkbox", name=re.compile(r"^选择 "))
            expect(cards).to_have_count(48)
            assert cards.evaluate_all("els => els.every(el => el.checked)")
            cards.first.uncheck()
            expect(page.get_by_text(f"已选择 {total-1} 项", exact=True)).to_be_visible()
            assert all_box.evaluate("el => el.indeterminate")
            all_box.click()
            expect(page.get_by_text(f"已选择 {total} 项", exact=True)).to_be_visible()
            all_box.click()
            expect(page.get_by_role("button", name="取消选择", exact=True)).not_to_be_visible()
            all_box.click()
            expect(page.get_by_text(f"已选择 {total} 项", exact=True)).to_be_visible()
            page.get_by_label("搜索网站", exact=True).fill(stamp)
            expect(cards).to_have_count(48)
            expect(all_box).not_to_be_checked()
            expect(page.get_by_role("button", name="取消选择", exact=True)).not_to_be_visible()
            all_box.click()
            expect(page.get_by_text("已选择 121 项", exact=True)).to_be_visible()
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(all_box).to_be_enabled()
            expect(all_box).to_be_checked()
            classify_selection({str(site.id) for site in sites})
            assert page.evaluate("async id => (await (await fetch('/api/v1/bookmarks?scope=folder&folder_id='+id)).json()).page.total", str(folder.id)) == 61
            page.get_by_role("button", name="移动", exact=True).click()
            dialog = page.get_by_role("dialog")
            expect(dialog.get_by_text("已选择 121 个网站。", exact=True)).to_be_visible()
            dialog.get_by_label("目标文件夹").select_option(str(folder.id))
            dialog.get_by_role("button", name="确认", exact=True).click()
            expect(dialog).not_to_be_visible()
            assert page.evaluate("async id => (await (await fetch('/api/v1/bookmarks?scope=folder&folder_id='+id)).json()).page.total", str(folder.id)) == 121
            assert page.evaluate("async id => (await (await fetch('/api/v1/bookmarks/'+id)).json()).data.folder_id", str(outside.id)) is None
            expect(all_box).to_be_enabled()
            all_box.click()
            expect(page.get_by_text("已选择 121 项", exact=True)).to_be_visible()
            classify_selection({str(site.id) for site in sites})
            page.get_by_role("button", name="添加标签", exact=True).click()
            dialog.get_by_role("checkbox", name=stamp, exact=True).check()
            dialog.get_by_role("button", name="确认", exact=True).click()
            expect(dialog).not_to_be_visible()
            assert page.evaluate("async id => (await (await fetch('/api/v1/bookmarks?tag_ids='+id)).json()).page.total", str(tag.id)) == 121
            assert page.evaluate("async id => (await (await fetch('/api/v1/bookmarks/'+id)).json()).data.tags", str(outside.id)) == []
            assert not errors, errors
            output = ROOT / "output" / "playwright"
            output.mkdir(parents=True, exist_ok=True)
            (output / "library-selection-report.json").write_text(json.dumps({
                "all_pages": True, "pagination_preserves_selection": True,
                "indeterminate_and_clear": True, "search_scope": True,
                "bulk_move_121": True, "bulk_tag_121": True, "page_errors": errors,
                "mixed_classified_and_unclassified_ai": True, "already_classified_ai": True,
                "ai_selection_covers_all_pages": True, "ai_opens_existing_task_dialog": True
            }, indent=2), encoding="utf-8")
            browser.close()
            print("Cross-page selection browser acceptance passed (121 filtered sites).")
    finally:
        Bookmark.objects.filter(pk__in=[b.id for b in sites] + [outside.id]).delete()
        folder.delete()
        tag.delete()


if __name__ == "__main__":
    main()
