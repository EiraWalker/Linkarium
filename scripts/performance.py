"""Read-only performance collection, resource sampling and repeatable reports."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import UUID
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "backend"))
from performance_report import write_report
from performance_resources import ResourceSampler
from runtime.performance import sanitize_fields

SAFE_FIELDS = {"schema", "schema_version", "timestamp", "pid", "operation", "record_type", "trace_id", "span_id", "parent_span_id",
    "duration_ms", "elapsed_ms", "process_cpu_ms", "outcome", "exception_type", "route", "job_id", "group_id", "bookmark_id", "attempt_id",
    "maintenance_id", "version_id", "request_id", "category", "kind", "variant", "stage", "status", "operation_type", "result_state",
    "error_code", "generation", "method", "count", "total", "processed", "byte_size", "response_bytes", "queue_ms", "wait_ms", "execution_ms",
    "retry_after_ms", "attempt_no", "retry_no", "status_code", "http_status", "sql_count", "sql_ms", "width", "height", "batch_size", "cache_hit"}


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def read_events(log_dir, since, *, byte_limit=64 * 1024 * 1024, row_limit=100000):
    events, consumed, malformed, excluded, unreadable = [], 0, 0, 0, 0
    paths = []
    for path in Path(log_dir).glob("perf-*.jsonl*"):
        try:
            paths.append((path.stat().st_mtime, path))
        except OSError:
            unreadable += 1
    paths.sort(key=lambda item: item[0], reverse=True)
    route_parts = set("api v1 session login logout runtime status data clear reset capture-network settings bookmarks folders tags job-groups jobs snapshots ai-profiles classification batches proposals accept reject imports preview execute review-notified backups restores validate download pre-backup maintenance-operations lookup refresh refresh-preview delete-preview bulk bulk-delete cancel retry test thumbnail all :id :asset :unmatched assets health live ready library transfers".split())
    for _, path in paths:
        if path.is_symlink() or not re.fullmatch(r"perf-\d+\.jsonl(?:\.[123])?", path.name):
            continue
        if consumed >= byte_limit or len(events) >= row_limit:
            break
        try:
            incoming = path.open("rb")
        except OSError:
            unreadable += 1
            continue
        with incoming:
            # Bound memory even if an externally edited log has no newlines.
            while consumed < byte_limit and len(events) < row_limit:
                try:
                    line = incoming.readline(32769)
                except OSError:
                    unreadable += 1
                    break
                if not line:
                    break
                consumed += len(line)
                if len(line) > 32768:
                    malformed += 1
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError
                    at = _timestamp(row.get("timestamp"))
                    if at is None or at < since:
                        excluded += 1
                        continue
                    # Writer is already strict; defend exports against arbitrary
                    # fields in manually edited or older diagnostic files too.
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", str(row.get("operation", ""))):
                        raise ValueError
                    clean = {**sanitize_fields(row), "timestamp": at.isoformat(), "operation": row["operation"]}
                    for key in ("trace_id", "span_id", "parent_span_id"):
                        if row.get(key):
                            try:
                                clean[key] = str(UUID(str(row[key])))
                            except (ValueError, TypeError):
                                pass
                    for key in ("record_type", "outcome", "exception_type"):
                        value = row.get(key)
                        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value):
                            clean[key] = value
                    if isinstance(row.get("pid"), int) and row["pid"] > 0:
                        clean["pid"] = row["pid"]
                    route = row.get("route")
                    if isinstance(route, str) and len(route) <= 180 and all(part in route_parts for part in route.strip('/').split('/') if part):
                        clean["route"] = route
                    clean["schema"] = 1
                    events.append(clean)
                except (ValueError, UnicodeError, TypeError):
                    malformed += 1
    return events, {"bytes_read": consumed, "malformed_rows": malformed, "unreadable_files": unreadable, "outside_window": excluded,
        "truncated": consumed >= byte_limit or len(events) >= row_limit, "since": since.isoformat(), "event_count": len(events)}


def _json(path, body):
    Path(path).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def collect(options):
    from performance_inventory import collect_inventory
    started = datetime.now(timezone.utc)
    target = options.output or ROOT / "output" / "performance" / started.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d-%H%M%S-%f")
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=False)
    data_root = options.data_root.resolve()
    manifest = {"schema_version": 1, "mode": "observe_only", "started_at": started.isoformat(),
        "started_at_local": started.astimezone(ZoneInfo("Asia/Taipei")).isoformat(), "duration_seconds": options.duration,
        "interval_seconds": options.interval, "browser_requested": options.browser,
        "instrumentation_default": "disabled_unless_services_started_with_BOOKMARK_PERF_1",
        "modifications": "diagnostic files and temporary browser owner session only; no business requests submitted",
        "errors": []}
    _json(target / "run.json", manifest)
    def inventory(name):
        try:
            value = collect_inventory(data_root)
        except Exception as error:
            value = {"schema_version": 1, "probe": "inventory", "error": type(error).__name__}
            manifest["errors"].append({"probe": name, "type": type(error).__name__})
        _json(target / name, value)
    inventory("inventory.json")
    resource_data = {"schema_version": 1, "probe": "resources", "samples": [], "errors": []}
    stop = threading.Event()
    def sample_resources():
        try:
            sampler = ResourceSampler(ROOT)
            deadline = time.monotonic() + options.duration
            while True:
                resource_data["samples"].append(sampler.sample())
                if time.monotonic() >= deadline or stop.wait(min(options.interval, max(0, deadline - time.monotonic()))):
                    break
        except Exception as error:
            resource_data["errors"].append(type(error).__name__)
    sampling = threading.Thread(target=sample_resources, daemon=True)
    sampling.start()
    try:
        if options.browser:
            command = [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "performance_browser.py"),
                "--base-url", options.base_url, "--output", str(target / "browser.json"), "--timeout", str(options.browser_timeout),
                "--devices", options.devices]
            if options.extra_scenarios:
                command.append("--extra-scenarios")
            environment = dict(os.environ)
            environment["BOOKMARK_DATA_ROOT"] = str(data_root)
            result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, check=False, env=environment)
            manifest["browser_exit_code"] = result.returncode
        # Sampling stops at its own deadline; a short browser probe does not
        # shorten the requested passive observation window.
        while sampling.is_alive():
            sampling.join(timeout=1)
    except KeyboardInterrupt:
        manifest["interrupted"] = True
    finally:
        stop.set()
        sampling.join(timeout=20)
        _json(target / "resources.json", resource_data)
        inventory("inventory-after.json")
        since = started - timedelta(minutes=options.lookback_minutes)
        events, coverage = read_events(options.log_dir or data_root / "diagnostics" / "performance", since)
        with (target / "events.jsonl").open("w", encoding="utf-8") as output:
            for row in events:
                output.write(json.dumps(row, ensure_ascii=True) + "\n")
        manifest.update(finished_at=datetime.now(timezone.utc).isoformat(), log_coverage=coverage)
        _json(target / "run.json", manifest)
        write_report(target, baseline=options.baseline)
    print(json.dumps({"output": str(target), "report": str(target / "report.html"),
        "event_count": len(events), "resource_samples": len(resource_data["samples"]), "errors": manifest["errors"]}, ensure_ascii=False))
    return 0 if not manifest.get("errors") and not resource_data["errors"] and not manifest.get("browser_exit_code") else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    monitor = sub.add_parser("collect", help="Collect existing traces, read-only inventory, resources and optional isolated browser timings")
    monitor.add_argument("--data-root", type=Path, default=ROOT / "data")
    monitor.add_argument("--log-dir", type=Path)
    monitor.add_argument("--output", type=Path)
    monitor.add_argument("--duration", type=float, default=30)
    monitor.add_argument("--interval", type=float, default=2)
    monitor.add_argument("--lookback-minutes", type=float, default=60)
    monitor.add_argument("--browser", action="store_true")
    monitor.add_argument("--base-url", default="http://127.0.0.1:8001")
    monitor.add_argument("--browser-timeout", type=float, default=60)
    monitor.add_argument("--devices", default="desktop,mobile")
    monitor.add_argument("--extra-scenarios", action="store_true")
    monitor.add_argument("--baseline", type=Path)
    report = sub.add_parser("report", help="Regenerate reports from a saved collection; never calls the application")
    report.add_argument("run_dir", type=Path)
    report.add_argument("--baseline", type=Path)
    options = parser.parse_args(argv)
    if options.command == "report":
        write_report(options.run_dir, baseline=options.baseline)
        return 0
    if not all(math.isfinite(v) for v in (options.duration, options.interval, options.lookback_minutes, options.browser_timeout)) or not (0 <= options.duration <= 86400 and .25 <= options.interval <= 60 and 0 <= options.lookback_minutes <= 10080 and 1 <= options.browser_timeout <= 600):
        parser.error("duration: 0–86400; interval: 0.25–60; lookback: 0–10080 minutes; browser timeout: 1–600")
    return collect(options)


if __name__ == "__main__":
    raise SystemExit(main())
