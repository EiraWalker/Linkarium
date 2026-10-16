"""Privacy, aggregation, and method-safety contracts for the browser workflow."""
from pathlib import Path
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.performance_browser import (INSTRUMENT, NetworkProbe, browser_error_code, display_measurement, origin,
                                       route_template, safe_request, server_timings, session_backend, summarize)


class BrowserProbeContracts(unittest.TestCase):
    def test_temporary_session_uses_an_enabled_owner_backend(self):
        self.assertEqual(session_backend(['runtime.authentication.PublicKeyBackend']),
                         'runtime.authentication.PublicKeyBackend')
        self.assertEqual(session_backend(['django.contrib.auth.backends.ModelBackend']),
                         'django.contrib.auth.backends.ModelBackend')
        with self.assertRaises(ValueError):
            session_backend(['unrelated.AuthenticationBackend'])

    def test_route_templates_drop_ids_queries_and_unknown_paths(self):
        self.assertEqual(route_template("http://localhost/api/v1/snapshots/private-id/thumbnail?q=secret"),
                         "/api/v1/snapshots/:id/thumbnail")
        self.assertEqual(route_template("http://localhost/unknown-personal-folder"), "/:other")
        self.assertEqual(route_template("http://localhost/api/v1/custom-private-name"), "/api/v1/:other")
        self.assertEqual(route_template("http://localhost/assets/private-name-a892.js"), "/assets/*.js")

    def test_only_same_origin_get_head_can_reach_server(self):
        for method in ("GET", "HEAD"):
            self.assertTrue(safe_request(method, "http://127.0.0.1:8001/library", "http://127.0.0.1:8001"))
        for method in ("POST", "PATCH", "PUT", "DELETE", "OPTIONS"):
            self.assertFalse(safe_request(method, "http://127.0.0.1:8001/api/v1/bookmarks", "http://127.0.0.1:8001"))
        self.assertFalse(safe_request("GET", "https://external.example/", "http://127.0.0.1:8001"))
        self.assertFalse(safe_request("GET", "http://127.0.0.1:8000/", "http://127.0.0.1:8001"))

    def test_base_origin_cannot_expose_local_session_to_remote(self):
        self.assertEqual(origin("http://127.0.0.1:8001/"), "http://127.0.0.1:8001")
        for value in ("https://external.example", "http://secret@localhost", "http://localhost/library", "http://localhost?q=secret"):
            with self.assertRaises(ValueError):
                origin(value)

    def test_summary_preserves_samples_and_does_not_invent_empty_percentiles(self):
        self.assertEqual(summarize([])["p95_ms"], None)
        data = summarize([5, 2, 9, None, -1, float("nan")])
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["samples_ms"], [2, 5, 9])
        self.assertEqual(data["p95_ms"], 9)

    def test_server_timing_drops_arbitrary_descriptions_and_unknown_names(self):
        self.assertEqual(server_timings('app;dur=25.5, db;dur=4;desc="private query", secret;dur=9'),
                         {"app": 25.5, "db": 4})
        self.assertEqual(server_timings('app;dur=25.5, sql;dur=4'), {'app': 25.5, 'sql': 4})

    def test_browser_exception_classification_never_returns_messages_or_urls(self):
        cases = [
            (Exception('page.evaluate: Execution context was destroyed http://private/secret'), 'execution_context_destroyed'),
            (Exception('APIRequestContext.head: Parse Error: Expected HTTP/ cookie=session-secret'), 'http_parser_expected_status_line'),
            (Exception('APIRequestContext.head: socket hang up secret'), 'connection_closed'),
            (Exception('APIRequestContext.head: read ECONNRESET'), 'connection_reset'),
            (Exception('unknown message http://private/secret'), 'browser_operation_error'),
        ]
        for error, expected in cases:
            self.assertEqual(browser_error_code(error), expected)

    def test_network_rollup_counts_requests_without_calling_sum_wall_time(self):
        probe = NetworkProbe("http://127.0.0.1:8001")
        probe.rows = [
            {"route": "/api/v1/bookmarks", "type": "Fetch", "status": 200, "duration_ms": 4, "wire_bytes": 100},
            {"route": "/api/v1/bookmarks", "type": "Fetch", "status": 200, "duration_ms": 6, "wire_bytes": 30},
            {"route": "/assets/*.js", "type": "Script", "status": 200, "duration_ms": 2, "wire_bytes": 50},
        ]
        rows = probe.snapshot(0)
        api = next(row for row in rows if row["route"] == "/api/v1/bookmarks")
        self.assertEqual(api["request_count"], 2)
        self.assertEqual(api["wire_bytes"], 130)
        self.assertEqual(api["duration"]["samples_ms"], [4, 6])
        self.assertNotIn("total_duration_ms", api)


class BrowserProbeIntegration(unittest.TestCase):
    def test_real_headless_display_and_method_guard(self):
        """A fixture POST must never reach even the isolated HTTP server."""
        class Fixture(BaseHTTPRequestHandler):
            writes = 0

            def log_message(self, *_):
                pass

            def do_POST(self):
                Fixture.writes += 1
                self.send_error(500)

            def do_GET(self):
                body = b'''<!doctype html><main><article><div data-preview-container data-preview-state="loading"></div>
                <div><img data-preview-image="fixture" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='40'%3E%3Crect width='40' height='40' fill='blue'/%3E%3C/svg%3E"></div></article></main>
                <script>fetch('/blocked-write',{method:'POST',body:'never send'}).catch(()=>{window.blockChecked=true;});
                const image=document.querySelector('img');image.decode().then(()=>{document.querySelector('[data-preview-container]').dataset.previewState='ready';});</script>'''
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        async def run():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[2] / "tools" / "browsers"))
            from playwright.async_api import async_playwright
            base = "http://127.0.0.1:" + str(server.server_port)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context()
                    await context.add_init_script(INSTRUMENT)
                    page = await context.new_page()
                    probe = NetworkProbe(base)
                    await probe.attach(context, page)
                    await page.goto(base + "/library")
                    await page.wait_for_function("() => window.blockChecked && window.__performanceProbe.images.some(i=>i.paint_opportunity_ms !== null)")
                    data = await display_measurement(page)
                    self.assertTrue(data["instrumented"])
                    self.assertEqual(data["preview"]["success"], 1)
                    self.assertGreater(data["preview"]["first_paint_opportunity_ms"], 0)
                    self.assertTrue(data["preview"]["image_samples"][0]["natural_valid"])
                    self.assertEqual(Fixture.writes, 0)
                    self.assertEqual(sum(probe.blocked.values()), 1)
                    await context.close()
                finally:
                    await browser.close()
        try:
            asyncio.run(run())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
