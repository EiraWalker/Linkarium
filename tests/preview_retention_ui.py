"""Browser acceptance for preview loading/retention against the built application.

Serves only frontend/dist and uses an in-browser, isolated API library. No Django
database, capture worker, real bookmarks or existing browser session is touched.
"""
import asyncio
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools" / "browsers")
from playwright.async_api import async_playwright, expect

OUTPUT = ROOT / "output" / "playwright"


class Frontend(SimpleHTTPRequestHandler):
    def do_GET(self):
        if not urlparse(self.path).path.startswith("/assets/"):
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *_):
        pass


class Library:
    def __init__(self):
        self.authenticated = True
        self.generation = "preview-library-one"
        self.versions = {}
        self.deleted = set()
        self.deleted_versions = set()
        self.failures = {}
        self.requests = Counter()
        self.loads = Counter()
        self.active = 0
        self.peak = 0
        self.expire_next = False
        self.folders = [dict(id="folder-a", revision=1, name="Preview Folder A", parent_id=None, count=60)]

    def snapshot(self, n, variant, suffix=""):
        version = f"version-{n}-{variant}{suffix}"
        return dict(id=version, version_id=version, variant=variant,
                    preview_url=f"/api/v1/snapshots/{version}/preview",
                    viewport_width=1366 if variant == "desktop" else 390,
                    viewport_height=768 if variant == "desktop" else 844,
                    captured_at="2026-10-15T00:00:00Z", is_current=not suffix)

    def bookmark(self, n, variant):
        return dict(id=f"site-{n}", revision=1, title=f"Preview Site {n:03}",
                    url=f"https://preview.example.test/{n}", domain="preview.example.test",
                    folder_id="folder-a" if n < 60 else None, tags=[], notes="",
                    is_starred=False, snapshot_enabled=n != 3, auto_refresh_paused=True,
                    preview=None if n == 119 else self.snapshot(n, variant, self.versions.get(n, "")),
                    capture_status={"state": "succeeded"})

    async def route(self, route):
        req = route.request
        parsed = urlparse(req.url)
        path = parsed.path.removeprefix("/api/v1/")
        q = parse_qs(parsed.query)
        variant = q.get("variant", ["desktop"])[0]
        data = []
        if path.startswith("snapshots/") and path.endswith("/preview"):
            version = path.split("/")[1]
            if req.method == "HEAD":
                if self.failures.get(version) == "error":
                    await route.fulfill(status=404, content_type="text/html", body="")
                else:
                    await route.fulfill(status=200, content_type="text/html", body="")
                return
            self.requests[version] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                mode = self.failures.get(version)
                if mode == "timeout":
                    await asyncio.sleep(17)
                else:
                    await asyncio.sleep(0.025)
                if mode == "error":
                    await route.abort("failed")
                    return
                await route.fulfill(status=200, content_type="text/html", headers={
                    "Cache-Control": "private, no-store",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; sandbox",
                }, body=f'''<!doctype html><html><body style="margin:0;background:#b9d9f2;color:#15375b">
                  <div style="padding:30px;font:100px sans-serif">{version}</div>
                  <div style="height:400px;background:linear-gradient(120deg,#5094b4,#f2d27b)"></div>
                  <script>parent.__previewLeak = true</script></body></html>''')
            finally:
                self.active -= 1
            return
        if path == "session":
            data = dict(authenticated=self.authenticated, csrf_token="fixture-csrf",
                        active_generation=self.generation, library_epoch=1)
        elif path == "session/logout":
            self.authenticated = False
            data = {}
        elif path == "session/login":
            self.authenticated = True
            data = {}
        elif path == "folders":
            data = self.folders
        elif path == "settings":
            data = dict(revision=1, tree_revision=1, default_refresh_seconds=604800)
        elif path == "job-groups":
            data = dict(groups=[], active_count=0, pending_proposals=0, completion_revision=0)
        elif path == "runtime/status":
            data = {}
        elif path == "bookmarks":
            sites = [self.bookmark(n, variant) for n in range(120) if n not in self.deleted]
            if q.get("folder_id"):
                sites = [site for site in sites if site["folder_id"] == q["folder_id"][0]]
            if q.get("q"):
                sites = [site for site in sites if q["q"][0].lower() in site["title"].lower()]
            if q.get("selection"):
                data = [dict(id=site["id"], revision=1) for site in sites]
            else:
                number = int(q.get("page", [1])[0])
                page = dict(total=len(sites), page=number, page_size=48, total_pages=(len(sites)+47)//48)
                await route.fulfill(json=dict(data=sites[(number-1)*48:number*48], page=page))
                return
        elif re.fullmatch(r"bookmarks/site-\d+", path):
            n = int(path.split("-")[-1])
            if req.method == "PATCH" and self.expire_next:
                self.expire_next = False
                self.authenticated = False
                await route.fulfill(status=401, json={"error": {"code": "SESSION_EXPIRED", "message": "会话已失效"}})
                return
            data = self.bookmark(n, variant)
        elif re.fullmatch(r"bookmarks/site-\d+/snapshots", path):
            n = int(path.split("/")[1].split("-")[-1])
            data = [self.snapshot(n, variant, self.versions.get(n, "")), self.snapshot(n, variant, "-old")]
            data = [snapshot for snapshot in data if snapshot["version_id"] not in self.deleted_versions]
        elif path in ("bookmarks/delete-preview", "snapshots/delete-preview") or path.endswith("/delete-preview"):
            data = dict(token="fixture-delete", websites=1, versions=1, files=1)
        elif path == "bookmarks/bulk":
            body = req.post_data_json
            if body["action"] == "delete":
                self.deleted.update(int(item["id"].split("-")[-1]) for item in body["items"])
            data = {}
        elif path == "snapshots/bulk-delete":
            self.deleted_versions.update(req.post_data_json["ids"])
            data = {}
        elif path == "folders/folder-a" and req.method == "DELETE":
            self.deleted.update(range(60))
            self.folders = []
            data = {}
        await route.fulfill(json={"data": data})


async def settle(page, count=48):
    anchors = page.locator('main [data-preview-container]')
    await expect(anchors).to_have_count(count)
    await expect(page.locator('main [data-preview-state="ready"]')).to_have_count(count, timeout=45000)
    await page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


async def scroll(page, bottom):
    await page.locator("main").evaluate("(el, bottom) => { el.scrollTop = bottom ? el.scrollHeight : 0; window.scrollTo(0, bottom ? document.body.scrollHeight : 0); }", bottom)
    await page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


async def metrics(page):
    session = await page.context.new_cdp_session(page)
    await session.send("Performance.enable")
    raw = await session.send("Performance.getMetrics")
    await session.detach()
    return {entry["name"]: entry["value"] for entry in raw["metrics"]
            if entry["name"] in ("JSHeapUsedSize", "Nodes", "Documents", "Frames", "TaskDuration")}


async def scenario(browser, origin, mobile=False):
    context = await browser.new_context(viewport={"width": 390 if mobile else 1440, "height": 844 if mobile else 960})
    page = await context.new_page()
    library = Library()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("framenavigated", lambda frame: library.loads.update([frame.url.split("/")[-2]])
            if "/api/v1/snapshots/" in frame.url else None)
    await page.route("**/api/v1/**", library.route)
    variant = "mobile" if mobile else "desktop"
    await page.goto(f"{origin}/library?device={variant}")
    await settle(page)
    assert len(library.requests) == 48 and all(n == 1 for n in library.requests.values()), library.requests
    assert library.peak <= (1 if mobile else 2), library.peak
    assert await page.locator("main").evaluate("el => el.scrollTop") == 0
    baseline = dict(library.loads)
    resource_start = await metrics(page)
    for _ in range(2):
        await scroll(page, True)
        await scroll(page, False)
    assert dict(library.loads) == baseline, "Scrolling reloaded preview documents"
    assert await page.locator('input[aria-label="选择 Preview Site 000"]').is_visible()
    await page.screenshot(path=str(OUTPUT / f"preview-retention-{variant}-top.png"))
    await scroll(page, True)
    await page.screenshot(path=str(OUTPUT / f"preview-retention-{variant}-bottom.png"))
    await page.get_by_role("button", name="下一页", exact=True).click()
    await settle(page)
    assert len(library.requests) == 96, "Unvisited third page must not preload"
    await page.get_by_role("button", name="上一页", exact=True).click()
    await settle(page)
    assert all(n == 1 for n in library.requests.values())
    first_two = dict(library.loads)
    if mobile:
        await scroll(page, False)
        await page.get_by_role("button", name="打开导航", exact=True).click()
    await page.get_by_role("button", name="Preview Folder A 60", exact=True).click()
    await settle(page)
    assert dict(library.loads) == first_two, "Folder navigation reloaded a retained snapshot"
    await page.go_back()
    await settle(page)
    if mobile:
        # The mobile header exposes compact links with the same accessible names.
        await scroll(page, False)
    if mobile:
        await page.get_by_role("button", name="打开导航", exact=True).click()
    await page.get_by_role("navigation", name="工具页面" if mobile else "主页面", exact=True).get_by_role("link", name="设置", exact=True).click()
    await expect(page.get_by_role("heading", name="设置", exact=True)).to_be_visible()
    assert await page.locator("iframe:visible").count() == 0
    await page.get_by_role("link", name="网站收藏", exact=True).click()
    await settle(page)
    await page.go_back()
    await expect(page.get_by_role("heading", name="设置", exact=True)).to_be_visible()
    await page.go_forward()
    await settle(page)
    assert dict(library.loads) == first_two, "Application navigation reloaded previews"
    after_returns = await metrics(page)
    # Real card actions must remain above the visual surface.
    await scroll(page, False)
    await page.get_by_role("checkbox", name="选择 Preview Site 000", exact=True).check()
    await page.get_by_role("checkbox", name="选择 Preview Site 000", exact=True).uncheck()
    await page.get_by_role("button", name="查看 Preview Site 000", exact=True).click()
    detail = page.get_by_role("dialog" if mobile else "complementary", name="网站详情", exact=True)
    await expect(detail.locator('[data-preview-state="ready"]')).to_have_count(1)
    await page.screenshot(path=str(OUTPUT / f"preview-retention-{variant}-detail.png"))
    frames = await page.locator("iframe").evaluate_all("els => els.every(el => el.getAttribute('sandbox') === '' && el.tabIndex === -1)")
    assert frames and await page.evaluate("window.__previewLeak === undefined")
    await page.get_by_role("button", name="关闭详情", exact=True).click()
    # Metadata updates must display the new version rather than a retained old one.
    library.versions[0] = "-new"
    await page.get_by_role("button", name="添加星标 Preview Site 000", exact=True).click()
    await expect(page.locator(f'iframe[src$="/version-0-{variant}-new/preview"]')).to_have_count(1)
    await settle(page)
    assert library.requests[f"version-0-{variant}-new"] == 1
    # An API 401 follows the real application session-expiry path.
    library.expire_next = True
    await page.get_by_role("button", name="添加星标 Preview Site 000", exact=True).click()
    await expect(page.get_by_role("button", name="进入收藏库", exact=True)).to_be_visible()
    await expect(page.locator("iframe")).to_have_count(0)
    library.generation = "preview-library-two"
    await page.locator('input[type="password"]').fill("isolated-fixture-password")
    await page.get_by_role("button", name="进入收藏库", exact=True).click()
    await settle(page)
    assert library.requests[f"version-0-{variant}-new"] == 2
    await page.reload()
    await settle(page)
    assert library.requests[f"version-0-{variant}-new"] == 3
    if not mobile:
        await page.get_by_role("button", name="下一页", exact=True).click()
        await settle(page)
        await page.get_by_role("button", name="上一页", exact=True).click()
        await settle(page)
        retained_loads = library.loads["version-60-desktop"]
        await scroll(page, False)
        await page.get_by_role("button", name="查看 Preview Site 001", exact=True).click()
        detail = page.get_by_role("complementary", name="网站详情", exact=True)
        await expect(detail.locator('[data-preview-state="ready"]')).to_have_count(1)
        await detail.locator("ul li button").last.click()
        old_frame = page.locator('iframe[src$="/version-1-desktop-old/preview"]')
        await expect(old_frame).to_have_count(1)
        await expect(detail.locator('[data-preview-state="ready"]')).to_have_count(1)
        await detail.get_by_role("button", name="查看全部", exact=True).click()
        await detail.get_by_role("button", name="清理旧历史", exact=True).click()
        cleanup = page.get_by_role("dialog", name="清理旧快照历史", exact=True)
        await cleanup.get_by_role("checkbox").last.check()
        await cleanup.get_by_role("button", name="预览删除影响", exact=True).click()
        await cleanup.get_by_role("button", name="确认删除", exact=True).click()
        await expect(old_frame).to_have_count(0)
        await page.get_by_role("button", name="关闭详情", exact=True).click()
        await scroll(page, False)
        await page.get_by_role("checkbox", name="选择 Preview Site 000", exact=True).check()
        await page.get_by_role("button", name="删除", exact=True).click()
        deletion = page.get_by_role("dialog", name="删除网站", exact=True)
        await deletion.get_by_role("button", name="确认", exact=True).click()
        await settle(page)
        await expect(page.locator('iframe[src*="/version-0-desktop"]')).to_have_count(0)
        await page.get_by_role("button", name="管理文件夹 Preview Folder A", exact=True).click()
        folder_dialog = page.get_by_role("dialog", name="管理文件夹", exact=True)
        await folder_dialog.get_by_role("button", name="删除文件夹", exact=True).click()
        await folder_dialog.get_by_role("button", name="确认删除全部", exact=True).click()
        await settle(page)
        await expect(page.locator('iframe[src*="/version-1-desktop"]')).to_have_count(0)
        # Unrelated retained documents survive deletion of another folder.
        await expect(page.locator('iframe[src$="/version-60-desktop/preview"]')).to_have_count(1)
        assert library.loads["version-60-desktop"] == retained_loads
    assert not errors, errors
    result = dict(viewport=variant, all_48_without_scrolling=True, loads_before_navigation=len(baseline),
                  unique_after_two_pages=96, scroll_and_navigation_no_reload=True,
                  sandbox_and_controls=True, new_version=True, session_expiry_clears=True,
                  new_library_and_refresh_reload=True, peak_concurrent=library.peak,
                  deletion_cleanup=not mobile,
                  resource_one_page=resource_start, resource_after_returns=after_returns, page_errors=errors)
    await context.close()
    return result


async def fault_scenario(browser, origin):
    context = await browser.new_context(viewport={"width": 1440, "height": 960})
    page = await context.new_page()
    library = Library()
    library.failures = {"version-0-desktop": "timeout", "version-1-desktop": "error"}
    await page.route("**/api/v1/**", library.route)
    await page.goto(f"{origin}/library?device=desktop")
    await expect(page.locator('main [data-preview-state="ready"]')).to_have_count(46, timeout=40000)
    await expect(page.locator('main [data-preview-state="timeout"]')).to_have_count(1, timeout=20000)
    await expect(page.locator('main [data-preview-state="error"]')).to_have_count(1)
    assert len(library.requests) == 47  # Rejected HEAD must not download the broken HTML.
    await expect(page.locator('main [data-preview-state="ready"]')).to_have_count(47, timeout=5000)
    # Final page includes a no-snapshot site; it must not enter the queue.
    await page.get_by_role("button", name="下一页", exact=True).click()
    await settle(page)
    await page.get_by_role("button", name="下一页", exact=True).click()
    await expect(page.locator('main [data-preview-state="ready"]')).to_have_count(23)
    await expect(page.locator('main [data-preview-state="missing"]')).to_have_count(1)
    assert "version-119-desktop" not in library.requests
    await context.close()
    return dict(error_and_timeout_do_not_block=True, late_success=True, missing_snapshot=True)


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Frontend, directory=str(ROOT / "frontend" / "dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    report = {"scope": "built application in Chromium; isolated browser API fixtures; no production data; mobile viewport is simulated"}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            report["desktop"] = await scenario(browser, origin)
            print("Desktop preview acceptance passed", flush=True)
            report["mobile"] = await scenario(browser, origin, mobile=True)
            print("Mobile viewport preview acceptance passed", flush=True)
            report["faults"] = await fault_scenario(browser, origin)
            await browser.close()
        report["passed"] = True
    finally:
        server.shutdown()
        (OUTPUT / "preview-retention-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
