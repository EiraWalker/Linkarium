"""Read-only live UI probe; opens existing results without submitting AI work.

Creates one temporary diagnostic owner session using local administrator access,
deletes that session afterwards, and never reads or changes the owner password.
"""
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
tempfile.tempdir = str(ROOT / "output/test-temp")
os.environ["TEMP"] = os.environ["TMP"] = tempfile.tempdir
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools/browsers")
sys.path.insert(0, str(ROOT / "backend"))
import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
import httpx
from playwright.sync_api import sync_playwright, expect


def open_tasks(page):
    expect(page.locator('header').get_by_role("button", name="后台任务", exact=True)).to_have_count(0)
    button = page.get_by_role("button", name="后台任务", exact=True)
    expect(button).to_be_visible()
    button.click()


def assert_fixed_task_entry(page):
    entry = page.get_by_role("button", name="后台任务", exact=True)
    expect(entry).to_have_count(1)
    expect(entry).to_be_visible()
    expect(entry).not_to_contain_text("连接进度", timeout=10000)
    expect(page.get_by_role("button", name=re.compile(r"^查看 ")).first).to_be_visible(timeout=10000)
    viewport = page.viewport_size
    before = entry.bounding_box()
    assert before and before["x"] >= 0 and before["y"] >= 0
    assert abs(viewport["width"] - before["x"] - before["width"] - 16) < 2
    assert abs(viewport["height"] - before["y"] - before["height"] - 16) < 2
    page.locator("aside").evaluate("node => { node.scrollTop = node.scrollHeight; }")
    page.locator("main").first.evaluate("node => { node.scrollTop = node.scrollHeight; }")
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    after = entry.bounding_box()
    assert all(abs(after[key] - before[key]) < 2 for key in ["x", "y", "width", "height"])
    page.locator("main").first.evaluate("node => { node.scrollTop = 0; }")
    page.evaluate("window.scrollTo(0, 0)")


def main():
    group_id = sys.argv[1]
    owner = get_user_model().objects.get(username="owner")
    store = importlib.import_module(settings.SESSION_ENGINE).SessionStore()
    store["_auth_user_id"] = str(owner.pk)
    store["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    store["_auth_user_hash"] = owner.get_session_auth_hash()
    store.set_expiry(180)
    store.save()
    try:
        with httpx.Client(base_url="http://127.0.0.1:8001", trust_env=False,
                          cookies={settings.SESSION_COOKIE_NAME: store.session_key}) as client:
            response = client.get(f"/api/v1/job-groups/{group_id}")
            response.raise_for_status()
            group = response.json()["data"]
            assert group["succeeded_sites"] > 0
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 933, "height": 828})
            context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": store.session_key,
                                 "url": "http://127.0.0.1:8001", "httpOnly": True}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://127.0.0.1:8001/library")
            expect(page.get_by_role("heading", name="全部网站", exact=True)).to_be_visible()
            assert_fixed_task_entry(page)
            page.screenshot(path=str(ROOT / "output/playwright/task-dock-desktop.png"))
            open_tasks(page)
            progress = page.get_by_role("dialog", name="后台任务", exact=True)
            expect(progress.get_by_label("任务批次").locator(f'option[value="{group_id}"]')).to_have_count(1)
            progress.get_by_label("任务批次").select_option(group_id)
            expect(progress.get_by_text(f"/ {group['website_total']}", exact=False).first).to_be_visible()
            expect(progress.get_by_text("批次", exact=False).first).to_be_visible()
            output = ROOT / "output/playwright"
            output.mkdir(parents=True, exist_ok=True)
            progress.screenshot(path=str(output / "classification-live-progress.png"))
            # Reload must reconnect to the persisted group without another AI
            # submission. This assertion reproduces lost task tracking.
            with page.expect_response(lambda response: f"/api/v1/job-groups/{group_id}" in response.url, timeout=10000):
                page.reload()
            expect(page.get_by_role("button", name="后台任务", exact=True)).to_be_visible()
            open_tasks(page)
            expect(progress).to_be_visible()
            expect(progress.get_by_role("button", name="查看建议", exact=True)).to_have_count(0)
            expect(progress.get_by_text(re.compile(r"本组待校对"))).to_have_count(0)
            progress.get_by_role("button", name=re.compile(r"^校对分类")).click()
            review = page.get_by_role("dialog", name="分类校对", exact=True)
            expect(review).to_be_visible()
            choices = review.get_by_role("checkbox", name=re.compile(r"^选择建议 "))
            expect(choices.first).to_be_visible(timeout=10000)
            visible_proposals = choices.count()
            assert visible_proposals > 0
            with page.expect_response(lambda response: "/classification/proposals?" in response.url and "selection=true" in response.url) as selection_response:
                review.get_by_role("checkbox", name="全选所有页", exact=True).click()
            selected_count = len(selection_response.value.json()["data"])
            expect(review.get_by_text(f"已选 {selected_count} 项", exact=True)).to_be_visible()
            expect(review.get_by_role("button", name="全部批准", exact=True)).to_be_visible(timeout=15000)
            review.get_by_role("button", name="关闭", exact=True).click()
            # A second device has no sessionStorage task pointer. It discovers
            # the saved work from the catalog, then opens the same saved results.
            second = browser.new_context(viewport={"width": 390, "height": 844})
            second.add_cookies(context.cookies())
            other = second.new_page()
            other.on("pageerror", lambda e: errors.append(str(e)))
            other.goto("http://127.0.0.1:8001/library")
            expect(other.get_by_role("heading", name="全部网站", exact=True)).to_be_visible()
            assert_fixed_task_entry(other)
            other.screenshot(path=str(output / "task-dock-mobile.png"))
            open_tasks(other)
            other_tasks = other.get_by_role("dialog", name="后台任务", exact=True)
            expect(other_tasks.get_by_label("任务批次").locator(f'option[value="{group_id}"]')).to_have_count(1)
            other_tasks.get_by_label("任务批次").select_option(group_id)
            other_tasks.get_by_role("button", name=re.compile(r"^校对分类")).click()
            expect(other.get_by_role("dialog", name="分类校对", exact=True).get_by_role("checkbox", name=re.compile(r"^选择建议 ")).first).to_be_visible()
            assert other.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            report = {"group_id": group_id, "website_total": group["website_total"],
                      "batch_total": group["total"], "succeeded_sites": group["succeeded_sites"],
                      "progress_ui": True, "review_ui": True, "visible_proposals": visible_proposals,
                      "reload_restored": True,
                      "new_device_recovered": True, "mobile_overflow": False,
                      "fixed_task_entry_reused": True, "header_task_button_removed": True,
                      "desktop_and_mobile_fixed_bottom_right": True,
                      "long_folder_scroll_does_not_move_entry": True,
                      "all_pages_selection": selected_count, "approval_submitted": False,
                      "single_classification_entry": True,
                      "page_errors": errors, "new_ai_requests": 0}
            (output / "classification-live-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            browser.close()
            print(json.dumps(report))
    finally:
        store.delete()


if __name__ == "__main__":
    main()
