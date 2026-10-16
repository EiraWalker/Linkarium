"""Opt-in, bounded, privacy-preserving timing records shared by web and workers.

Only stdlib is imported here: the capture child can use this before Django setup.
BOOKMARK_PERF=1 enables records for the lifetime of the process. This is an
observer: failures to collect or write a record never change a business result.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid


_ENABLED = os.environ.get("BOOKMARK_PERF", "0") == "1"
_CURRENT = ContextVar("bookmark_performance_span", default=None)
_LOCK = threading.Lock()
_SYMBOL = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_UUID_FIELDS = frozenset({"job_id", "group_id", "bookmark_id", "attempt_id",
    "maintenance_id", "version_id", "request_id"})
_TEXT_FIELDS = frozenset({"category", "kind", "variant", "stage", "status",
    "operation_type", "result_state", "error_code", "generation", "method"})
_NUMBER_FIELDS = frozenset({"count", "total", "processed", "byte_size", "response_bytes",
    "queue_ms", "wait_ms", "execution_ms", "retry_after_ms", "attempt_no", "retry_no",
    "status_code", "http_status", "sql_count", "sql_ms", "process_cpu_ms",
    "duration_ms", "elapsed_ms", "width", "height", "batch_size", "cache_hit"})
_INHERITED = _UUID_FIELDS | frozenset({"category", "kind", "variant", "generation"})


def enabled():
    return _ENABLED


def sanitize_fields(values):
    """Drop unknown keys and non-scalar values rather than stringifying them."""
    clean = {}
    for key, value in values.items():
        if key in _UUID_FIELDS:
            try:
                clean[key] = str(uuid.UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                pass
        elif key in _TEXT_FIELDS and isinstance(value, str) and _SYMBOL.fullmatch(value):
            clean[key] = value
        elif key in _NUMBER_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and abs(value) < 1e18:
                clean[key] = round(value, 3) if isinstance(value, float) else value
        elif key == "cache_hit" and isinstance(value, bool):
            clean[key] = value
    return clean


def _write(record):
    # One file per process avoids rotation collisions between web, worker and
    # capture children. Size and backup count are bounded even if env is invalid.
    root = Path(os.environ.get("BOOKMARK_PERF_DIR") or
        Path(os.environ.get("BOOKMARK_DATA_ROOT") or Path(__file__).resolve().parents[2] / "data") /
        "diagnostics" / "performance")
    try:
        maximum = max(65536, min(int(os.environ.get("BOOKMARK_PERF_MAX_BYTES", 5 * 1024 * 1024)), 50 * 1024 * 1024))
    except ValueError:
        maximum = 5 * 1024 * 1024
    encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _LOCK:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / f"perf-{os.getpid()}.jsonl"
        if path.exists() and path.stat().st_size + len(encoded) > maximum:
            last = path.with_suffix(".jsonl.3")
            last.unlink(missing_ok=True)
            for index in (2, 1):
                old = path.with_suffix(f".jsonl.{index}")
                if old.exists():
                    old.replace(path.with_suffix(f".jsonl.{index + 1}"))
            path.replace(path.with_suffix(".jsonl.1"))
        with path.open("ab") as output:
            output.write(encoded)
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _record(operation, fields, current=None, **system):
    if not isinstance(operation, str) or not _SYMBOL.fullmatch(operation):
        return
    inherited = {key: value for key, value in current.fields.items() if key in _INHERITED} if current else {}
    inherited.update(sanitize_fields(fields))
    record = {"schema": 1, "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "pid": os.getpid(), "operation": operation, **inherited, **system}
    _write(record)


class _DisabledSpan:
    trace_id = span_id = None
    elapsed_ms = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def set_fields(self, **fields):
        pass


_DISABLED_SPAN = _DisabledSpan()


class _Span:
    def __init__(self, operation, fields, new_trace=False, parent=None):
        self.operation, self.fields = operation, sanitize_fields(fields)
        self.parent = None if new_trace else (parent if parent is not None else _CURRENT.get())
        inherited = {key: value for key, value in self.parent.fields.items() if key in _INHERITED} if self.parent else {}
        self.fields = {**inherited, **self.fields}
        self.trace_id = self.parent.trace_id if self.parent else self.fields.get("request_id", uuid.uuid4().hex)
        self.span_id = uuid.uuid4().hex
        self._token = None
        self._start = self._cpu_start = None
        self.route = self.parent.route if self.parent else None

    def __enter__(self):
        try:
            self._start, self._cpu_start = time.perf_counter(), time.process_time()
            self._token = _CURRENT.set(self)
        except Exception:
            pass
        return self

    @property
    def elapsed_ms(self):
        try:
            return round((time.perf_counter() - self._start) * 1000, 3) if self._start is not None else 0.0
        except Exception:
            return 0.0

    def set_fields(self, **fields):
        try:
            self.fields.update(sanitize_fields(fields))
        except Exception:
            pass

    def __exit__(self, exception_type, exception, traceback):
        try:
            elapsed = self.elapsed_ms
            system = {"record_type": "span", "trace_id": self.trace_id, "span_id": self.span_id,
                "parent_span_id": self.parent.span_id if self.parent else None,
                "duration_ms": elapsed, "elapsed_ms": elapsed,
                "process_cpu_ms": round((time.process_time() - self._cpu_start) * 1000, 3),
                "outcome": "error" if exception_type else "ok"}
            if exception_type:
                system["exception_type"] = exception_type.__name__ if _SYMBOL.fullmatch(exception_type.__name__) else "Exception"
            if self.route is not None:
                system["route"] = self.route
            _record(self.operation, self.fields, **system)
        except Exception:
            pass
        finally:
            if self._token is not None:
                try:
                    _CURRENT.reset(self._token)
                except Exception:
                    pass
        return False


def span(operation, **fields):
    if not _ENABLED:
        return _DISABLED_SPAN
    try:
        return _Span(operation, fields)
    except Exception:
        return _DISABLED_SPAN


def trace(operation, **fields):
    if not _ENABLED:
        return _DISABLED_SPAN
    try:
        return _Span(operation, fields, new_trace=True)
    except Exception:
        return _DISABLED_SPAN


def emit(operation, **fields):
    if not _ENABLED:
        return
    try:
        current = _CURRENT.get()
        _record(operation, fields, current=current, record_type="event",
            trace_id=current.trace_id if current else None,
            parent_span_id=current.span_id if current else None)
    except Exception:
        pass


event = emit


def linked_span(operation, parent, **fields):
    """Continue a closed request trace while its streaming body is consumed."""
    if not _ENABLED:
        return _DISABLED_SPAN
    try:
        return _Span(operation, fields, parent=parent)
    except Exception:
        return _DISABLED_SPAN
