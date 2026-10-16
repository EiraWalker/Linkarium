"""Disposable first-screen images derived from immutable HTML archives.

No schema migration or archive mutation is required. Missing images are rendered
by the worker, never synchronously in an HTTP request. Generation and viewport
are part of the identity, including historical snapshots.
"""
import hashlib
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from django.db import transaction
from runtime import performance
from config.paths import generation_root, PROJECT_ROOT, current_generation
from .artifacts import artifact_path
from .errors import ApiError

CACHE_VERSION = "v1"
RETRY_SECONDS = 3600


def cache_root(generation=None):
    root = generation_root(generation)
    target = root / "previews" / CACHE_VERSION
    if any(p.is_symlink() for p in (root, root / "previews", target)) or not target.resolve().is_relative_to(root.resolve()):
        raise ApiError("INVALID_PREVIEW", "预览缓存路径无效")
    return target


def image_path(version, generation=None):
    digest = version.artifact_id
    width, height = version.viewport_width, version.viewport_height
    if (not re.fullmatch(r"[0-9a-f]{64}", digest or "") or version.variant not in ("desktop", "mobile")
            or not 1 <= width <= 4096 or not 1 <= height <= 4096):
        raise ApiError("INVALID_PREVIEW", "预览缓存标识无效")
    root = cache_root(generation)
    target = root / f"{digest}-{version.variant}-{width}x{height}.jpg"
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ApiError("INVALID_PREVIEW", "预览缓存路径无效")
    return target


def request_path(version_id, generation=None):
    root = cache_root(generation)
    return root / f"{uuid.UUID(str(version_id))}.request"


def request_preview(version, generation=None):
    """Prioritize a missing image the user is actually looking at (idempotent)."""
    target = request_path(version.id, generation)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb"):
            pass
    except FileExistsError:
        pass


def store_preview(version, source, generation=None, *, expected_sha256=None, expected_size=None):
    with performance.span("preview.store", category="preview", version_id=str(version.id), variant=version.variant):
        from capture.preview import validate_preview
        source = Path(source)
        validate_preview(source, expected_sha256=expected_sha256, expected_size=expected_size,
                         width=version.viewport_width, height=version.viewport_height)
        target = image_path(version, generation)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(source.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            target.with_suffix(".failed").unlink(missing_ok=True)
            request_path(version.id, generation).unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return target


def preview_response(request, version):
    from django.http import FileResponse, HttpResponseNotModified, JsonResponse
    from django.utils.http import parse_etags
    path = image_path(version)
    if not path.is_file():
        if not artifact_path(version.artifact_id, version.artifact.relative_path).is_file():
            raise ApiError("ARTIFACT_MISSING", "归档文件缺失", 404)
        failure = path.with_suffix(".failed")
        if failure.exists() and time.time() - failure.stat().st_mtime < RETRY_SECONDS:
            response = JsonResponse({"error": {"code": "PREVIEW_UNAVAILABLE", "message": "预览图暂不可用"}}, status=503)
            response["Retry-After"] = str(max(1, int(RETRY_SECONDS - (time.time() - failure.stat().st_mtime))))
            return response
        request_preview(version)
        response = JsonResponse({"data": {"state": "pending"}}, status=202)
        response["Retry-After"] = "3"
        return response
    # Authentication has already run. Revalidation prevents a cached private
    # image being returned after logout or deletion; the client keeps decoded
    # images only for its authenticated application session.
    with path.open("rb") as content:
        etag = '"' + hashlib.file_digest(content, "sha256").hexdigest() + '"'
    tags = parse_etags(request.headers.get("If-None-Match", ""))
    if etag in [tag.removeprefix("W/") for tag in tags] or "*" in tags:
        response = HttpResponseNotModified()
    else:
        response = FileResponse(path.open("rb"), content_type="image/jpeg")
    response["ETag"] = etag
    response["Cache-Control"] = "private, max-age=0, must-revalidate"
    response["Vary"] = "Cookie"
    return response


def _eligible(version, generation):
    path = image_path(version, generation)
    if path.is_file():
        return False
    failure = path.with_suffix(".failed")
    return not failure.exists() or time.time() - failure.stat().st_mtime >= RETRY_SECONDS


def next_missing(generation):
    from .models import SnapshotVersion, CaptureState
    root = cache_root(generation)
    for marker in sorted(root.glob("*.request"), key=lambda p: p.stat().st_mtime):
        if marker.is_symlink():
            continue
        try:
            version_id = uuid.UUID(marker.stem)
        except ValueError:
            continue
        version = SnapshotVersion.objects.select_related("artifact").filter(pk=version_id).first()
        if version and _eligible(version, generation):
            return version
        marker.unlink(missing_ok=True)
    current_ids = CaptureState.objects.exclude(current_version_id=None).values("current_version_id")
    for version in SnapshotVersion.objects.filter(pk__in=current_ids).select_related("artifact").order_by("-captured_at").iterator():
        if _eligible(version, generation):
            return version
    return None


def render_version(version, generation):
    """Render a stored archive in a bounded child; never fetch the source URL."""
    with performance.span("preview.render", category="preview", version_id=str(version.id), variant=version.variant, byte_size=version.artifact.byte_size):
        from .models import SnapshotVersion
        from . import queue
        source = artifact_path(version.artifact_id, version.artifact.relative_path, generation)
        if not source.is_file() or source.stat().st_size != version.artifact.byte_size:
            raise ApiError("ARTIFACT_CORRUPT", "归档文件校验失败")
        with performance.span("preview.verify_archive", category="preview"):
            with source.open("rb") as content:
                if hashlib.file_digest(content, "sha256").hexdigest() != version.artifact_id:
                    raise ApiError("ARTIFACT_CORRUPT", "归档文件校验失败")
        target = image_path(version, generation)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with performance.span("preview.child", category="preview"):
                env = dict(os.environ)
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(PROJECT_ROOT / "tools" / "browsers")
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                child = subprocess.Popen([sys.executable, "-m", "library.preview_render", str(source), str(temporary),
                    str(version.viewport_width), str(version.viewport_height), str(os.getpid())], cwd=PROJECT_ROOT / "backend",
                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                from capture.development import _terminate
                from runtime.models import RuntimeState
                from django.utils import timezone
                deadline = time.monotonic() + 65
                next_heartbeat = 0
                try:
                    while child.poll() is None:
                        runtime = queue.runtime_state()
                        if runtime.maintenance_mode or runtime.active_generation != generation:
                            return False
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Preview renderer exceeded its budget")
                        if time.monotonic() >= next_heartbeat:
                            RuntimeState.objects.filter(pk=1).update(worker_heartbeat=timezone.now())
                            next_heartbeat = time.monotonic() + 5
                        time.sleep(0.25)
                finally:
                    _terminate(child)
                if child.returncode:
                    raise RuntimeError("PREVIEW_RENDER_FAILED")
            with transaction.atomic(using="library"):
                runtime = queue.runtime_state()
                if runtime.maintenance_mode or runtime.active_generation != generation or not SnapshotVersion.objects.filter(pk=version.id, artifact_id=version.artifact_id).exists():
                    return False
                store_preview(version, temporary, generation)
            return True
        finally:
            temporary.unlink(missing_ok=True)


def backfill_one():
    from . import queue
    if queue.runtime_state().maintenance_mode:
        return False
    generation = current_generation()
    version = next_missing(generation)
    if not version:
        return False
    with performance.trace("preview.backfill", category="preview", version_id=str(version.id), variant=version.variant) as timing:
        try:
            completed = render_version(version, generation)
            if completed:
                print(f"preview_cached version={version.id}", flush=True)
        except Exception as error:
            # This is a derived-cache failure, never a failed website capture.
            # Do not print page content, source URLs, cookies or subprocess output.
            path = image_path(version, generation).with_suffix(".failed")
            path.write_text(type(error).__name__, encoding="ascii")
            print(f"preview_deferred version={version.id} error={type(error).__name__}", flush=True)
            timing.set_fields(result_state="deferred")
        return True


def cleanup_previews(generation=None):
    from .models import SnapshotVersion
    root = cache_root(generation)
    referenced = {image_path(version, generation).name for version in SnapshotVersion.objects.all()}
    for path in root.glob("*.jpg"):
        if not path.is_symlink() and path.name not in referenced and time.time() - path.stat().st_mtime > 3600:
            path.unlink(missing_ok=True)
