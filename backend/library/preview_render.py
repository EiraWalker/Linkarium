"""Offline renderer for existing archives. Invoked in a bounded child process."""
import os
from pathlib import Path
import sys
import time


def render(source, output, width, height):
    from playwright.sync_api import sync_playwright
    from capture.models import MAX_CAPTURE_BYTES
    from capture.preview import screenshot_preview
    from capture.replay_document import ReplayDocument
    from capture.runner import REPLAY_CSP
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_CAPTURE_BYTES:
        raise ValueError("Invalid archive")
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise ValueError("Invalid viewport")
    deadline = time.monotonic() + 55
    def remaining():
        return max(1, int((deadline - time.monotonic()) * 1000))
    with sync_playwright() as playwright, ReplayDocument(source.read_bytes(), REPLAY_CSP, deadline) as document:
        # Proxy only serves one immutable document; cannot forward traffic.
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        browser = playwright.chromium.launch(headless=True, chromium_sandbox=True, env=env,
            args=["--disable-background-networking", "--disable-component-update", "--disable-sync"], timeout=remaining())
        try:
            context = browser.new_context(viewport={"width": width, "height": height},
                device_scale_factor=1, java_script_enabled=False, service_workers="block", accept_downloads=False,
                proxy={"server": document.proxy_url, "bypass": "<-loopback>"})
            page = context.new_page()
            def route(request_route):
                request = request_route.request
                if request.url == document.url and request.is_navigation_request() and request.frame == page.main_frame:
                    request_route.continue_()
                else:
                    request_route.abort()
            context.route("**/*", route)
            context.route_web_socket("**/*", lambda socket: socket.close())
            response = page.goto(document.url, wait_until="load", timeout=remaining())
            if response is None or response.status != 200:
                raise ValueError("Archive failed to load")
            data = screenshot_preview(page, width, height, timeout_ms=remaining())
            with output.open("xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        from library.preview_lifetime import install_parent_death_guard
        install_parent_death_guard(int(sys.argv[5]) if len(sys.argv) > 5 else None)
        render(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        sys.exit(1)
