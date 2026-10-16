"""Process lifetime control for explicit local development capture.

This does not provide the network/filesystem isolation of the Linux launcher.
The app owns the child and can terminate its process tree if capture hangs.
"""
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from .diagnostics import CaptureTrace, development_trace_path, error_evidence
from .models import CaptureError, CaptureResult


def _terminate(child):
    if child.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    else:
        os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            child.kill()
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def _cleanup(action, errors):
    """Keep cleanup failures separate from the capture's primary outcome."""
    try:
        action()
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(exc)


def _run_capture_process(command, output_dir, timeout_seconds, execution_guard, *, cleanup_errors=None):
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stdout_path = output_dir / "runner-result.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    deadline = time.monotonic() + timeout_seconds
    cleanup_errors = cleanup_errors if cleanup_errors is not None else []
    failure = None
    stdout_created = False
    try:
        with stdout_path.open("xb") as stdout:
            stdout_created = True
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout,
                stderr=subprocess.DEVNULL, env=environment, **options)
            try:
                while child.poll() is None:
                    if not execution_guard():
                        raise CaptureError("cancelled", "Capture attempt was cancelled or superseded")
                    if time.monotonic() >= deadline:
                        raise CaptureError("timeout", "Capture process exceeded its total time budget")
                    if stdout_path.stat().st_size > 128 * 1024:
                        raise CaptureError("invalid_result", "Capture result exceeds size budget")
                    time.sleep(0.1)
                if not execution_guard():
                    raise CaptureError("cancelled", "Capture attempt was cancelled or superseded")
                if stdout_path.stat().st_size > 128 * 1024:
                    raise CaptureError("invalid_result", "Capture result exceeds size budget")
            finally:
                active_failure = sys.exc_info()[1]
                before_cleanup = len(cleanup_errors)
                _cleanup(lambda: _terminate(child), cleanup_errors)
                if active_failure is None and len(cleanup_errors) > before_cleanup:
                    raise CaptureError("browser_failed", "Capture process cleanup failed") from cleanup_errors[-1]
        try:
            body = json.loads(stdout_path.read_bytes())
        except (ValueError, OSError) as exc:
            raise CaptureError("browser_failed", "Capture runner stopped without a valid result") from exc
        if not isinstance(body, dict):
            raise CaptureError("invalid_result", "Capture runner returned an invalid result")
        if body.get("ok") is not True:
            error = body.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not code or len(code) > 64 or not code.isidentifier():
                raise CaptureError("invalid_result", "Capture runner returned an invalid error")
            raise CaptureError(code, "Capture runner rejected this attempt")
        if child.returncode != 0 or not isinstance(body.get("result"), dict):
            raise CaptureError("invalid_result", "Capture runner returned an invalid result")
        return body["result"]
    except BaseException as exc:
        failure = exc
        raise
    finally:
        before_cleanup = len(cleanup_errors)
        if stdout_created:
            _cleanup(lambda: stdout_path.unlink(missing_ok=True), cleanup_errors)
        if failure is None and len(cleanup_errors) > before_cleanup:
            raise CaptureError("browser_failed", "Capture result cleanup failed") from cleanup_errors[-1]


def _record_parent_failure(request, failure, cleanup_errors, elapsed_seconds):
    """Merge bounded parent evidence after the child has stopped, retaining its last stage."""
    try:
        path = development_trace_path(request)
    except (OSError, ValueError):
        return
    if path is None:
        return
    trace = CaptureTrace(path, request)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(body, dict) and body.get("attempt_id") == request.attempt_uuid:
            trace.body = body
    except (OSError, ValueError):
        pass
    if failure is not None:
        trace.body["parent_failure"] = error_evidence(failure)
        if trace.body.get("state") == "running":
            trace.body["state"] = ("terminated_by_parent"
                if getattr(failure, "code", None) in ("timeout", "cancelled") else "failed")
            trace.body["elapsed_seconds"] = round(elapsed_seconds, 3)
        elif trace.body.get("state") == "succeeded":
            trace.body["child_state"] = "succeeded"
            trace.body["state"] = "failed"
        trace.body.setdefault("errors", error_evidence(failure))
    if cleanup_errors:
        trace.body["cleanup_errors"] = [error_evidence(error) for error in cleanup_errors[:4]]
    trace.save()


def run_development_capture(request, execution_guard):
    request.validate()
    if not request.development:
        raise CaptureError("isolation_required", "Direct capture is available only in development")
    output_dir = Path(request.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_path = output_dir / "runner-request.json"
    started = time.monotonic()
    failure, cleanup_errors = None, []
    request_created = False
    try:
        with request_path.open("x", encoding="utf-8") as destination:
            request_created = True
            json.dump(asdict(request), destination, ensure_ascii=False)
        result = _run_capture_process([sys.executable, "-X", "utf8", "-m", "capture", "--request", str(request_path)],
            output_dir, request.timeout_seconds, execution_guard, cleanup_errors=cleanup_errors)
        return CaptureResult(**result)
    except CaptureError as exc:
        failure = exc
        raise
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        failure = CaptureError("browser_failed", "Capture parent process failed")
        raise failure from exc
    finally:
        active_failure = sys.exc_info()[1]
        if request_created:
            _cleanup(lambda: request_path.unlink(missing_ok=True), cleanup_errors)
        if active_failure is None and cleanup_errors:
            failure = CaptureError("browser_failed", "Capture request cleanup failed")
        if failure is not None or cleanup_errors:
            _record_parent_failure(request, failure, cleanup_errors, time.monotonic() - started)
        if active_failure is None and failure is not None:
            raise failure from cleanup_errors[-1]
