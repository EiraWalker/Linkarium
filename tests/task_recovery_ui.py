"""Real queue + delayed HTTP AI response + browser reload, isolated test data.

Run run_import_workflow_server.py with BOOKMARK_ACCEPTANCE_DATA_ROOT set to
output/task-recovery-data first. No production library or provider is touched.
"""
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
tempfile.tempdir = str(ROOT / "output/test-temp")
os.environ["TEMP"] = os.environ["TMP"] = tempfile.tempdir
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools/browsers")
started, release = threading.Event(), threading.Event()


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = next(m["content"] for m in reversed(request["messages"]) if m["role"] == "user")
        try:
            payload = json.loads(user)
        except ValueError:
            payload = {}
        if payload.get("task") == "plan_taxonomy":
            result = {"categories": [{"path": ["任务恢复验收"], "description": "Fixture websites"}]}
        elif "websites" in payload:
            started.set()
            if not release.wait(45):
                self.send_error(504)
                return
            result = {"items": [{"bookmark_id": site["bookmark_id"],
                "target": payload["categories"][0]["target"],
                "reason": "Delayed fixture result"} for site in payload["websites"]]}
        else:
            result = {"ok": True}
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(result)}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def api(page, path, method="GET", body=None):
    return page.evaluate("""async ({path,method,body}) => {
      const token=(await (await fetch('/api/v1/session')).json()).data.csrf_token;
      const r=await fetch('/api/v1/'+path,{method,headers:{'Content-Type':'application/json','X-CSRFToken':token},body:method==='GET'?undefined:JSON.stringify(body)});
      const result=await r.json(); if(!r.ok) throw new Error(JSON.stringify(result)); return result.data;
    }""", {"path": path, "method": method, "body": body})


def open_tasks(page):
    expect(page.locator('header').get_by_role("button", name="后台任务", exact=True)).to_have_count(0)
    button = page.get_by_role("button", name="后台任务", exact=True)
    button.click()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 18809), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://127.0.0.1:18767/library")
            page.locator('input[type="password"]').fill((ROOT / "output/task-recovery-data/runtime/acceptance-password").read_text())
            page.get_by_role("button", name="进入收藏库", exact=True).click()
            expect(page.get_by_role("heading", name="全部网站", exact=True)).to_be_visible()
            settings = api(page, "settings")
            api(page, "settings", "PATCH", {"revision": settings["revision"], "active_ai_profile_id": None})
            site = api(page, "bookmarks", "POST", {"url": f"https://example.org/recovery-{time.time_ns()}", "title": "Task recovery fixture", "snapshot_enabled": False})
            profile = api(page, "ai-profiles", "POST", {"name": "Delayed recovery fixture", "base_url": "http://127.0.0.1:18809/v1", "model": "fixture", "api_key": "fixture-key"})
            test = api(page, f"ai-profiles/{profile['id']}/test", "POST", {})
            deadline = time.monotonic() + 10
            while api(page, f"jobs/{test['job_id']}")["state"] != "succeeded":
                assert time.monotonic() < deadline, "Fixture AI verification did not finish"
                page.wait_for_timeout(250)
            group = api(page, "classification/batches", "POST", {"bookmark_ids": [site["id"]], "profile_id": profile["id"]})
            assert started.wait(10), "Worker did not reach the delayed AI provider"
            identity = group["group_id"]
            # No trackGroup() call: reload recovers a server-created task even
            # when the original response was never tracked by this client.
            page.reload()
            open_tasks(page)
            tasks = page.get_by_role("dialog", name="后台任务", exact=True)
            expect(tasks.get_by_label("任务批次").locator(f'option[value="{identity}"]')).to_have_count(1)
            tasks.get_by_label("任务批次").select_option(identity)
            expect(tasks.get_by_role("button", name="查看建议", exact=True)).to_have_count(0)
            tasks.get_by_role("button", name="关闭", exact=True).click()
            # A site's detail remains the way to review a single site while
            # the unified task entry has no pending results yet.
            page.get_by_label("搜索网站", exact=True).fill(site["url"])
            expect(page.get_by_role("button", name="查看 Task recovery fixture", exact=True)).to_have_count(1)
            page.get_by_role("button", name="查看 Task recovery fixture", exact=True).click()
            page.get_by_role("button", name="查看建议", exact=True).click()
            review = page.get_by_role("dialog", name="分类校对", exact=True)
            expect(review.get_by_text("暂无待确认的分类建议", exact=True)).to_be_visible()
            release.set()
            # Real model HTTP response is validated/published by the real worker;
            # the already-open modal must receive it without manual refresh.
            expect(review.get_by_label("选择建议 Task recovery fixture", exact=True)).to_be_visible(timeout=20000)
            assert api(page, f"bookmarks/{site['id']}")["folder_id"] is None
            review.get_by_role("button", name="关闭", exact=True).click()
            expect(page.get_by_role("button", name="后台任务", exact=True)).to_contain_text("待校对", timeout=10000)
            # A fresh mobile context has no task pointer, and opens server results.
            other_context = browser.new_context(viewport={"width": 390, "height": 844})
            other_context.add_cookies(context.cookies())
            other = other_context.new_page()
            other.on("pageerror", lambda e: errors.append(str(e)))
            other.goto("http://127.0.0.1:18767/library")
            open_tasks(other)
            other_tasks = other.get_by_role("dialog", name="后台任务", exact=True)
            expect(other_tasks.get_by_label("任务批次").locator(f'option[value="{identity}"]')).to_have_count(1)
            other_tasks.get_by_role("button", name=re.compile(r"^校对分类")).click()
            other_review = other.get_by_role("dialog", name="分类校对", exact=True)
            choice = other_review.get_by_label("选择建议 Task recovery fixture", exact=True)
            expect(choice).to_be_visible()
            assert other.evaluate("document.documentElement.scrollWidth <= innerWidth")
            choice.click()
            other_review.get_by_role("button", name=re.compile(r"^(确认选中|全部批准)$")).click()
            expect(choice).not_to_be_visible()
            assert api(other, f"bookmarks/{site['id']}")["folder_id"] is not None
            other_review.get_by_role("button", name="关闭", exact=True).click()
            open_tasks(other)
            expect(other.get_by_role("dialog", name="后台任务").get_by_role("button", name=re.compile(r"^校对分类"))).to_be_disabled(timeout=10000)
            open_tasks(page)
            expect(page.get_by_role("dialog", name="后台任务").get_by_role("button", name=re.compile(r"^校对分类"))).to_be_disabled(timeout=15000)
            output = ROOT / "output/playwright"
            output.mkdir(exist_ok=True, parents=True)
            other.screenshot(path=str(output / "task-recovery-mobile.png"))
            report = {"server_task_recovered_after_reload": True, "delayed_ai_result_published": True,
                      "open_review_auto_updated": True, "pending_badge_auto_updated": True,
                      "fresh_device_recovered": True, "manual_confirmation_required": True,
                      "accepted_result_applied": True, "mobile_overflow": False, "page_errors": errors}
            report["other_device_confirmation_synced"] = True
            assert not errors, errors
            (output / "task-recovery-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report))
            browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
