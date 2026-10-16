"""Exercise the built UI against isolated thumbnail API fixtures; no live data."""
import asyncio
from collections import Counter
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.preview_retention_ui import Frontend, Library, async_playwright, expect, scroll, settle

OUTPUT = ROOT / "output" / "playwright" / "thumbnail-preview"


class ThumbnailLibrary(Library):
    def __init__(self, jpeg):
        super().__init__()
        self.jpeg = jpeg
        self.thumbnail_requests = Counter()
        self.modes = {}

    def snapshot(self, *args, **kwargs):
        snapshot = super().snapshot(*args, **kwargs)
        snapshot["thumbnail_url"] = snapshot["preview_url"].replace("/preview", "/thumbnail?v=1")
        return snapshot

    async def route(self, route):
        path = urlparse(route.request.url).path
        if not path.endswith("/thumbnail"):
            return await super().route(route)
        version = path.split("/")[-2]
        self.thumbnail_requests[version] += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.025)
            mode = self.modes.get(version)
            if mode == "generating" and self.thumbnail_requests[version] < 2:
                await route.fulfill(status=202, json={"state": "pending"}, headers={"Retry-After": "2"})
            elif mode == "error":
                await route.fulfill(status=404, json={"error": {"code": "NOT_FOUND"}})
            elif mode == "timeout":
                await asyncio.sleep(17)
                await route.fulfill(status=200, content_type="image/jpeg", body=self.jpeg)
            elif mode == "expired":
                self.authenticated = False
                await route.fulfill(status=401, json={"error": {"code": "SESSION_EXPIRED"}})
            elif mode == "wrong_type":
                await route.fulfill(status=200, content_type="text/html", body="<h1>Forbidden fallback</h1>")
            else:
                await route.fulfill(status=200, content_type="image/jpeg", body=self.jpeg,
                                    headers={"Cache-Control": "private, max-age=31536000", "ETag": '"fixture"'})
        finally:
            self.active -= 1


async def scenario(browser, origin, jpeg, mobile=False):
    variant = "mobile" if mobile else "desktop"
    context = await browser.new_context(viewport={"width": 390 if mobile else 1440, "height": 844 if mobile else 960})
    page = await context.new_page()
    library = ThumbnailLibrary(jpeg)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.route("**/api/v1/**", library.route)
    await page.goto(f"{origin}/library?device={variant}")
    await settle(page)
    assert len(library.thumbnail_requests) == 48 and 3 <= library.peak <= 6
    await expect(page.locator("iframe")).to_have_count(0)
    await page.screenshot(path=str(OUTPUT / f"{variant}-first-page.png"))
    initial = dict(library.thumbnail_requests)
    await scroll(page, True)
    await scroll(page, False)
    assert dict(library.thumbnail_requests) == initial
    await page.get_by_role("button", name="下一页", exact=True).click()
    await settle(page)
    assert len(library.thumbnail_requests) == 96
    await page.get_by_role("button", name="上一页", exact=True).click()
    await settle(page)
    assert all(n == 1 for n in library.thumbnail_requests.values())
    if mobile:
        await scroll(page, False)
        await page.get_by_role("button", name="打开导航", exact=True).click()
    await page.get_by_role("button", name="Preview Folder A 60", exact=True).click()
    await settle(page)
    await page.go_back()
    await settle(page)
    before_detail = dict(library.thumbnail_requests)
    await scroll(page, False)
    await page.get_by_role("button", name="查看 Preview Site 000", exact=True).click()
    detail = page.get_by_role("dialog" if mobile else "complementary", name="网站详情", exact=True)
    await expect(detail.locator('[data-preview-state="ready"]')).to_have_count(1)
    assert dict(library.thumbnail_requests) == before_detail, "Card and details must share image download"
    await expect(detail.get_by_role("link", name="查看完整快照", exact=True)).to_have_attribute(
        "href", f"{origin}/api/v1/snapshots/version-0-{variant}/preview")
    await detail.locator("ul li button").last.click()
    await expect(detail.locator('[data-preview-state="ready"]')).to_have_count(1)
    await expect(detail.get_by_role("link", name="查看完整快照", exact=True)).to_have_attribute(
        "href", f"{origin}/api/v1/snapshots/version-0-{variant}-old/preview")
    await expect(page.locator(f'img[data-preview-image="version-0-{variant}-old"]')).to_have_count(1)
    await page.screenshot(path=str(OUTPUT / f"{variant}-history.png"))
    await page.get_by_role("button", name="关闭详情", exact=True).click()
    await scroll(page, False)
    library.versions[0] = "-new"
    await page.get_by_role("button", name="添加星标 Preview Site 000", exact=True).click()
    await expect(page.locator(f'img[data-preview-image="version-0-{variant}-new"]')).to_have_count(1)
    await settle(page)
    assert library.thumbnail_requests[f"version-0-{variant}-new"] == 1
    if mobile:
        await page.get_by_role("button", name="打开导航", exact=True).click()
    await page.get_by_role("navigation", name="工具页面" if mobile else "主页面", exact=True).get_by_role("link", name="设置", exact=True).click()
    await expect(page.get_by_role("heading", name="设置", exact=True)).to_be_visible()
    await expect(page.locator("[data-preview-image]:visible")).to_have_count(0)
    await page.get_by_role("link", name="Linkarium", exact=True).click()
    await settle(page)
    assert library.thumbnail_requests[f"version-0-{variant}-new"] == 1
    library.expire_next = True
    await page.get_by_role("button", name="添加星标 Preview Site 000", exact=True).click()
    await expect(page.get_by_role("button", name="密码登录", exact=True)).to_be_visible()
    await expect(page.locator("[data-preview-image]")).to_have_count(0)
    library.generation = "second-library"
    await page.locator('input[type="password"]').fill("fixture")
    await page.get_by_role("button", name="密码登录", exact=True).click()
    await settle(page)
    assert library.thumbnail_requests[f"version-0-{variant}-new"] == 2
    if not mobile:
        await page.get_by_role("button", name="下一页", exact=True).click()
        await settle(page)
        await page.get_by_role("button", name="上一页", exact=True).click()
        await settle(page)
        retained_requests = library.thumbnail_requests["version-60-desktop"]
        await scroll(page, False)
        await page.get_by_role("button", name="查看 Preview Site 001", exact=True).click()
        detail = page.get_by_role("complementary", name="网站详情", exact=True)
        await expect(detail.locator('[data-preview-state="ready"]')).to_have_count(1)
        await detail.locator("ul li button").last.click()
        old_image = page.locator('img[data-preview-image="version-1-desktop-old"]')
        await expect(old_image).to_have_count(1)
        await detail.get_by_role("button", name="查看全部", exact=True).click()
        await detail.get_by_role("button", name="清理旧历史", exact=True).click()
        cleanup = page.get_by_role("dialog", name="清理旧快照历史", exact=True)
        await cleanup.get_by_role("checkbox").last.check()
        await cleanup.get_by_role("button", name="预览删除影响", exact=True).click()
        await cleanup.get_by_role("button", name="确认删除", exact=True).click()
        await expect(old_image).to_have_count(0)
        await page.get_by_role("button", name="关闭详情", exact=True).click()
        await scroll(page, False)
        await page.get_by_role("checkbox", name="选择 Preview Site 000", exact=True).check()
        await page.get_by_role("button", name="删除", exact=True).click()
        deletion = page.get_by_role("dialog", name="删除网站", exact=True)
        await deletion.get_by_role("button", name="确认", exact=True).click()
        await settle(page)
        await expect(page.locator('img[data-preview-image^="version-0-desktop"]')).to_have_count(0)
        await page.get_by_role("button", name="管理文件夹 Preview Folder A", exact=True).click()
        folder_dialog = page.get_by_role("dialog", name="管理文件夹", exact=True)
        await folder_dialog.get_by_role("button", name="删除文件夹", exact=True).click()
        await folder_dialog.get_by_role("button", name="确认删除全部", exact=True).click()
        await settle(page)
        await expect(page.locator('img[data-preview-image^="version-1-desktop"]')).to_have_count(0)
        await expect(page.locator('img[data-preview-image="version-60-desktop"]')).to_have_count(1)
        assert library.thumbnail_requests["version-60-desktop"] == retained_requests
    assert not library.requests, "Automatic previews must never fetch HTML archives"
    assert not errors, errors
    result = {"viewport": variant, "all_current_page_loaded": True, "unvisited_page_not_loaded": True,
              "card_detail_share_download": True, "scroll_folder_navigation_retained": True,
              "version_history_identity": True, "session_cleanup": True,
              "history_bookmark_folder_delete_cleanup": not mobile,
              "html_archive_requests": sum(library.requests.values()), "peak_requests": library.peak}
    await context.close()
    return result


async def faults(browser, origin, jpeg):
    context = await browser.new_context(viewport={"width": 1440, "height": 960})
    page = await context.new_page()
    library = ThumbnailLibrary(jpeg)
    library.modes = {"version-0-desktop": "generating", "version-1-desktop": "error",
                     "version-2-desktop": "wrong_type", "version-3-desktop": "timeout"}
    await page.route("**/api/v1/**", library.route)
    await page.goto(f"{origin}/library?device=desktop")
    await expect(page.locator('main [data-preview-state="generating"]')).to_have_count(1)
    await expect(page.locator('main [data-preview-state="ready"]')).to_have_count(45, timeout=40000)
    await expect(page.locator('main [data-preview-state="error"]')).to_have_count(2)
    await expect(page.locator('main [data-preview-state="timeout"]')).to_have_count(1, timeout=20000)
    assert library.thumbnail_requests["version-0-desktop"] == 2
    assert not library.requests
    await context.close()
    context = await browser.new_context()
    page = await context.new_page()
    library = ThumbnailLibrary(jpeg)
    library.modes = {"version-0-desktop": "expired"}
    await page.route("**/api/v1/**", library.route)
    await page.goto(f"{origin}/library?device=desktop")
    await expect(page.get_by_role("button", name="密码登录", exact=True)).to_be_visible()
    await expect(page.locator("[data-preview-image]")).to_have_count(0)
    await context.close()
    return {"202_retries_then_ready": True, "404_and_wrong_type_no_html_fallback": True,
            "timeout_releases_queue": True, "thumbnail_401_ends_session": True}


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Frontend, directory=str(ROOT / "frontend" / "dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {"scope": "Built frontend; headless isolated API fixtures; simulated mobile; no production writes"}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            fixture = await browser.new_page(viewport={"width": 1366, "height": 768})
            await fixture.set_content('<body style="margin:0;background:#b9d9f2;color:#15375b"><h1 style="padding:50px">Cached first-screen preview</h1><div style="height:550px;background:linear-gradient(120deg,#5094b4,#f2d27b)"></div></body>')
            jpeg = await fixture.screenshot(type="jpeg", quality=85)
            await fixture.close()
            report["desktop"] = await scenario(browser, f"http://127.0.0.1:{server.server_port}", jpeg)
            print("Desktop thumbnails passed", flush=True)
            report["mobile"] = await scenario(browser, f"http://127.0.0.1:{server.server_port}", jpeg, mobile=True)
            print("Mobile thumbnails passed", flush=True)
            report["faults"] = await faults(browser, f"http://127.0.0.1:{server.server_port}", jpeg)
            await browser.close()
        report["passed"] = True
    finally:
        server.shutdown()
        (OUTPUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
