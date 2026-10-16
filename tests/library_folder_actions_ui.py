"""All-sites folder actions through real HTTP, using only the isolated test library."""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = ROOT / "output" / "import-workflow-data"
os.environ["BOOKMARK_DATA_ROOT"] = str(TEST_DATA)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools" / "browsers")
sys.path.insert(0, str(ROOT / "backend"))
import django
django.setup()
from config.paths import DATA_ROOT
from library.models import Bookmark, Folder, LibrarySettings
from playwright.sync_api import sync_playwright, expect


def main():
    assert DATA_ROOT.resolve() == TEST_DATA.resolve()
    name = f"FolderAction{time.time_ns()}"
    folder = Folder.objects.create(name=name, name_key=name.lower())
    child = Folder.objects.create(name="Child", name_key="child", parent=folder)
    sites = Bookmark.objects.bulk_create([
        Bookmark(original_url=f"https://example.org/{name}/{n}", canonical_url=f"https://example.org/{name}/{n}",
                 domain="example.org", source_title=f"{name} {n}", folder=child if n == 0 else None,
                 snapshot_enabled=n < 2) for n in range(3)
    ])
    settings = LibrarySettings.objects.get(pk=1)
    previous_profile = settings.active_ai_profile_id
    LibrarySettings.objects.filter(pk=1).update(active_ai_profile_id=None)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 933, "height": 828})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://127.0.0.1:18767/library")
            page.locator('input[type="password"]').fill((TEST_DATA / "runtime/acceptance-password").read_text())
            page.get_by_role("button", name="进入收藏库").click()
            refresh = page.get_by_role("button", name="刷新文件夹", exact=True)
            ai = page.get_by_role("button", name="AI分类", exact=True)
            expect(refresh).to_be_enabled()
            expect(ai).to_be_enabled()
            assert page.get_by_role("button", name="AI 分类文件夹", exact=True).count() == 0
            with page.expect_response(lambda r: r.url.endswith("/folders/all/refresh-preview")) as pending:
                refresh.click()
            preview = pending.value.json()["data"]
            total = page.evaluate("async () => (await (await fetch('/api/v1/bookmarks')).json()).page.total")
            assert preview["total"] == total
            dialog = page.get_by_role("dialog", name="刷新文件夹", exact=True)
            expect(dialog.get_by_text("将刷新全部网站中的", exact=False)).to_be_visible()
            with page.expect_response(lambda r: r.url.endswith("/folders/all/refresh")) as pending:
                dialog.get_by_role("button", name="立即刷新", exact=True).click()
            response = pending.value
            assert response.status == 202
            group = response.json()["data"]
            assert group["requested_count"] == total
            assert group["total"] == preview["eligible"] * 2
            assert {str(sites[0].id), str(sites[1].id)} <= {j["bookmark_id"] for j in group["jobs"]}
            assert str(sites[2].id) not in {j["bookmark_id"] for j in group["jobs"]}
            expect(dialog).not_to_be_visible()
            tasks = page.get_by_role("dialog", name="后台任务", exact=True)
            expect(tasks).to_be_visible()
            tasks.get_by_role("button", name="关闭", exact=True).click()
            with page.expect_response(lambda r: r.url.endswith("/classification/batches")) as pending:
                ai.click()
            response = pending.value
            assert response.request.post_data_json["folder_id"] == "all"
            assert response.json()["error"]["code"] == "AI_UNCONFIGURED"
            expect(ai).to_be_enabled()
            page.goto(f"http://127.0.0.1:18767/library?folder={folder.id}")
            expect(refresh).to_be_enabled()
            expect(ai).to_be_enabled()
            with page.expect_response(lambda r: r.url.endswith(f"/folders/{folder.id}/refresh-preview")) as pending:
                refresh.click()
            assert pending.value.json()["data"]["total"] == 1
            dialog.get_by_role("button", name="取消", exact=True).click()
            with page.expect_response(lambda r: r.url.endswith("/classification/batches")) as pending:
                ai.click()
            assert pending.value.request.post_data_json["folder_id"] == str(folder.id)
            assert not errors, errors
            report = {"all_sites_buttons_enabled": True, "ai_label": "AI分类",
                      "all_sites_refresh": True, "skip_snapshot_disabled": True,
                      "all_sites_ai_request": True, "ai_unconfigured_feedback": True,
                      "folder_scope_preserved": True, "page_errors": errors}
            (ROOT / "output/playwright/library-folder-actions-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
            print("All-sites folder actions browser acceptance passed.")
    finally:
        LibrarySettings.objects.filter(pk=1).update(active_ai_profile_id=previous_profile)
        Bookmark.objects.filter(pk__in=[b.id for b in sites]).delete()
        folder.delete()


if __name__ == "__main__":
    main()
