"""Assert preview/card alignment in the scroll frame, before any later JS update."""
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.parse import urlparse

from preview_retention_ui import Frontend, Library, OUTPUT, ROOT
from playwright.async_api import async_playwright, expect


class ScrollLibrary(Library):
    def __init__(self, count):
        super().__init__()
        self.count = count

    async def route(self, route):
        if urlparse(route.request.url).path == "/api/v1/bookmarks":
            mobile = "variant=mobile" in route.request.url
            await route.fulfill(json={"data": [self.bookmark(n, "mobile" if mobile else "desktop") for n in range(self.count)],
                                      "page": {"total": self.count, "page": 1, "page_size": 48}})
            return
        await super().route(route)


async def alignment(page, selector):
    result = await page.evaluate("""selector => new Promise(resolve => requestAnimationFrame(() => {
      const root = document.querySelector(selector);
      const anchor = root.querySelector('[data-preview-container]');
      const frame = [...root.querySelectorAll('iframe')].find(el => el.src.includes('/version-0-'));
      const scroller = getComputedStyle(root).overflowY === 'visible' ? document.scrollingElement : root;
      const gaps = [];
      for (const delta of [80, -60, 40, -30]) {
        scroller.scrollTop += delta;
        const actual = frame.getBoundingClientRect();
        const expected = anchor.getBoundingClientRect();
        gaps.push(Math.abs(actual.top - expected.top - anchor.clientTop));
      }
      resolve({gaps, max_gap_px: Math.max(...gaps), scrollable: scroller.scrollHeight > scroller.clientHeight});
    }))""", selector)
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    result["settled_gap_px"] = await page.evaluate("""selector => {
      const root = document.querySelector(selector);
      const anchor = root.querySelector('[data-preview-container]');
      const frame = [...root.querySelectorAll('iframe')].find(el => el.src.includes('/version-0-'));
      return Math.abs(frame.getBoundingClientRect().top - anchor.getBoundingClientRect().top - anchor.clientTop);
    }""", selector)
    assert result["scrollable"], "Fixture must exercise a real scroll container"
    return result


async def measure(browser, origin, mobile, count=6):
    context = await browser.new_context(viewport={"width": 390 if mobile else 1440, "height": 844 if mobile else 720})
    page = await context.new_page()
    library = ScrollLibrary(count)
    await page.route("**/api/v1/**", library.route)
    await page.goto(origin + "/library?device=" + ("mobile" if mobile else "desktop"))
    await expect(page.locator('main [data-preview-state="ready"]')).to_have_count(count, timeout=30000)
    result = await alignment(page, "main")
    # Exercise actual wheel input too, in this isolated headless browser only.
    await page.get_by_role("button", name="查看 Preview Site 000", exact=True).hover()
    await page.mouse.wheel(0, 80)
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    result["wheel"] = await alignment(page, "main")
    await page.get_by_role("button", name="查看 Preview Site 000", exact=True).click()
    detail = page.get_by_role("dialog" if mobile else "complementary", name="网站详情", exact=True)
    await expect(detail.locator('[data-preview-state="ready"]')).to_have_count(1)
    result["detail"] = await alignment(page, '[aria-label="网站详情"]')
    await page.screenshot(path=str(OUTPUT / f"preview-scroll-{'mobile' if mobile else 'desktop'}-{count}.png"))
    await context.close()
    return result


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Frontend, directory=str(ROOT / "frontend/dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            origin = f"http://127.0.0.1:{server.server_port}"
            report = {"desktop": await measure(browser, origin, False), "mobile": await measure(browser, origin, True),
                      "desktop_48": await measure(browser, origin, False, 48), "mobile_48": await measure(browser, origin, True, 48)}
            await browser.close()
        report["passed"] = all(item["max_gap_px"] <= 1 and item["wheel"]["max_gap_px"] <= 1
                               and item["detail"]["max_gap_px"] <= 1 for item in report.values())
        (OUTPUT / "preview-scroll-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
        assert report["passed"], "Preview lags behind its card during scrolling"
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
