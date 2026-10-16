"""Measure the existing UI in a disposable browser; never submit business writes.

Thumbnail GETs can prioritize missing derived images through the existing API.
No capture, refresh, classification, import, or approval action is invoked.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Use an explicit loopback origin without credentials or a path")
    return value.rstrip("/")


def route_template(url):
    """Allowlisted route shapes only: never preserve arbitrary paths or queries."""
    if urlsplit(url).scheme in {"blob", "data"}:
        return ":derived-image"
    path = urlsplit(url).path
    if path in {"/", "/login", "/library", "/settings", "/transfers", "/health/ready", "/health/live"}:
        return path
    if path.startswith("/assets/"):
        suffix = Path(path).suffix.lower()
        return "/assets/*" + (suffix if suffix in {".js", ".css", ".woff", ".woff2", ".svg", ".png", ".ico"} else "")
    patterns = (
        (r"/api/v1/snapshots/[^/]+/(thumbnail|preview)", r"/api/v1/snapshots/:id/\1"),
        (r"/api/v1/bookmarks/[^/]+/snapshots", "/api/v1/bookmarks/:id/snapshots"),
        (r"/api/v1/bookmarks/[^/]+", "/api/v1/bookmarks/:id"),
        (r"/api/v1/job-groups/[^/]+", "/api/v1/job-groups/:id"),
    )
    for pattern, replacement in patterns:
        if re.fullmatch(pattern, path):
            return re.sub(pattern, replacement, path)
    if path in {"/api/v1/session", "/api/v1/bookmarks", "/api/v1/folders", "/api/v1/tags", "/api/v1/settings",
                "/api/v1/job-groups", "/api/v1/classification/proposals", "/api/v1/ai-profiles",
                "/api/v1/capture-network", "/api/v1/workflows", "/api/v1/runtime"}:
        return path
    return "/api/v1/:other" if path.startswith("/api/v1/") else "/:other"


def safe_request(method, url, base):
    return method in {"GET", "HEAD"} and urlsplit(url)[:2] == urlsplit(base)[:2]


def summarize(values):
    values = sorted(float(v) for v in values if isinstance(v, (float, int)) and math.isfinite(v) and v >= 0)
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None, "samples_ms": []}
    return {"count": len(values), "p50_ms": round(values[math.ceil(len(values) * .5) - 1], 3),
            "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3), "max_ms": round(values[-1], 3),
            "samples_ms": [round(v, 3) for v in values]}


def server_timings(header):
    result = {}
    for item in (header or "").split(","):
        name = item.split(";", 1)[0].strip()
        match = re.search(r"(?:^|;)\s*dur=([\d.]+)", item)
        if name in {"app", "db", "sql", "serialize", "dispatch", "total"} and match:
            try:
                result[name] = float(match.group(1))
            except ValueError:
                pass
    return result


def browser_error_code(error):
    """Classify transient browser exceptions without persisting their URLs/text."""
    value = str(error).lower()
    for needle, code in (("execution context was destroyed", "execution_context_destroyed"),
                         ("cannot find context", "execution_context_missing"),
                         ("most likely because of a navigation", "navigation_interrupted_evaluation"),
                         ("target page, context or browser has been closed", "target_closed"),
                         ("iframe_load_failed", "iframe_load_failed"),
                         ("document_body_missing", "host_document_body_missing"),
                         ("expected http/", "http_parser_expected_status_line"),
                         ("parse error", "http_parse_error"),
                         ("socket hang up", "connection_closed"),
                         ("econnreset", "connection_reset"),
                         ("econnrefused", "connection_refused"),
                         ("enotfound", "name_resolution_failed"),
                         ("certificate", "tls_certificate_error")):
        if needle in value:
            return code
    if (isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower()
            or "timeout" in value or "timed out" in value):
        return "measurement_timeout"
    match = re.search(r"net::(err_[a-z_]+)", value)
    if match:
        return match.group(1).upper()
    return "browser_operation_error"


def session_backend(backends):
    """Use an owner backend the running application actually accepts."""
    for candidate in ("runtime.authentication.PublicKeyBackend", "django.contrib.auth.backends.ModelBackend"):
        if candidate in backends:
            return candidate
    raise ValueError("No supported owner session backend is configured")


@contextmanager
def temporary_owner_session(base):
    """Direct session creation avoids password/last_login changes and is deleted."""
    sys.path.insert(0, str(ROOT / "backend"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from django.conf import settings
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
    from django.contrib.sessions.backends.db import SessionStore
    owner = get_user_model().objects.get(username=settings.BOOKMARK_OWNER)
    session = SessionStore()
    session[SESSION_KEY] = str(owner.pk)
    session[BACKEND_SESSION_KEY] = session_backend(settings.AUTHENTICATION_BACKENDS)
    session[HASH_SESSION_KEY] = owner.get_session_auth_hash()
    session.set_expiry(900)
    session.save()
    state = {"deleted": False}
    try:
        yield {"name": settings.SESSION_COOKIE_NAME, "value": session.session_key, "url": base,
               "httpOnly": True, "sameSite": "Lax"}, state
    finally:
        key = session.session_key
        session.delete(key)
        state["deleted"] = not SessionStore().exists(key)


INSTRUMENT = r"""(() => {
  const probe = window.__performanceProbe = {longtasks: [], images: [], firstReady: null, allReady: null,
    selector:'main', phaseStart:0};
  const stamp = () => performance.now() - probe.phaseStart;
  try { new PerformanceObserver(list => {
    for (const e of list.getEntries()) probe.longtasks.push({start_ms:e.startTime, duration_ms:e.duration});
  }).observe({type:'longtask', buffered:true}); } catch {}
  let seen = new WeakSet();
  let queued = false;
  const measure = () => {
    queued = false;
    const nodes = [...document.querySelectorAll(probe.selector + ' [data-preview-container]')];
    const available = nodes.filter(e => e.dataset.previewState !== 'missing');
    const ready = available.filter(e => e.dataset.previewState === 'ready');
    if (ready.length && probe.firstReady === null) probe.firstReady = stamp();
    if (available.length && ready.length === available.length && probe.allReady === null)
      probe.allReady = stamp();
    for (const image of document.querySelectorAll(probe.selector + ' img[data-preview-image]')) {
      if (image.parentElement.hidden) continue;
      if (seen.has(image)) continue;
      seen.add(image);
      const row = {sample:probe.images.length + 1, observed_ms:stamp(), decode_ms:null,
        paint_opportunity_ms:null, natural_valid:false, onscreen:false};
      const resource = performance.getEntriesByType('resource').find(entry =>
        entry.name.includes('/snapshots/' + image.dataset.previewImage + '/thumbnail'));
      row.cached_before_action = !!resource && resource.responseEnd < probe.phaseStart;
      row.thumbnail_headers_ms = resource && !row.cached_before_action ? resource.responseStart - probe.phaseStart : null;
      row.thumbnail_body_end_ms = resource && !row.cached_before_action ? resource.responseEnd - probe.phaseStart : null;
      probe.images.push(row);
      image.decode().then(() => {
        row.decode_ms = stamp();
        row.natural_valid = image.naturalWidth > 0 && image.naturalHeight > 0;
        requestAnimationFrame(() => requestAnimationFrame(() => {
          row.paint_opportunity_ms = stamp();
          const r = image.getBoundingClientRect();
          row.onscreen = r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
            && r.right > 0 && r.left < innerWidth && !image.parentElement.hidden;
        }));
      }).catch(() => { row.decode_failed = true; });
    }
  };
  new MutationObserver(() => {
    if (!queued) { queued = true; queueMicrotask(measure); }
  }).observe(document, {subtree:true, childList:true, attributes:true,
    attributeFilter:['data-preview-state', 'src', 'hidden']});
  document.addEventListener('DOMContentLoaded', measure);
  probe.reset = selector => {
    probe.selector=selector;probe.phaseStart=performance.now();probe.firstReady=null;probe.allReady=null;
    probe.images=[];probe.longtasks=[];seen=new WeakSet();
  };
})();"""


class NetworkProbe:
    def __init__(self, base):
        self.base = base
        self.rows = []
        self.pending = {}
        self.blocked = Counter()
        self.tasks = set()
        self.bookmarks = []  # Private process memory only, never serialized.
        self.folders = []
        self.phase = 0

    def begin(self):
        self.phase += 1
        return self.phase

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda value: value.exception() if not value.cancelled() else None)

    async def attach(self, context, page):
        cdp = await context.new_cdp_session(page)
        self.cdp = cdp
        await cdp.send("Network.enable")
        await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

        async def paused(event):
            request = event["request"]
            if safe_request(request["method"], request["url"], self.base):
                await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            else:
                self.blocked[(request["method"], route_template(request["url"]))] += 1
                await cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
        cdp.on("Fetch.requestPaused", lambda event: self.task(paused(event)))

        def requested(event):
            request = event["request"]
            self.pending[event["requestId"]] = {"route": route_template(request["url"]),
                "method": request["method"], "type": event.get("type", "Other"), "start": event["timestamp"], "_phase": self.phase}
        cdp.on("Network.requestWillBeSent", requested)

        def responded(event):
            row = self.pending.get(event["requestId"])
            if row is None:
                return
            response = event["response"]
            headers = {key.lower(): value for key, value in response.get("headers", {}).items()}
            row.update(status=response.get("status"), from_disk_cache=response.get("fromDiskCache", False),
                       from_service_worker=response.get("fromServiceWorker", False),
                       headers_elapsed_ms=max(0, (event["timestamp"] - row["start"]) * 1000),
                       headers_ms=response.get("timing", {}).get("receiveHeadersEnd"),
                       server_timing=server_timings(headers.get("server-timing", "")))
            identifier = headers.get("x-request-id", "")
            if re.fullmatch(r"[a-zA-Z0-9-]{8,64}", identifier):
                row["request_id"] = identifier
        cdp.on("Network.responseReceived", responded)

        def finished(event):
            row = self.pending.pop(event["requestId"], None)
            if row:
                row["duration_ms"] = (event["timestamp"] - row.pop("start")) * 1000
                row["wire_bytes"] = max(0, event.get("encodedDataLength", 0))
                self.rows.append(row)
        cdp.on("Network.loadingFinished", finished)

        def failed(event):
            row = self.pending.pop(event["requestId"], None)
            if row:
                row.pop("start", None)
                row.update(failed=True, duration_ms=None, wire_bytes=0)
                self.rows.append(row)
        cdp.on("Network.loadingFailed", failed)

        async def metadata(response):
            route = route_template(response.url)
            if response.status == 200 and route in {"/api/v1/bookmarks", "/api/v1/folders"}:
                try:
                    data = (await response.json()).get("data", [])
                    if isinstance(data, list):
                        if route.endswith("bookmarks"):
                            self.bookmarks = data
                        else:
                            self.folders = data
                except Exception:
                    pass
        page.on("response", lambda response: self.task(metadata(response)))
        return cdp

    def snapshot(self, start):
        groups = defaultdict(list)
        rows = [row for row in self.rows if row.get("_phase", 0) == start]
        rows += [dict(row, inflight=True) for row in self.pending.values() if row.get("_phase", 0) == start]
        for row in rows:
            groups[(row["route"], row["type"])].append(row)
        result = []
        for (route, kind), rows in sorted(groups.items()):
            result.append({"route": route, "type": kind, "request_count": len(rows),
                "status_counts": dict(Counter(str(row.get("status", "failed")) for row in rows)),
                "duration": summarize([row.get("duration_ms") for row in rows]),
                "headers": summarize([row.get("headers_ms") for row in rows]),
                "wire_bytes": sum(row.get("wire_bytes", 0) for row in rows),
                "disk_cache_hits": sum(bool(row.get("from_disk_cache")) for row in rows),
                "failed": sum(bool(row.get("failed")) for row in rows),
                "inflight": sum(bool(row.get("inflight")) for row in rows),
                "request_samples": [{key: row[key] for key in ("method", "status", "duration_ms", "headers_elapsed_ms",
                    "wire_bytes", "from_disk_cache", "failed", "inflight", "server_timing", "request_id") if key in row} for row in rows],
                "server_samples": [{"request_id": row.get("request_id"), "timings_ms": row.get("server_timing", {})}
                                   for row in rows if row.get("server_timing") or row.get("request_id")]})
        return result


async def display_measurement(page):
    data = await page.evaluate("""() => {
      const p = window.__performanceProbe || {images:[], longtasks:[], selector:'main'};
      const states = {}; let onscreen = 0;
      for (const e of document.querySelectorAll(p.selector + ' [data-preview-container]')) {
        states[e.dataset.previewState] = (states[e.dataset.previewState] || 0) + 1;
        const r = e.getBoundingClientRect(); if (r.bottom > 0 && r.top < innerHeight) onscreen++;
      }
      const resources = performance.getEntriesByType('resource').map(e => ({name:e.name,
        type:e.initiatorType, start_ms:e.startTime, headers_ms:e.responseStart, body_end_ms:e.responseEnd,
        duration_ms:e.duration, transfer_bytes:e.transferSize, encoded_bytes:e.encodedBodySize, decoded_bytes:e.decodedBodySize}));
      const nav = performance.getEntriesByType('navigation')[0];
      return {instrumented:!!window.__performanceProbe, states, onscreen, cards:document.querySelectorAll('main article').length,
        navigation:nav ? {ttfb_ms:nav.responseStart, response_end_ms:nav.responseEnd,
          dom_content_loaded_ms:nav.domContentLoadedEventEnd, load_ms:nav.loadEventEnd} : null,
        paint:performance.getEntriesByType('paint').map(e=>({name:e.name, start_ms:e.startTime})),
        first_ready_ms:p.firstReady ?? null, all_ready_ms:p.allReady ?? null,
        longtasks:p.longtasks, images:p.images, resources};
    }""")
    resource_groups = defaultdict(list)
    for row in data.pop("resources"):
        row["route"] = route_template(row.pop("name"))
        resource_groups[(row["route"], row["type"])].append(row)
    resources = [{"route": route, "initiator": kind, "count": len(rows),
        "duration": summarize([row["duration_ms"] for row in rows]),
        "transfer_bytes": sum(row["transfer_bytes"] for row in rows),
        "encoded_bytes": sum(row["encoded_bytes"] for row in rows),
        "decoded_bytes": sum(row["decoded_bytes"] for row in rows),
        "samples": rows} for (route, kind), rows in sorted(resource_groups.items())]
    images = data.pop("images")
    states = data.pop("states")
    all_images = [row["paint_opportunity_ms"] for row in images if row["natural_valid"] and row["paint_opportunity_ms"] is not None]
    visible_images = [row["paint_opportunity_ms"] for row in images if row["onscreen"] and row["natural_valid"]
                      and row["paint_opportunity_ms"] is not None]
    count = sum(states.values())
    available = count - states.get("missing", 0)
    data.update(resources=resources, preview={"states": states, "containers": count, "available": available,
        "missing": states.get("missing", 0), "success": states.get("ready", 0),
        "pending": sum(states.get(key, 0) for key in ("queued", "loading", "generating")),
        "errors": sum(states.get(key, 0) for key in ("error", "timeout")),
        "first_ready_ms": data.pop("first_ready_ms"), "all_ready_ms": data.pop("all_ready_ms"),
        "first_paint_opportunity_ms": min(all_images, default=None),
        "all_paint_opportunity_ms": max(all_images) if len(all_images) == available and available else None,
        "first_onscreen_paint_opportunity_ms": min(visible_images, default=None),
        "last_onscreen_paint_opportunity_ms": max(visible_images, default=None), "image_samples": images})
    return data


async def settle(page, maximum=12):
    """Finish once each image is ready, a known pending generation, or a failure."""
    await page.wait_for_function("""() => document.querySelector('main') &&
      !document.querySelector('main[aria-busy="true"], main [aria-busy="true"]') &&
      (document.querySelector('main [data-preview-container]') ||
       document.querySelector('main h2,main h1'))""", timeout=maximum * 1000)
    await page.wait_for_function("""() => !document.querySelector(
      'main [data-preview-state="queued"], main [data-preview-state="loading"]')""", timeout=maximum * 1000)
    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


async def measure_navigation(page, probe, path, device, cache, timeout):
    start = time.perf_counter()
    index = probe.begin()
    result = {"device": device, "cache": cache, "scenario": "library", "route": "/library", "status": "complete"}
    try:
        async with asyncio.timeout(timeout):
            response = await page.goto(path, wait_until="domcontentloaded", timeout=timeout * 1000)
            result["http_status"] = response.status if response else None
            if urlsplit(page.url).path == "/login":
                result.update(status="failed", error="authentication_required")
            else:
                await settle(page, min(timeout, 20))
    except (TimeoutError, asyncio.TimeoutError):
        result.update(status="incomplete", error="measurement_timeout")
    except Exception as error:
        result.update(status="incomplete", error=type(error).__name__)
    try:
        result.update(await display_measurement(page))
        if not result.get("instrumented"):
            result.update(status="incomplete", error="instrumentation_unavailable")
    except Exception as error:
        result.update(status="incomplete", display_error=type(error).__name__)
    result["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
    result["network"] = probe.snapshot(index)
    result["inflight_requests"] = len(probe.pending)
    return result


async def extra_scenarios(page, probe, base, device):
    results = []
    root = page.locator('[data-preview-root]').first
    actions = [("scroll", lambda: root.evaluate("e => {e.scrollTop=e.scrollHeight;}"))]
    next_page = page.get_by_role("button", name="下一页", exact=True)
    if await next_page.count() and await next_page.is_enabled():
        actions.append(("pagination", lambda: next_page.click()))
        actions.append(("pagination_return", lambda: page.get_by_role("button", name="上一页", exact=True).click()))
    if probe.folders:
        folder = probe.folders[0].get("id", "")
        if re.fullmatch(r"[a-zA-Z0-9-]+", folder):
            actions.append(("folder", lambda: page.evaluate("url => {history.pushState({},'',url);dispatchEvent(new PopStateEvent('popstate'));}",
                f"/library?device={device}&folder={folder}")))
            actions.append(("folder_return", lambda: page.go_back(wait_until="domcontentloaded")))
    # Link navigation stays within the SPA and preserves its image session.
    actions.append(("settings", lambda: page.evaluate("() => {history.pushState({},'', '/settings');dispatchEvent(new PopStateEvent('popstate'));}")))
    actions.append(("settings_return", lambda: page.go_back(wait_until="domcontentloaded")))
    for name, action in actions:
        start, index = time.perf_counter(), probe.begin()
        result = {"scenario": name, "device": device, "status": "complete", "timing_origin": "action_start"}
        before = await page.evaluate("performance.now()")
        try:
            async with asyncio.timeout(8):
                await action()
                await settle(page, 6)
                preview = (await display_measurement(page))["preview"]
                result["preview_state_counts"] = {key: preview[key] for key in
                    ("states", "containers", "available", "missing", "success", "pending", "errors")}
                result["action_paint_opportunity_ms"] = (await page.evaluate("performance.now()")) - before
        except Exception as error:
            result.update(status="incomplete", error=type(error).__name__)
        result.update(duration_ms=round((time.perf_counter() - start) * 1000, 3), network=probe.snapshot(index))
        results.append(result)
    return results


async def display_scenarios(page, probe, device):
    """Time explicit image display when choosing another device or a detail."""
    results = []
    other = "mobile" if device == "desktop" else "desktop"
    actions = [("switch_device", lambda: page.get_by_role("button", name="手机快照" if other == "mobile" else "桌面快照", exact=True).click(), "main")]
    for name, action, scope in actions:
        start, index = time.perf_counter(), probe.begin()
        result = {"device": device, "target_variant": other, "scenario": name, "status": "complete",
                  "timing_origin": "action_start"}
        try:
            async with asyncio.timeout(12):
                await page.evaluate("selector => window.__performanceProbe.reset(selector)", scope)
                await action()
                await settle(page, 10)
                result["preview"] = (await display_measurement(page))["preview"]
        except Exception as error:
            result.update(status="incomplete", error=type(error).__name__)
        result.update(duration_ms=round((time.perf_counter() - start) * 1000, 3), network=probe.snapshot(index))
        results.append(result)
    detail_button = page.locator('main article button[aria-pressed]').first
    if await detail_button.count():
        start, index = time.perf_counter(), probe.begin()
        result = {"device": device, "scenario": "open_detail", "status": "complete", "timing_origin": "action_start"}
        try:
            async with asyncio.timeout(8):
                await page.evaluate("selector => window.__performanceProbe.reset(selector)", '[aria-label="网站详情"]')
                await detail_button.click()
                await page.wait_for_selector('[aria-label="网站详情"] [data-preview-container]', timeout=6000)
                await page.wait_for_function("""() => !document.querySelector(
                  '[aria-label="网站详情"] [data-preview-state="queued"], [aria-label="网站详情"] [data-preview-state="loading"]')""", timeout=6000)
                await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                result["preview"] = (await display_measurement(page))["preview"]
                await page.get_by_role("button", name="关闭详情", exact=True).click()
        except Exception as error:
            result.update(status="incomplete", error=type(error).__name__)
        result.update(duration_ms=round((time.perf_counter() - start) * 1000, 3), network=probe.snapshot(index))
        results.append(result)
    await page.evaluate("() => window.__performanceProbe.reset('main')")
    return results


async def html_sample(context, probe, base, device, maximum_bytes, timeout):
    candidates = [item.get("preview") for item in probe.bookmarks if item.get("preview")]
    candidates = [item for item in candidates if isinstance(item.get("byte_size"), int)
                  and 0 < item["byte_size"] <= maximum_bytes]
    result = {"device": device, "scenario": "html_archive", "status": "skipped",
              "max_bytes": maximum_bytes, "timing_definition": "sandbox iframe load plus two outer rAF; no pixel-presentation claim"}
    if not candidates:
        result["reason"] = "no_existing_snapshot_within_byte_cap"
        return result
    selected = min(candidates, key=lambda value: value["byte_size"])
    path = selected.get("preview_url", "")
    if not re.fullmatch(r"/api/v1/snapshots/[a-zA-Z0-9-]+/preview", path):
        result["reason"] = "invalid_preview_route"
        return result
    result["artifact_bytes"] = selected["byte_size"]
    result["snapshot_variant"] = selected.get("variant")
    result["host_mode"] = "service_json_health_endpoint"
    page = await context.new_page()
    html_probe = NetworkProbe(base)
    await html_probe.attach(context, page)
    stage = "head_request"
    index = html_probe.begin()
    started = time.perf_counter()
    contexts_cleared = []
    html_probe.cdp.on("Runtime.executionContextsCleared", lambda _: contexts_cleared.append(time.perf_counter()))
    await html_probe.cdp.send("Runtime.enable")
    try:
        async with asyncio.timeout(timeout):
            head_started = time.perf_counter()
            try:
                head = await context.request.head(base + path, timeout=timeout * 1000)
            finally:
                result["head_request_elapsed_ms"] = round((time.perf_counter() - head_started) * 1000, 3)
            size = int(head.headers.get("content-length", "0"))
            if head.status != 200 or not 0 < size <= maximum_bytes:
                result.update(reason="head_size_or_status_rejected", http_status=head.status)
                return result
            result["head_status"] = head.status
            result["head_content_length"] = size
            result["head_server_timing_ms"] = server_timings(head.headers.get("server-timing", ""))
            request_id = head.headers.get("x-request-id", "")
            if re.fullmatch(r"[a-zA-Z0-9-]{8,64}", request_id):
                result["head_request_id"] = request_id
            stage = "host_navigation"
            await page.goto(base + "/health/live", wait_until="domcontentloaded")
            stage = "iframe_evaluation"
            start = time.perf_counter()
            index = html_probe.begin()
            observed = await page.evaluate("""({url,width,height}) => new Promise((resolve,reject) => {
              if (!document.body) throw new Error('document_body_missing');
              document.body.replaceChildren(); const frame=document.createElement('iframe');
              frame.sandbox=''; frame.width=width;frame.height=height;
              const start=performance.now();
              frame.onload=()=> { const load=performance.now()-start;
                requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({iframe_load_ms:load,
                  outer_paint_opportunity_ms:performance.now()-start}))); };
              frame.onerror=()=>reject(new Error('iframe_load_failed'));
              frame.src=url;document.body.append(frame);
            })""", {"url": base + path, "width": selected.get("viewport_width", 1366),
                     "height": selected.get("viewport_height", 768)})
            network = html_probe.snapshot(index)
            stage = "verify_html_response"
            html_rows = [row for row in network if row["route"] == "/api/v1/snapshots/:id/preview"]
            successful = bool(html_rows) and all(set(row["status_counts"]) <= {"200", "304"} for row in html_rows)
            result.update(observed, status="complete" if successful else "incomplete",
                          duration_ms=round((time.perf_counter() - start) * 1000, 3),
                          network=network, scripts_disabled=True)
            if not successful:
                result["error"] = "html_response_not_successful"
    except Exception as error:
        result.update(status="incomplete", error=type(error).__name__, error_code=browser_error_code(error), stage=stage)
    finally:
        result["network"] = html_probe.snapshot(index)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["execution_context_resets"] = len(contexts_cleared)
        result["blocked_requests"] = [{"method": method, "route": route, "count": count}
            for (method, route), count in html_probe.blocked.items()]
        await page.close()
    return result


async def collect(base, cookie, devices, timeout=60, extra=False, html_samples=1, html_max_bytes=2 * 1024 * 1024):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "tools" / "browsers"))
    from playwright.async_api import async_playwright
    report = {"runs": [], "scenarios": [], "html_samples": [], "blocked_requests": [], "browser_errors": 0}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        report["browser_version"] = browser.version
        try:
            for device in devices:
                context = await browser.new_context(viewport={"width": 390 if device == "mobile" else 1440,
                    "height": 844 if device == "mobile" else 1000}, device_scale_factor=1,
                    is_mobile=device == "mobile", has_touch=device == "mobile", service_workers="block")
                await context.add_cookies([cookie])
                await context.add_init_script(INSTRUMENT)
                page = await context.new_page()
                page.on("pageerror", lambda error: report.__setitem__("browser_errors", report["browser_errors"] + 1))
                probe = NetworkProbe(base)
                cdp = await probe.attach(context, page)
                await cdp.send("Network.clearBrowserCache")
                await cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
                try:
                    # One bounded budget per viewport, including optional probes.
                    async with asyncio.timeout(timeout):
                        for cache in ("cold", "warm"):
                            report["runs"].append(await measure_navigation(page, probe,
                                base + "/library?device=" + device, device, cache, min(timeout / 2, 25)))
                        if html_samples:
                            report["html_samples"].append(await html_sample(context, probe, base, device,
                                html_max_bytes, min(timeout / 3, 15)))
                        report["scenarios"].extend(await display_scenarios(page, probe, device))
                        if extra:
                            report["scenarios"].extend(await extra_scenarios(page, probe, base, device))
                except TimeoutError:
                    report["scenarios"].append({"device": device, "scenario": "device_budget", "status": "incomplete",
                                                "error": "measurement_timeout"})
                finally:
                    report["blocked_requests"].extend({"device": device, "method": method, "route": route, "count": count}
                        for (method, route), count in probe.blocked.items())
                    await context.close()
        finally:
            await browser.close()
    return report


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--base-url", type=origin, default="http://127.0.0.1:8001")
    value.add_argument("--output", type=Path, required=True, help="JSON file (normally RUN/browser.json)")
    value.add_argument("--timeout", type=float, default=60, help="Total seconds per viewport, including optional scenarios")
    value.add_argument("--devices", default="desktop,mobile")
    value.add_argument("--extra-scenarios", action="store_true", help="Also observe scrolling, pages, folder and settings navigation")
    value.add_argument("--html-sample", type=int, choices=(0, 1), default=1, help="Existing HTML sample per viewport; 0 disables")
    value.add_argument("--html-max-bytes", type=int, default=2 * 1024 * 1024)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    devices = args.devices.split(",")
    if not devices or any(value not in {"desktop", "mobile"} for value in devices) or not 1 <= args.timeout <= 600:
        raise SystemExit("devices must be desktop/mobile; timeout must be 1..600")
    if not 1 <= args.html_max_bytes <= 96 * 1024 * 1024:
        raise SystemExit("html-max-bytes must be 1..100663296")
    report = {"schema_version": 1, "probe": "browser", "started_at": datetime.now(timezone.utc).isoformat(),
        "base_origin": args.base_url, "status": "incomplete", "constraints": {"headless": True,
            "isolated_browser": True, "GET_HEAD_only": True, "temporary_session_deleted": False,
            "thumbnail_GET_side_effect": "missing derived image requests may prioritize existing offline HTML conversion",
            "cache_policy": "cold=fresh isolated context+cleared isolated browser cache; warm=same context reload with HTTP cache enabled",
            "timeout_seconds_per_device": args.timeout, "mobile": "Chromium emulation, not a physical phone",
            "display_time_definition": "navigation to thumbnail headers/body end, image decode, preview ready, then two outer rAF paint opportunities; no hardware presentation proof",
            "privacy": "allowlisted route templates, numeric counts/timings only; no titles, URLs, search terms, bodies or session secrets"}}
    session_state = {"deleted": False}
    try:
        with temporary_owner_session(args.base_url) as (cookie, session_state):
            report.update(asyncio.run(collect(args.base_url, cookie, devices, args.timeout,
                args.extra_scenarios, args.html_sample, args.html_max_bytes)))
        measurements = report["runs"] + report["scenarios"] + report["html_samples"]
        report["status"] = "complete" if report["runs"] and all(row.get("status") in {"complete", "skipped"}
            for row in measurements) else "incomplete"
    except KeyboardInterrupt:
        report["error"] = "interrupted"
    except Exception as error:
        report.update(status="failed", error=type(error).__name__)
    finally:
        report["constraints"]["temporary_session_deleted"] = session_state["deleted"]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output), "runs": len(report.get("runs", [])),
                      "temporary_session_deleted": session_state["deleted"]}, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
