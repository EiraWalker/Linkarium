"""Selected snapshot refresh through the browser and real API on the isolated library."""
import os
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
from library.models import Bookmark, Job, JobGroup
from playwright.sync_api import expect, sync_playwright


def main():
    assert DATA_ROOT.resolve() == TEST_DATA.resolve(), "Only use the isolated test library"
    stamp = f"SnapshotSelection{time.time_ns()}"
    sites = Bookmark.objects.bulk_create([
        Bookmark(original_url=f"https://selection.example.org/{stamp}/{n}",
                 canonical_url=f"https://selection.example.org/{stamp}/{n}",
                 domain="selection.example.org", source_title=f"{stamp} {n}",
                 snapshot_enabled=n == 0)
        for n in range(121)
    ])
    group_id = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.goto("http://127.0.0.1:18767/library")
            page.get_by_role("button", name="返回登录").click()
            page.get_by_label("调试密码").fill((TEST_DATA / "runtime/acceptance-password").read_text())
            page.get_by_role("button", name="密码登录").click()
            page.get_by_label("搜索网站").fill(stamp)
            expect(page.get_by_text("121 个网站", exact=True)).to_be_visible()
            page.get_by_role("checkbox", name="全选", exact=True).click()
            expect(page.get_by_text("已选择 121 项", exact=True)).to_be_visible()
            page.get_by_role("button", name="下一页", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/bookmarks/refresh")) as pending:
                page.get_by_role("button", name="抓取快照", exact=True).click()
            response = pending.value
            assert response.status == 202, response.text()
            assert set(response.request.post_data_json["bookmark_ids"]) == {str(site.id) for site in sites}
            group = response.json()["data"]
            group_id = group["group_id"]
            assert group["requested_count"] == 121, group
            assert group["skipped"] == 120, group
            assert group["source"] == "selection", group
            assert {job["bookmark_id"] for job in group["jobs"]} == {str(sites[0].id)}, group
            assert {job["variant"] for job in group["jobs"]} == {"desktop", "mobile"}, group
            expect(page.get_by_text("已选择 121 项", exact=True)).to_be_visible()
            tasks = page.get_by_role("dialog", name="后台任务", exact=True)
            expect(tasks).to_be_visible()
            expect(tasks.get_by_role("option", name="抓取快照", exact=False)).to_have_count(1)
            tasks.get_by_role("button", name="关闭", exact=True).click()
            page.set_viewport_size({"width": 390, "height": 844})
            refresh_button = page.get_by_role("button", name="抓取快照", exact=True)
            expect(refresh_button).to_be_visible()
            delayed = []
            page.route("**/api/v1/bookmarks/refresh", lambda route: delayed.append(route))
            refresh_button.click()
            expect(refresh_button).to_be_disabled()
            assert len(delayed) == 1
            expect(page.get_by_text("已选择 121 项", exact=True)).to_be_visible()
            delayed[0].fulfill(status=503, content_type="application/json",
                               body='{"error":{"code":"SERVICE_UNAVAILABLE","message":"模拟提交失败"}}')
            expect(refresh_button).to_be_enabled()
            expect(page.get_by_text("已选择 121 项", exact=True)).to_be_visible()
            expect(page.get_by_text("模拟提交失败", exact=True)).to_be_visible()
            browser.close()
            print("Selected snapshot browser acceptance passed (cross-page, pending, failure, mobile).")
    finally:
        if group_id:
            JobGroup.objects.filter(pk=group_id).delete()
        Job.objects.filter(target_id__in=[site.id for site in sites], kind="capture").delete()
        Bookmark.objects.filter(pk__in=[site.id for site in sites]).delete()


if __name__ == "__main__":
    main()
