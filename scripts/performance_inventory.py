"""Collect existing timing evidence without initializing Django or executing work.

Database connections use mode=ro plus query_only. This probe never claims jobs,
fetches pages, calls an AI provider, creates previews, or mutates library state.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"queued", "running", "retry_wait"}
MAX_DETAIL = 5000
MAX_CAPTURE_FILE_BYTES = 256 * 1024
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
GENERATION = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
CAPTURE_PHASES = {"starting", "network_setup", "url_validation", "browser_launch",
                  "navigation", "readiness", "freeze", "rewrite", "archive", "replay", "replay_check"}


@contextmanager
def read_database(path):
    """No fallback to a writable/create-capable SQLite connection is permitted."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def elapsed(start, end):
    a, b = timestamp(start), timestamp(end)
    if a is None or b is None or b < a:
        return None
    return round((b - a).total_seconds() * 1000, 3)


def distribution(values):
    values = sorted(float(value) for value in values if value is not None and math.isfinite(value) and value >= 0)
    if not values:
        return {"count": 0, "min_ms": None, "median_ms": None, "p95_ms": None, "max_ms": None, "sum_ms": 0}
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    return {"count": len(values), "min_ms": round(values[0], 3), "median_ms": round(median, 3),
            "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3),
            "max_ms": round(values[-1], 3), "sum_ms": round(sum(values), 3)}


def _rows(connection, table, fields):
    # All table/field names originate from the static calls below, never user data.
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    selected = [name for name in fields if name in present]
    return [dict(row) for row in connection.execute(f"SELECT {','.join(selected)} FROM {table}")] if selected else []


def _counter(rows, field):
    return dict(Counter(str(row.get(field) or "unknown") for row in rows))


def _file_inventory(paths):
    sizes, skipped = [], 0
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                skipped += 1
                continue
            sizes.append(path.stat().st_size)
        except OSError:
            skipped += 1
    return {"count": len(sizes), "bytes": sum(sizes), "largest_bytes": max(sizes, default=0), "skipped": skipped}


def _attempt_evidence(jobs, attempts, now):
    by_job = defaultdict(list)
    for attempt in attempts:
        by_job[attempt["job_id"]].append(attempt)
    job_by_id = {job["id"]: job for job in jobs}
    details, initial_waits, retry_waits, run_times = [], [], [], []
    by_kind = defaultdict(list)
    for job_id, items in by_job.items():
        items.sort(key=lambda row: row.get("attempt_no", 0))
        previous = None
        for attempt in items:
            job = job_by_id.get(job_id, {})
            start, finish = attempt.get("created_at"), attempt.get("finished_at")
            wait = elapsed(previous.get("finished_at"), start) if previous else elapsed(job.get("created_at"), start)
            duration = elapsed(start, finish)
            (retry_waits if previous else initial_waits).append(wait)
            run_times.append(duration)
            by_kind[job.get("kind", "unknown")].append(duration)
            details.append({"attempt_id": attempt["id"], "job_id": job_id, "kind": job.get("kind"),
                "variant": job.get("variant"), "attempt_no": attempt.get("attempt_no"), "state": attempt.get("state"),
                "created_at": start, "finished_at": finish, "execution_ms": duration,
                "wait_since_previous_finish_ms" if previous else "initial_queue_ms": wait,
                "running_age_ms": elapsed(start, now) if attempt.get("state") == "running" else None,
                "duration_missing": duration is None})
            previous = attempt
    details.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return {"total": len(attempts), "by_state": _counter(attempts, "state"),
            "execution": distribution(run_times), "initial_queue": distribution(initial_waits),
            "between_attempts": distribution(retry_waits),
            "execution_by_kind": {kind: distribution(values) for kind, values in by_kind.items()},
            "details": details[:MAX_DETAIL], "details_truncated": max(0, len(details) - MAX_DETAIL),
            "running_with_nonrunning_job": sum(row.get("state") == "running" and
                job_by_id.get(row.get("job_id"), {}).get("state") != "running" for row in attempts),
            "missing_finished_at": sum(not row.get("finished_at") for row in attempts)}, by_job


def _group_evidence(groups, members, jobs, by_job, now):
    job_by_id = {job["id"]: job for job in jobs}
    group_members = defaultdict(list)
    for member in members:
        group_members[member["group_id"]].append(member["job_id"])
    details = []
    for group in groups:
        members_jobs = [job_by_id[job_id] for job_id in set(group_members[group["id"]]) if job_id in job_by_id]
        finished = []
        complete = bool(members_jobs) and all(job.get("state") not in ACTIVE for job in members_jobs)
        for job in members_jobs:
            ends = [attempt["finished_at"] for attempt in by_job[job["id"]] if timestamp(attempt.get("finished_at"))]
            if not ends:
                complete = False
            else:
                finished.append(max(ends, key=timestamp))
                latest = max(by_job[job["id"]], key=lambda attempt: attempt.get("attempt_no", 0))
                if latest.get("state") != job.get("state") or not timestamp(latest.get("finished_at")):
                    # Cancellation can happen after a previously failed attempt.
                    # That failure's end cannot stand in for the cancellation time.
                    complete = False
        end = max(finished, key=timestamp) if complete and finished else None
        details.append({"group_id": group["id"], "kind": group.get("kind"), "created_at": group.get("created_at"),
            "requested_count": group.get("requested_count"), "job_count": len(members_jobs),
            "by_state": _counter(members_jobs, "state"), "last_attempt_finished_at": max(finished, key=timestamp) if finished else None,
            "derived_finished_at": end, "wall_ms": elapsed(group.get("created_at"), end),
            "active_age_ms": elapsed(group.get("created_at"), now) if any(job.get("state") in ACTIVE for job in members_jobs) else None,
            "duration_missing": end is None,
            "completion_basis": "all_members_terminal_and_have_finished_attempts" if end else "no_reliable_complete_timestamp"})
    details.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return {"total": len(groups), "details": details[:MAX_DETAIL],
            "details_truncated": max(0, len(details) - MAX_DETAIL),
            "wall": distribution(row["wall_ms"] for row in details)}


def _capture_evidence(data_root, attempt_ids):
    """Reuse capture's cumulative stage clocks; adjacent differences are intervals.

    Stages are emission markers, not necessarily exclusive pipeline phases. The
    last interval of a running/incomplete trace is deliberately left unknown.
    """
    durations, by_stage, by_phase = [], defaultdict(list), defaultdict(list)
    details, malformed, skipped, linked = [], 0, 0, 0
    paths = sorted((data_root / "diagnostics" / "capture").glob("*.json"))
    for path in paths:
        try:
            identity = UUID(path.stem).hex
            if path.is_symlink() or path.stat().st_size > MAX_CAPTURE_FILE_BYTES:
                skipped += 1
                continue
            trace = json.loads(path.read_text("utf-8"))
            if not isinstance(trace, dict):
                raise ValueError
            total = trace.get("elapsed_seconds")
            total_ms = round(float(total) * 1000, 3) if isinstance(total, (float, int)) and math.isfinite(total) and total >= 0 else None
            state = trace.get("state")
            events = trace.get("events", [])
            if not isinstance(events, list):
                raise ValueError
            segments = []
            usable = [item for item in events[:80] if isinstance(item, dict) and TOKEN.fullmatch(str(item.get("stage", "")))
                      and isinstance(item.get("elapsed_seconds"), (int, float)) and math.isfinite(item["elapsed_seconds"])
                      and item["elapsed_seconds"] >= 0]
            for index, event in enumerate(usable):
                start_ms = event["elapsed_seconds"] * 1000
                end_ms = usable[index + 1]["elapsed_seconds"] * 1000 if index + 1 < len(usable) else (
                    total_ms if state not in {"running", "starting"} else None)
                duration = round(end_ms - start_ms, 3) if end_ms is not None and end_ms >= start_ms else None
                segments.append({"stage": event["stage"], "start_ms": round(start_ms, 3), "duration_ms": duration})
                if duration is not None:
                    by_stage[event["stage"]].append(duration)
            # High-level phase markers omit nested image/frame/document events.
            # Their tail still includes any shutdown work before trace completion.
            phases = []
            phase_markers = [event for event in usable if event["stage"] in CAPTURE_PHASES]
            for index, event in enumerate(phase_markers):
                start_ms = event["elapsed_seconds"] * 1000
                end_ms = phase_markers[index + 1]["elapsed_seconds"] * 1000 if index + 1 < len(phase_markers) else (
                    total_ms if state not in {"running", "starting"} else None)
                duration = round(end_ms - start_ms, 3) if end_ms is not None and end_ms >= start_ms else None
                phases.append({"stage": event["stage"], "start_ms": round(start_ms, 3), "duration_ms": duration})
                if duration is not None:
                    by_phase[event["stage"]].append(duration)
            is_linked = identity in attempt_ids
            linked += is_linked
            durations.append(total_ms)
            details.append({"attempt_id": str(UUID(identity)), "linked_to_active_library": is_linked,
                "started_at": trace.get("started_at") if timestamp(trace.get("started_at")) else None,
                "state": state if isinstance(state, str) and TOKEN.fullmatch(state) else "unknown",
                "variant": trace.get("variant") if trace.get("variant") in {"desktop", "mobile"} else None,
                "elapsed_ms": total_ms, "segments": segments, "phases": phases,
                "incomplete": state in {"running", "starting"},
                "timeout_seconds": trace.get("timeout_seconds") if isinstance(trace.get("timeout_seconds"), int) else None,
                "max_bytes": trace.get("max_bytes") if isinstance(trace.get("max_bytes"), int) else None})
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            malformed += 1
    details.sort(key=lambda row: row.get("started_at") or "", reverse=True)
    return {"files_seen": len(paths), "usable": len(details), "linked_attempts": linked,
            "malformed": malformed, "skipped": skipped, "execution": distribution(durations),
            "marker_interval_by_stage": {stage: distribution(values) for stage, values in by_stage.items()},
            "major_phase_intervals": {stage: distribution(values) for stage, values in by_phase.items()},
            "major_phase_semantics": "interval_until_next_major_marker_or_trace_completion_including_nested_events_and_final_shutdown",
            "interval_semantics": "adjacent_cumulative_event_markers_not_exclusive_pipeline_phases",
            "observed_budgets": {"timeout_seconds": sorted({row["timeout_seconds"] for row in details if row["timeout_seconds"] is not None}),
                                 "max_bytes": sorted({row["max_bytes"] for row in details if row["max_bytes"] is not None})},
            "includes_unlinked_historical_files": True,
            "details": details[:MAX_DETAIL], "details_truncated": max(0, len(details) - MAX_DETAIL)}


def collect_inventory(data_root):
    started = time.perf_counter()
    data_root = Path(data_root).resolve()
    now = datetime.now(timezone.utc).isoformat()
    with read_database(data_root / "state.sqlite3") as state:
        runtime_rows = _rows(state, "runtime_runtimestate", ["id", "active_generation", "library_epoch", "maintenance_mode", "worker_heartbeat"])
        runtime = next((row for row in runtime_rows if row.get("id") == 1), None)
        operations = _rows(state, "runtime_maintenanceoperation", ["id", "kind", "state", "stage", "created_at", "updated_at"])
    if runtime is None or not GENERATION.fullmatch(str(runtime.get("active_generation", ""))):
        raise ValueError("No valid active generation")
    generation_dir = data_root / "generations" / runtime["active_generation"]
    if generation_dir.is_symlink() or not generation_dir.resolve().is_relative_to(data_root):
        raise ValueError("Active generation path is unsafe")
    with read_database(generation_dir / "library.sqlite3") as db:
        jobs = _rows(db, "library_job", ["id", "kind", "variant", "state", "available_at", "created_at", "attempt_count", "active_attempt_id"])
        attempts = _rows(db, "library_jobattempt", ["id", "job_id", "attempt_no", "state", "created_at", "finished_at", "heartbeat_at", "lease_expires_at"])
        groups = _rows(db, "library_jobgroup", ["id", "kind", "requested_count", "created_at"])
        members = _rows(db, "library_jobgroupmember", ["group_id", "job_id"])
        imports = _rows(db, "library_importbatch", ["id", "state", "stage", "created_at", "total", "valid", "capture_group_id", "classification_group_id", "ai_status"])
        versions = _rows(db, "library_snapshotversion", ["id", "variant", "artifact_id", "viewport_width", "viewport_height", "captured_at"])
        artifacts = _rows(db, "library_artifact", ["sha256", "byte_size"])
        captures = _rows(db, "library_capturestate", ["variant", "current_version_id", "failure_count", "last_error_code"])
        bookmark_count = db.execute("SELECT COUNT(*) FROM library_bookmark").fetchone()[0]
        folder_count = db.execute("SELECT COUNT(*) FROM library_folder").fetchone()[0]
    attempt_report, by_job = _attempt_evidence(jobs, attempts, now)
    group_report = _group_evidence(groups, members, jobs, by_job, now)
    group_by_id = {row["group_id"]: row for row in group_report["details"]}
    current_ids = {row["current_version_id"] for row in captures if row.get("current_version_id")}
    cache_root = generation_dir / "previews" / "v1"
    cached, current_cached = 0, 0
    for version in versions:
        digest, variant = version.get("artifact_id", ""), version.get("variant", "")
        if re.fullmatch(r"[0-9a-f]{64}", digest) and variant in {"desktop", "mobile"}:
            target = cache_root / f"{digest}-{variant}-{version.get('viewport_width')}x{version.get('viewport_height')}.jpg"
            exists = target.is_file() and not target.is_symlink()
            cached += exists
            current_cached += exists and version["id"] in current_ids
    active = [{**job, "job_age_ms": elapsed(job.get("created_at"), now),
               "ready_wait_ms": elapsed(job.get("available_at"), now) if job.get("state") != "running" else None}
              for job in jobs if job.get("state") in ACTIVE]
    active.sort(key=lambda row: row.get("created_at") or "")
    diagnostics = _capture_evidence(data_root, {UUID(attempt["id"]).hex for attempt in attempts})
    result = {"schema_version": 1, "probe": "inventory", "timestamp": now,
        "runtime": {**runtime, "worker_heartbeat_age_ms": elapsed(runtime.get("worker_heartbeat"), now)},
        "jobs": {"total": len(jobs), "by_state": _counter(jobs, "state"), "by_kind": _counter(jobs, "kind"),
                   "details": jobs[:MAX_DETAIL], "details_truncated": max(0, len(jobs) - MAX_DETAIL),
                 "active": active[:MAX_DETAIL], "active_truncated": max(0, len(active) - MAX_DETAIL)},
        "attempts": attempt_report, "groups": group_report,
        "imports": {"total": len(imports), "by_state": _counter(imports, "state"),
            "details": [{**row, "duration_ms": None, "duration_missing": True,
                "missing_reason": "import_has_no_finished_at_or_historical_stage_timestamps",
                "capture_group_wall_ms": group_by_id.get(row.get("capture_group_id"), {}).get("wall_ms"),
                "classification_group_wall_ms": group_by_id.get(row.get("classification_group_id"), {}).get("wall_ms")}
                for row in imports[:MAX_DETAIL]]},
        "maintenance": {"total": len(operations), "by_state": _counter(operations, "state"),
            "details": [{**row, "duration_ms": None, "duration_missing": True,
                "missing_reason": "updated_at_is_not_a_reliable_stage_or_completion_clock"} for row in operations[:MAX_DETAIL]]},
        "snapshots": {"bookmarks": bookmark_count, "folders": folder_count, "versions": len(versions),
            "versions_by_variant": _counter(versions, "variant"), "artifacts": len(artifacts),
            "artifact_bytes_recorded": sum(row.get("byte_size", 0) for row in artifacts),
            "current_versions": len(current_ids), "current_errors": _counter([row for row in captures if row.get("last_error_code")], "last_error_code"),
            "versions_with_preview": cached, "current_with_preview": current_cached,
            "current_missing_preview": len(current_ids) - current_cached,
            "preview_cache_version": "v1", "preview_integrity_not_revalidated": True},
        "resources": {"state_database": _file_inventory([data_root / "state.sqlite3", data_root / "state.sqlite3-wal"]),
            "library_database": _file_inventory([generation_dir / "library.sqlite3", generation_dir / "library.sqlite3-wal"]),
            "html_archives": _file_inventory((generation_dir / "artifacts").glob("*/*.html")),
            "preview_images": _file_inventory(cache_root.glob("*.jpg")),
            "preview_requests": _file_inventory(cache_root.glob("*.request")),
            "preview_failures": _file_inventory(cache_root.glob("*.failed")),
            "performance_logs": _file_inventory((data_root / "diagnostics" / "performance").glob("perf-*"))},
        "capture_diagnostics": diagnostics,
        "coverage": {"attempts_total": len(attempts), "attempts_with_finished_at": sum(bool(row.get("finished_at")) for row in attempts),
            "attempts_with_capture_diagnostics": diagnostics["linked_attempts"],
            "historical_http_timings": "missing_unless_performance_logging_was_enabled",
            "historical_ai_request_timings": "missing_unless_performance_logging_was_enabled",
            "historical_import_stage_timings": "missing_unless_performance_logging_was_enabled",
            "historical_maintenance_stage_timings": "missing_unless_performance_logging_was_enabled"},
        "limitations": ["Live databases and files are observed at slightly different times; counts can change during collection.",
            "Queue age includes scheduling delay and previous attempts; initial queue and between-attempt waits are reported separately.",
            "Group wall time is derived only when every member is terminal and has a finished attempt; cancelled work can lack a final timestamp.",
            "Capture intervals are adjacent diagnostic marker differences, not profiler-exclusive stage durations.",
            "Preview presence and file sizes are counted without decoding images or hashing full HTML archives."]}
    result["collection_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, help="JSON file; omitted writes JSON to stdout")
    args = parser.parse_args(argv)
    result = collect_inventory(args.data_root)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
        print(json.dumps({"probe": "inventory", "output": str(args.output.resolve()), "collection_ms": result["collection_ms"]}, ensure_ascii=False))
    else:
        print(content, end="")
    return result


if __name__ == "__main__":
    main()
