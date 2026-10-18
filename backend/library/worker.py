import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction, close_old_connections
from django.utils import timezone
from capture.models import MAX_CAPTURE_BYTES
from config.paths import DATA_ROOT, generation_root
from runtime.models import RuntimeState
from runtime import performance
from . import queue
from .catalog import library_settings
from .errors import ApiError
from .artifacts import artifact_path
from .models import Bookmark, PageMetadata, CaptureState, Artifact, SnapshotVersion, Job, JobAttempt


def capture_page(job, attempt):
    from capture import CaptureRequest, LauncherClient
    bookmark = Bookmark.objects.filter(pk=job.target_id).first()
    if not bookmark or bookmark.capture_epoch != job.input_revision or (job.kind == "capture" and not bookmark.snapshot_enabled):
        raise ApiError("OBJECT_CHANGED", "采集条件已变化", 409)
    output = DATA_ROOT / "work" / "capture" / str(attempt.id)
    development = settings.BOOKMARK_CAPTURE_MODE == "development"
    request = CaptureRequest(attempt_uuid=str(attempt.id), generation=job.generation, library_epoch=job.library_epoch, url=bookmark.canonical_url, variant=job.variant or "desktop", mode="metadata" if job.kind == "metadata" else "capture", output_dir=str(output), development=development, dns_mode=os.environ.get("BOOKMARK_CAPTURE_DNS_MODE", "system") if development else "system", deny_hosts=tuple(host for host in settings.ALLOWED_HOSTS if host != "*"))
    if settings.BOOKMARK_CAPTURE_MODE == "development":
        from capture.development import run_development_capture
        return run_development_capture(request, lambda: queue.execution_valid(job, attempt))
    try:
        return LauncherClient(settings.BOOKMARK_CAPTURE_SOCKET).capture(request)
    except OSError as error:
        raise ApiError("CAPTURE_ISOLATION_REQUIRED", "采集服务不可用，请检查部署配置", 503) from error


def result_value(result, key, default=None):
    return result.get(key, default) if isinstance(result, dict) else getattr(result, key, default)


def save_metadata(bookmark, result):
    with performance.span("worker.save_metadata", category="worker"):
        title = (result_value(result, "title", "") or "")[:500]
        excerpt = (result_value(result, "text_excerpt", "") or "")[:6000]
        description = (result_value(result, "description", "") or "")[:2000]
        digest = hashlib.sha256((title + description + excerpt).encode()).hexdigest()
        previous = PageMetadata.objects.filter(bookmark=bookmark).first()
        if not previous or previous.content_hash != digest:
            bookmark.classification_revision += 1
        bookmark.source_title = title or bookmark.source_title
        bookmark.final_url = result_value(result, "final_url", bookmark.canonical_url)
        bookmark.save(update_fields=["source_title", "final_url", "classification_revision"])
        PageMetadata.objects.update_or_create(bookmark=bookmark, defaults={"title": title, "description": description, "text_excerpt": excerpt, "fetched_at": timezone.now(), "source_url": bookmark.canonical_url, "content_hash": digest})


@transaction.atomic(using="library")
def publish_metadata(job, attempt, result):
    if not queue.execution_valid(job, attempt):
        raise ApiError("ATTEMPT_EXPIRED", "任务已取消或失效", 409)
    bookmark = Bookmark.objects.filter(pk=job.target_id, capture_epoch=job.input_revision).first()
    if not bookmark:
        raise ApiError("OBJECT_CHANGED", "采集条件已变化", 409)
    save_metadata(bookmark, result)
    queue.finish(job, attempt, {"bookmark_id": str(bookmark.id)})


def publish_snapshot(job, attempt, result):
    previous = SnapshotVersion.objects.filter(capture_attempt=attempt).first()
    if previous:
        return previous
    source = Path(result_value(result, "artifact_path"))
    if not source.is_file() or source.stat().st_size > MAX_CAPTURE_BYTES:
        raise ApiError("INVALID_ARTIFACT", "快照文件无效")
    with performance.span("snapshot.hash", category="worker"):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    relative = f"artifacts/{digest[:2]}/{digest}.html"
    destination = artifact_path(digest, relative, job.generation)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f".{attempt.id}.tmp")
    owns_temporary = False
    try:
        with performance.span("snapshot.copy", category="worker"):
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                owns_temporary = True
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        with transaction.atomic(using="library"):
            if not queue.execution_valid(job, attempt):
                raise ApiError("ATTEMPT_EXPIRED", "任务已取消或失效", 409)
            bookmark = Bookmark.objects.filter(pk=job.target_id, capture_epoch=job.input_revision, snapshot_enabled=True).first()
            if not bookmark:
                raise ApiError("OBJECT_CHANGED", "采集条件已变化", 409)
            # Cleanup takes the same library write lock. Publish the prepared
            # file under that lock so it cannot reclaim an older orphan with
            # identical bytes between rename and reference creation.
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise ApiError("ARTIFACT_CORRUPT", "已有归档文件校验失败")
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            artifact, _ = Artifact.objects.get_or_create(sha256=digest, defaults={"relative_path": relative, "byte_size": destination.stat().st_size})
            now = timezone.now()
            version = SnapshotVersion.objects.create(bookmark=bookmark, variant=job.variant, artifact=artifact, capture_attempt=attempt, captured_at=now, source_url=bookmark.canonical_url, final_url=result_value(result, "final_url", bookmark.canonical_url), viewport_width=result_value(result, "viewport_width"), viewport_height=result_value(result, "viewport_height"), recipe_version=str(result_value(result, "recipe_version", "1")), capture_epoch=bookmark.capture_epoch, content_warnings=result_value(result, "warnings", []) or [])
            preview_source = result_value(result, "preview_path")
            if preview_source:
                try:
                    from .preview_cache import store_preview
                    store_preview(version, preview_source, job.generation,
                        expected_sha256=result_value(result, "preview_sha256"),
                        expected_size=result_value(result, "preview_byte_size"))
                except Exception as error:
                    # The immutable HTML remains authoritative. Cache generation
                    # is recoverable independently by the idle worker.
                    print(f"preview_deferred version={version.id} error={type(error).__name__}", flush=True)
            state, _ = CaptureState.objects.get_or_create(bookmark=bookmark, variant=job.variant)
            state.current_version, state.last_attempt_at, state.last_success_at = version, now, now
            state.next_due_at = now + timedelta(seconds=bookmark.refresh_interval_seconds or library_settings().default_refresh_seconds)
            state.failure_count, state.last_error_code = 0, ""
            state.save()
            if job.variant == "desktop" or not PageMetadata.objects.filter(bookmark=bookmark).exists():
                save_metadata(bookmark, result)
            queue.finish(job, attempt, {"version_id": str(version.id)})
            return version
    finally:
        if owns_temporary:
            temporary.unlink(missing_ok=True)


def cleanup_artifacts():
    root = generation_root()
    # Short write lock prevents a concurrent publication from acquiring a row
    # for an object while it is removed from disk.
    with transaction.atomic(using="library"):
        for artifact in Artifact.objects.filter(versions__isnull=True):
            path = artifact_path(artifact.sha256, artifact.relative_path)
            if path.exists() and time.time() - path.stat().st_mtime < 3600:
                continue
            path.unlink(missing_ok=True)
            artifact.delete()
        referenced = set(Artifact.objects.values_list("relative_path", flat=True))
        for path in (root / "artifacts").glob("*/*.html"):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            if str(path.relative_to(root)).replace("\\", "/") not in referenced and time.time() - path.stat().st_mtime > 3600:
                path.unlink(missing_ok=True)
        from .preview_cache import cleanup_previews
        cleanup_previews()


def heartbeat(job, attempt, stop):
    while not stop.wait(10):
        close_old_connections()
        if not Job.objects.filter(pk=job.id, state="running", active_attempt_id=attempt.id).exists():
            if settings.BOOKMARK_CAPTURE_MODE != "development" and job.kind in ("capture", "metadata"):
                try:
                    from capture import LauncherClient
                    LauncherClient(settings.BOOKMARK_CAPTURE_SOCKET).cancel(str(attempt.id), job.generation, job.library_epoch)
                except Exception:
                    pass
            return
        now = timezone.now()
        JobAttempt.objects.filter(pk=attempt.id, lease_token=attempt.lease_token, state="running").update(heartbeat_at=now, lease_expires_at=now+timedelta(seconds=180))
        RuntimeState.objects.filter(pk=1).update(worker_heartbeat=now)
    close_old_connections()


def cleanup_capture_work(attempt):
    """Remove only this attempt's private development staging directory."""
    expected_base = DATA_ROOT.resolve() / "work" / "capture"
    target = expected_base / str(attempt.id)
    if not target.exists():
        return
    # Check the final absolute target before recursive removal on Windows too.
    if expected_base.resolve() != expected_base or target.resolve() != target:
        return
    shutil.rmtree(target)


def run_one(prefer_background=False):
    from .importing import advance_imports
    with performance.span("worker.advance_imports", category="worker"):
        advance_imports()
    claimed = queue.claim_next(prefer_background)
    if not claimed:
        return False
    job, attempt = claimed
    with performance.trace("worker.job", category="worker", job_id=str(job.id), attempt_id=str(attempt.id), group_id=job.payload.get("group_id") if isinstance(job.payload, dict) else None, kind=job.kind, variant=job.variant, attempt_no=attempt.attempt_no) as timing:
        if performance.enabled():
            performance.emit("worker.queue_age", category="worker", job_id=str(job.id),
                attempt_id=str(attempt.id), kind=job.kind, attempt_no=attempt.attempt_no,
                queue_ms=max(0, (attempt.created_at - job.created_at).total_seconds() * 1000),
                wait_ms=max(0, (attempt.created_at - job.available_at).total_seconds() * 1000))
        stopped = threading.Event()
        thread = threading.Thread(target=heartbeat, args=(job, attempt, stopped), daemon=True)
        thread.start()
        try:
            if job.kind in ("capture", "metadata"):
                with performance.span("worker.capture", category="worker", job_id=str(job.id),
                        attempt_id=str(attempt.id), kind=job.kind, variant=job.variant):
                    result = capture_page(job, attempt)
                if job.kind == "capture":
                    with performance.span("worker.publish_snapshot", category="worker"):
                        publish_snapshot(job, attempt, result)
                else:
                    with performance.span("worker.publish_metadata", category="worker"):
                        publish_metadata(job, attempt, result)
            elif job.kind == "classification":
                from .classification import generate_proposals
                from .ai_transport import request_budget
                from .importing import reserve_ai_request
                import_id = job.payload.get('import_id')
                with performance.span("worker.classification", category="worker", count=len(job.payload["bookmark_ids"])):
                    with request_budget((lambda: reserve_ai_request(import_id)) if import_id else None):
                        result = generate_proposals(job.payload["bookmark_ids"], job.payload.get("profile_id"), execution_guard=lambda: queue.execution_valid(job, attempt), job_group_id=job.payload.get("group_id"))
                queue.finish(job, attempt, {"proposal_count": result.get("count", 0) if isinstance(result, dict) else len(result)})
            elif job.kind == "import":
                from .importing import execute_import
                with performance.span("worker.import", category="worker"):
                    result = execute_import(job.target_id, execution_guard=lambda: queue.execution_valid(job, attempt))
                queue.finish(job, attempt, {"import_id": str(job.target_id)})
            elif job.kind == "ai_test":
                from .classification import test_profile
                with performance.span("worker.ai_test", category="worker"):
                    result = test_profile(job.target_id, execution_guard=lambda: queue.execution_valid(job, attempt))
                queue.finish(job, attempt, result)
            elif job.kind == "cleanup":
                with performance.span("worker.cleanup", category="worker"):
                    cleanup_artifacts()
                queue.finish(job, attempt)
            else:
                raise ApiError("UNKNOWN_JOB", "任务类型无效")
        except Exception as error:
            code = getattr(error, "code", "TASK_FAILED")
            if job.kind in ("capture", "metadata"):
                code = {"unsafe_url": "CAPTURE_BLOCKED", "proxy_config": "CAPTURE_PROXY_CONFIG", "proxy_unavailable": "CAPTURE_PROXY_UNAVAILABLE", "access_denied": "CAPTURE_ACCESS_DENIED", "not_found": "CAPTURE_NOT_FOUND", "challenge": "CAPTURE_CHALLENGE", "dom_budget": "CAPTURE_DOM_TOO_LARGE", "timeout": "CAPTURE_TIMEOUT", "dns_failed": "CAPTURE_NETWORK", "browser_failed": "CAPTURE_NETWORK", "tool_missing": "CAPTURE_TOOL_MISSING", "isolation_required": "CAPTURE_ISOLATION_REQUIRED", "byte_budget": "CAPTURE_TOO_LARGE", "request_budget": "CAPTURE_TOO_LARGE", "unsupported_visual_state": "CAPTURE_UNSUPPORTED", "replay_failed": "CAPTURE_REPLAY_FAILED"}.get(code, code.upper() if isinstance(code, str) else "TASK_FAILED")
            queue.finish(job, attempt, error_code=code, retryable=code in ("CAPTURE_TIMEOUT", "AI_TIMEOUT", "AI_RATE_LIMITED", "NETWORK_ERROR", "CAPTURE_NETWORK", "CAPTURE_PROXY_UNAVAILABLE"))
            timing.set_fields(result_state="handled_error", error_code=code)
            print(f"job={job.id} kind={job.kind} error={code}", flush=True)
        finally:
            stopped.set()
            thread.join(timeout=2)
            if job.kind in ("capture", "metadata"):
                try:
                    with performance.span("worker.cleanup_capture_work", category="worker"):
                        if settings.BOOKMARK_CAPTURE_MODE == "development":
                            cleanup_capture_work(attempt)
                        else:
                            from capture import LauncherClient
                            LauncherClient(settings.BOOKMARK_CAPTURE_SOCKET).release(str(attempt.id), job.generation, job.library_epoch)
                except Exception:
                    print(f"job={job.id} staging_cleanup_deferred", flush=True)
            with performance.span("worker.advance_imports", category="worker"):
                advance_imports()
            close_old_connections()
        return True


def recover_expired():
    for job in Job.objects.filter(state="running"):
        attempt = JobAttempt.objects.filter(pk=job.active_attempt_id).first()
        if not attempt or attempt.lease_expires_at > timezone.now():
            continue
        if job.kind in ("capture", "metadata") and settings.BOOKMARK_CAPTURE_MODE != "development":
            from capture import LauncherClient
            # A failed cancellation must block reassignment of this attempt.
            try:
                LauncherClient(settings.BOOKMARK_CAPTURE_SOCKET).release(str(attempt.id), job.generation, job.library_epoch)
            except Exception:
                continue
        queue.finish(job, attempt, error_code="LEASE_EXPIRED", retryable=True, allow_expired=True)


def run_worker(once=False):
    cycles = 0
    while True:
        RuntimeState.objects.filter(pk=1).update(worker_heartbeat=timezone.now())
        if not queue.runtime_state().maintenance_mode:
            recover_expired()
            queue.schedule_due()
            worked = run_one(cycles % 6 == 5)
            if not worked:
                from .preview_cache import backfill_one
                try:
                    worked = backfill_one()
                except Exception as error:
                    print(f"preview_cache_unavailable error={type(error).__name__}", flush=True)
            cycles += 1
        else:
            worked = False
        if once:
            break
        if not worked:
            time.sleep(2)
