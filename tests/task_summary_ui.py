"""Reproduce a remembered old batch hiding new work; mock only task APIs."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
tempfile.tempdir = str(ROOT / "output/test-temp")
os.environ["TEMP"] = os.environ["TMP"] = tempfile.tempdir
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools/browsers")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
sys.path.insert(0, str(ROOT / "backend"))
import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
from runtime.models import RuntimeState
from playwright.sync_api import sync_playwright, expect


def main():
    old_id, active_id = str(uuid.uuid4()), str(uuid.uuid4())
    old = dict(id=old_id, kind="classification", state="partial", total=49, completed=49,
               requested_count=482, created_at="2026-10-09T16:00:00Z", jobs=[])
    active = dict(id=active_id, kind="classification", state="running", total=49, completed=7,
                  requested_count=482, created_at="2026-10-09T18:22:11Z", jobs=[])
    owner = get_user_model().objects.get(username="owner")
    store = importlib.import_module(settings.SESSION_ENGINE).SessionStore()
    store["_auth_user_id"] = str(owner.pk)
    store["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    store["_auth_user_hash"] = owner.get_session_auth_hash()
    store.set_expiry(180)
    store.save()
    runtime = RuntimeState.objects.get(pk=1)
    key = f"website-task-{runtime.active_generation}:{runtime.library_epoch}"
    errors = []
    completion = {"revision": 7}
    reads = {"bookmarks": 0, "folders": 0}
    detail_reads = {"count": 0}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1102, "height": 828})
            context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": store.session_key,
                                 "url": "http://127.0.0.1:8001", "httpOnly": True}])
            context.add_init_script(f"sessionStorage.setItem({json.dumps(key)}, {json.dumps(old_id)})")
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            navigations = []
            page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
            def library_read(route):
                path = urlparse(route.request.url).path
                result = route.fetch()
                body = result.json()
                if path.endswith("/bookmarks"):
                    reads["bookmarks"] += 1
                    body["data"][0]["title"] = f"同步卡片 {completion['revision']}"
                else:
                    reads["folders"] += 1
                route.fulfill(response=result, json=body)
            page.route("**/api/v1/bookmarks?*", library_read)
            page.route("**/api/v1/folders", library_read)
            def tasks(route):
                url = urlparse(route.request.url)
                if url.path.endswith("/job-groups"):
                    number = int(parse_qs(url.query).get("page", ["1"])[0])
                    route.fulfill(json={"data": {"groups": [active, old] if number == 1 else [old],
                        "current_group": active, "active_count": 1, "pending_proposals": 70,
                        "completion_revision": completion["revision"]},
                        "page": {"total": 21, "page": number, "page_size": 20}})
                else:
                    detail_reads["count"] += 1
                    identity = url.path.rsplit("/", 1)[-1]
                    route.fulfill(json={"data": active if identity == active_id else old})
            page.route("**/api/v1/job-groups**", tasks)
            page.goto("http://127.0.0.1:8001/library")
            summary = page.get_by_role("button", name="后台任务", exact=True)
            expect(summary).to_contain_text("7 / 49", timeout=10000)
            expect(summary).to_contain_text("正在执行")
            expect(summary).not_to_contain_text("部分成功")
            selected_card = page.get_by_role("checkbox", name="选择 同步卡片 7", exact=True)
            selected_card.check()
            assert detail_reads["count"] == 0, "Closed task dialog must use the catalog summary"
            summary.click()
            dialog = page.get_by_role("dialog", name="后台任务", exact=True)
            select = dialog.get_by_label("任务批次", exact=True)
            expect(select).to_have_value(active_id)
            page.wait_for_timeout(250)
            history_reads = dict(reads)
            select.select_option(old_id)
            expect(dialog.get_by_text("已完成 49 / 49 批次", exact=False)).to_be_visible()
            page.wait_for_timeout(250)
            assert reads == history_reads, "Opening completed history must not refresh library data"
            expect(summary).to_contain_text("7 / 49")
            dialog.get_by_role("button", name="下一组任务", exact=True).click()
            active["completed"] = 8
            completion["revision"] = 8
            expect(summary).to_contain_text("8 / 49", timeout=12000)
            expect(page.get_by_role("checkbox", name="选择 同步卡片 8", exact=True)).to_be_checked(timeout=10000)
            expect(select).to_have_value(old_id)
            # A failed request entering retry_wait also completed an attempt,
            # even though the group's terminal-job counter has not advanced.
            completion["revision"] = 9
            expect(page.get_by_role("checkbox", name="选择 同步卡片 9", exact=True)).to_be_checked(timeout=10000)
            expect(select).to_have_value(old_id)
            page.wait_for_timeout(500)
            idle_reads = dict(reads)
            page.wait_for_timeout(3500)
            assert reads == idle_reads, "Unchanged polling must not repeatedly reload library data"
            assert len(navigations) == 1, "Completion must update data without navigating"
            dialog.get_by_role("button", name="关闭", exact=True).click()
            summary.click()
            expect(select).to_have_value(active_id)
            expect(dialog.get_by_text("已完成 8 / 49 批次", exact=False)).to_be_visible()
            dialog.get_by_role("button", name="关闭", exact=True).click()
            page.reload()
            expect(summary).to_contain_text("8 / 49")
            summary.click()
            expect(select).to_have_value(active_id)
            assert not errors, errors
            output = ROOT / "output/playwright"
            page.screenshot(path=str(output / "task-summary.png"))
            (output / "task-summary-report.json").write_text(json.dumps({
                "old_session_pointer_replaced": True, "sidebar_tracks_live_task": True,
                "history_selection_preserved_while_open": True, "pagination_does_not_hide_live_task": True,
                "reopen_follows_live_task": True, "reload_follows_live_task": True,
                "each_completion_refreshes_library": True, "retry_failure_refreshes_library": True,
                "selection_and_history_preserved": True, "no_full_page_reload": True,
                "unchanged_poll_does_not_reload_data": True,
                "opening_history_does_not_reload_data": True,
                "closed_dialog_does_not_fetch_job_payloads": True,
                "page_errors": errors, "real_ai_submissions": 0}, indent=2), encoding="utf8")
            browser.close()
        print("Task summary browser acceptance passed")
    finally:
        store.delete()


if __name__ == "__main__":
    main()
