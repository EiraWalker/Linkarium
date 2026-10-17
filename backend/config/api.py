"""Native Django JSON API. Views delegate catalog and maintenance rules."""
import hashlib
import json
import logging
import shutil
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ValidationError, RequestDataTooBig
from django.core import signing
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count
from django.http import JsonResponse, FileResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from config.paths import DATA_ROOT, generation_root
from runtime.models import RuntimeState, ProviderSecret, MaintenanceOperation, RuntimeIdempotency
from library.errors import ApiError
from library.artifacts import artifact_path
from library import catalog, queue
from library.models import *
from .middleware import error_response

logger = logging.getLogger(__name__)
MAX_BOOKMARK_BATCH = 1000


def ok(data, status=200, page=None):
    body = {"data": data}
    if page is not None:
        body["page"] = page
    return JsonResponse(body, status=status)


def payload(request):
    if request.content_type != "application/json":
        raise ApiError("INVALID_CONTENT_TYPE", "请使用 JSON 请求")
    if len(request.body) > 1024*1024:
        raise ApiError("REQUEST_TOO_LARGE", "请求内容过大", 413)
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeError):
        raise ApiError("INVALID_JSON", "请求 JSON 格式无效")
    if not isinstance(data, dict):
        raise ApiError("INVALID_JSON", "请求应为对象")
    return data


def bounded_int(value, default, low=1, high=100):
    try:
        result = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise ApiError("INVALID_NUMBER", "数值参数无效")
    if not low <= result <= high:
        raise ApiError("INVALID_NUMBER", f"数值应在 {low}–{high} 之间")
    return result


def require_method(request, *methods):
    if request.method not in methods:
        raise ApiError("METHOD_NOT_ALLOWED", "不支持此操作", 405)


def variant_of(request):
    variant = request.GET.get("variant", "desktop")
    if variant not in ("desktop", "mobile"):
        raise ApiError("INVALID_VARIANT", "请选择桌面或手机预览")
    return variant


def paginate(values, request):
    number = bounded_int(request.GET.get("page"), 1, high=1000000)
    size = bounded_int(request.GET.get("page_size"), 48)
    total = len(values)
    return values[(number-1)*size:number*size], {"number": number, "size": size, "total": total}


def serialize_tag(tag):
    return {"id": str(tag.id), "name": tag.name, "revision": tag.revision, "count": getattr(tag, "count", 0)}


def serialize_folder(folder, folders=None, counts=None):
    return {"id": str(folder.id), "name": folder.name, "parent_id": str(folder.parent_id) if folder.parent_id else None, "revision": folder.revision, "position": folder.position, "path": catalog.folder_path(folder.id, folders), "count": (counts or {}).get(str(folder.id), 0)}


def serialize_snapshot(version, current_id=None):
    return {"id": str(version.id), "version_id": str(version.id), "variant": version.variant, "captured_at": version.captured_at.isoformat(), "viewport_width": version.viewport_width, "viewport_height": version.viewport_height, "preview_url": f"/api/v1/snapshots/{version.id}/preview", "thumbnail_url": f"/api/v1/snapshots/{version.id}/thumbnail?v=1", "warnings": version.content_warnings, "is_current": version.id == current_id, "byte_size": version.artifact.byte_size, "source_url": version.source_url, "final_url": version.final_url}


def serialize_bookmark(bookmark, variant="desktop", detail=False, folders=None, jobs=None):
    captures = list(bookmark.capture_states.all())
    state = next((s for s in captures if s.variant == variant), None)
    preview = None
    if state and state.current_version_id:
        preview = serialize_snapshot(state.current_version, state.current_version_id)
        preview["state"] = "ready"
    tags = [serialize_tag(tag) for tag in bookmark.tags.all()]
    if jobs is None:
        jobs = capture_jobs([bookmark])
    active_state = jobs.get((str(bookmark.id), variant))
    # CaptureState retains the last attempt's diagnostic until a new snapshot
    # succeeds. A queued/running replacement has no current error yet.
    capture_status = {"state": "disabled" if not bookmark.snapshot_enabled else active_state or ("failed" if state and state.last_error_code else "ready" if preview else "missing"), "last_error_code": state.last_error_code if state and active_state not in ("queued", "running") else ""}
    result = {"id": str(bookmark.id), "revision": bookmark.revision, "title": bookmark.title, "url": bookmark.original_url, "domain": bookmark.domain, "folder_id": str(bookmark.folder_id) if bookmark.folder_id else None, "folder_path": catalog.folder_path(bookmark.folder_id, folders), "tags": tags, "is_starred": bookmark.is_starred, "snapshot_enabled": bookmark.snapshot_enabled, "preview": preview, "capture_status": capture_status, "added_at": bookmark.created_at.isoformat(), "created_at": bookmark.created_at.isoformat()}
    if detail:
        result.update(notes=bookmark.notes, final_url=bookmark.final_url, auto_refresh_paused=bookmark.auto_refresh_paused, refresh_interval_seconds=bookmark.refresh_interval_seconds, source_title=bookmark.source_title, title_override=bookmark.title_override, capture_states=[])
        for capture in captures:
            active = jobs.get((str(bookmark.id), capture.variant))
            result["capture_states"].append({"variant": capture.variant, "state": active or ("failed" if capture.last_error_code else "ready" if capture.current_version_id else "missing"), "last_success_at": capture.last_success_at.isoformat() if capture.last_success_at else None, "last_attempt_at": capture.last_attempt_at.isoformat() if capture.last_attempt_at else None, "next_due_at": capture.next_due_at.isoformat() if capture.next_due_at else None, "last_error_code": capture.last_error_code if active not in ("queued", "running") else "", "current_version_id": str(capture.current_version_id) if capture.current_version_id else None})
    return result


def capture_jobs(bookmarks):
    """Load both devices' active status in one query for a catalog page."""
    result = {}
    rows = Job.objects.filter(target_id__in=[bookmark.id for bookmark in bookmarks], kind="capture", state__in=queue.ACTIVE).order_by("-created_at").values("target_id", "variant", "state")
    for row in rows:
        result.setdefault((str(row["target_id"]), row["variant"]), row["state"])
    return result


def loaded_bookmarks():
    from django.db.models import Prefetch
    return Bookmark.objects.prefetch_related("tags", Prefetch("capture_states", queryset=CaptureState.objects.select_related("current_version__artifact")))


def list_bookmarks(request):
    variant = variant_of(request)
    fields = request.GET.get("fields", "title,domain,tags,folder,notes,url").split(",")
    allowed = {"title", "domain", "tags", "folder", "notes", "url"}
    if not fields or any(field not in allowed for field in fields):
        raise ApiError("INVALID_SEARCH_FIELDS", "请至少选择一种有效搜索方式")
    query = request.GET.get("q", "").strip()
    if len(query) > 500:
        raise ApiError("INVALID_QUERY", "搜索内容过长")
    tokens = [catalog.text_key(token) for token in query.split()]
    scope = request.GET.get("scope", "all")
    if scope not in ("all", "folder"):
        raise ApiError("INVALID_SCOPE", "搜索范围无效")
    qs = Bookmark.objects.all()
    folders = catalog.folder_map()
    if scope == "folder":
        folder_id = request.GET.get("folder_id")
        if not folder_id:
            raise ApiError("INVALID_SCOPE", "请选择搜索文件夹")
        ids = catalog.descendant_ids(folder_id, folders) if request.GET.get("include_descendants", "true") == "true" else [folder_id]
        qs = qs.filter(folder_id__in=ids)
    if request.GET.get("unclassified") == "true":
        qs = qs.filter(folder__isnull=True)
    if "starred" in request.GET:
        if request.GET["starred"] not in ("true", "false"):
            raise ApiError("INVALID_FILTER", "星标筛选无效")
        qs = qs.filter(is_starred=request.GET["starred"] == "true")
    tag_ids = [t for t in request.GET.get("tag_ids", "").split(",") if t]
    if tag_ids:
        qs = qs.filter(tags__id__in=tag_ids).distinct()
    if tokens:
        # Search bounded scalar rows, then load related models only for the page.
        # This preserves Unicode NFKC substring matching without constructing
        # 1000 ORM objects and 2000 device states on every keystroke.
        tag_text = {}
        if "tags" in fields:
            for bookmark_id, name in BookmarkTag.objects.filter(bookmark__in=qs).values_list("bookmark_id", "tag__name"):
                tag_text.setdefault(bookmark_id, []).append(name)
        paths = {key: catalog.folder_path(key, folders) for key in folders} if "folder" in fields else {}
        matched = []
        for row in qs.values("id", "source_title", "title_override", "domain", "original_url", "notes", "folder_id"):
            candidates = {"title": row["title_override"] or row["source_title"] or row["domain"], "domain": row["domain"], "url": row["original_url"], "notes": row["notes"], "tags": " ".join(tag_text.get(row["id"], [])), "folder": paths.get(str(row["folder_id"]), "")}
            normalized = [catalog.text_key(candidates[field]) for field in fields]
            if any(token in candidate for token in tokens for candidate in normalized):
                matched.append(row["id"])
        qs = qs.filter(pk__in=matched)
    if request.GET.get("selection") == "true":
        # Reuse every search/filter rule, without loading cards, previews or
        # pagination. Revisions bind subsequent bulk changes to this selection.
        return ok([{"id": str(identity), "revision": revision} for identity, revision in qs.values_list("id", "revision")])
    order = request.GET.get("order", "desc")
    if order not in ("asc", "desc"):
        raise ApiError("INVALID_SORT", "排序方向无效")
    sort = request.GET.get("sort", "added_at")
    if sort == "snapshot_updated_at":
        from django.db.models import F, OuterRef, Subquery
        capture = CaptureState.objects.filter(bookmark_id=OuterRef("pk"), variant=variant).values("last_success_at")[:1]
        qs = qs.annotate(snapshot_time=Subquery(capture)).order_by(F("snapshot_time").desc(nulls_last=True) if order == "desc" else F("snapshot_time").asc(nulls_last=True), "-id" if order == "desc" else "id")
    elif sort == "added_at":
        qs = qs.order_by("-created_at", "-id") if order == "desc" else qs.order_by("created_at", "id")
    elif sort == "title":
        sorted_rows = sorted(qs.values("id", "title_override", "source_title", "domain"), key=lambda row: (catalog.text_key(row["title_override"] or row["source_title"] or row["domain"]), str(row["id"])), reverse=order == "desc")
        page_rows, page = paginate(sorted_rows, request)
        ordered_ids = [row["id"] for row in page_rows]
        lookup = {b.id: b for b in loaded_bookmarks().filter(pk__in=ordered_ids)}
        jobs = capture_jobs(lookup.values())
        return ok([serialize_bookmark(lookup[i], variant, folders=folders, jobs=jobs) for i in ordered_ids], page=page)
    else:
        raise ApiError("INVALID_SORT", "排序字段无效")
    number = bounded_int(request.GET.get("page"), 1, high=1000000)
    size = bounded_int(request.GET.get("page_size"), 48)
    page = {"number": number, "size": size, "total": qs.count()}
    from django.db.models import Prefetch
    values = list(qs.prefetch_related("tags", Prefetch("capture_states", queryset=CaptureState.objects.select_related("current_version__artifact")))[(number-1)*size:number*size])
    jobs = capture_jobs(values)
    return ok([serialize_bookmark(b, variant, folders=folders, jobs=jobs) for b in values], page=page)


def selected_items(data, limit=100):
    items = data.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= limit or any(not isinstance(item, dict) or not item.get("id") for item in items) or len({item["id"] for item in items}) != len(items):
        raise ApiError("INVALID_BATCH", f"每次请选择 1–{limit} 个不同项目")
    return items


def folder_bookmark_ids(folder_id):
    # "all" is a virtual folder: it includes unclassified sites and every
    # folder, without creating a mutable Folder record.
    bookmarks = Bookmark.objects.all()
    if folder_id != "all":
        folder = catalog.get_entity(Folder, folder_id)
        bookmarks = bookmarks.filter(folder_id__in=catalog.descendant_ids(folder.id))
    return list(bookmarks.values_list("id", flat=True))


@transaction.atomic(using="library")
def bulk_bookmarks(data):
    items = selected_items(data, limit=MAX_BOOKMARK_BATCH)
    bookmarks = [catalog.get_entity(Bookmark, item["id"]) for item in items]
    for bookmark, item in zip(bookmarks, items):
        catalog.require_revision(bookmark, item)
    action = data.get("action")
    if action == "delete":
        return catalog.delete_catalog("bookmark", [b.id for b in bookmarks], data.get("preview_token"))
    if action == "move":
        folder_id = data.get("folder_id") or None
        if folder_id:
            catalog.get_entity(Folder, folder_id)
        for bookmark in bookmarks:
            bookmark.folder_id = folder_id
            bookmark.revision += 1
            bookmark.classification_revision += 1
            bookmark.save()
    elif action in ("add_tags", "remove_tags"):
        tag_ids = data.get("tag_ids", [])
        catalog.check_bookmark_fields({"tag_ids": tag_ids})
        for bookmark in bookmarks:
            getattr(bookmark.tags, "add" if action == "add_tags" else "remove")(*tag_ids)
            bookmark.revision += 1
            bookmark.save(update_fields=["revision", "updated_at"])
    else:
        raise ApiError("INVALID_ACTION", "批量操作无效")
    return {"updated_count": len(bookmarks)}


def serialize_profile(profile):
    from library.classification import serialize_profile as serialize
    return serialize(profile)


def runtime_status():
    state = queue.runtime_state()
    heartbeat = state.worker_heartbeat
    return {"active_generation": state.active_generation, "library_epoch": state.library_epoch, "maintenance_mode": state.maintenance_mode, "active_maintenance_id": str(state.active_maintenance_id) if state.active_maintenance_id else None, "worker_alive": bool(heartbeat and heartbeat > timezone.now()-timedelta(seconds=180)), "worker_heartbeat": heartbeat.isoformat() if heartbeat else None, "free_disk_bytes": shutil.disk_usage(DATA_ROOT).free, "capture_mode": settings.BOOKMARK_CAPTURE_MODE, "capture_available": (Path(settings.BOOKMARK_CAPTURE_SOCKET).exists() if settings.BOOKMARK_CAPTURE_MODE != "development" else bool(shutil.which("monolith") or (settings.BASE_DIR / "tools" / "monolith").exists()))}


def idempotent(request, data, execute, runtime=False):
    key = request.headers.get("Idempotency-Key")
    if not key:
        if runtime:
            raise ApiError("IDEMPOTENCY_REQUIRED", "此操作需要请求唯一键")
        return execute()
    if not 1 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ApiError("INVALID_IDEMPOTENCY_KEY", "请求唯一键无效")
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    alias, model = ("default", RuntimeIdempotency) if runtime else ("library", LibraryIdempotency)
    with transaction.atomic(using=alias):
        lookup = {"owner_key": "owner", "method": request.method, "path": request.path, "key": key}
        existing = model.objects.filter(**lookup).first()
        state = queue.runtime_state()
        if existing and not runtime and (existing.generation != state.active_generation or existing.library_epoch != state.library_epoch):
            existing.delete()
            existing = None
        if existing:
            if existing.request_hash != digest:
                raise ApiError("IDEMPOTENCY_CONFLICT", "相同请求键已用于其他内容", 409)
            return JsonResponse(existing.response_body, status=existing.response_status)
        response = execute()
        body = json.loads(response.content)
        fields = {**lookup, "request_hash": digest, "response_status": response.status_code, "response_body": body, "expires_at": timezone.now()+timedelta(hours=24)}
        if runtime:
            identity = body.get("data", {}).get("operation_id")
            fields["operation_id"] = identity
        else:
            fields.update(generation=state.active_generation, library_epoch=state.library_epoch)
        model.objects.create(**fields)
        return response


def dispatch(request, route):
    try:
        return route_request(request, route)
    except ApiError as error:
        extra = {}
        if error.details is not None:
            extra["details"] = error.details
        if error.field_errors is not None:
            extra["field_errors"] = error.field_errors
        return error_response(error.code, error.message, error.status, getattr(request, "request_id", None), **extra)
    except (ValueError, TypeError, ValidationError):
        return error_response("INVALID_INPUT", "请求字段格式无效", 400, getattr(request, "request_id", None))
    except RequestDataTooBig:
        return error_response("REQUEST_TOO_LARGE", "请求内容过大", 413, getattr(request, "request_id", None))
    except IntegrityError:
        return error_response("CONFLICT", "数据已变化或存在同名记录，请重新读取", 409, getattr(request, "request_id", None))
    except Exception as error:
        code = getattr(error, "code", None)
        if code:
            return error_response(code, "外部服务请求失败，请检查配置或稍后重试", 503 if "TIMEOUT" in code else 400, getattr(request, "request_id", None))
        logger.exception("API failure request=%s", getattr(request, "request_id", ""))
        return error_response("INTERNAL_ERROR", "服务暂时无法处理请求", 500, getattr(request, "request_id", None))


def route_request(request, route):
    parts = route.split("/")
    from runtime.authentication import auth_mode
    if route == "session":
        require_method(request, "GET")
        owner = request.user.is_authenticated and request.user.username == settings.BOOKMARK_OWNER
        from django.contrib.auth import get_user_model
        result = {"authenticated": owner, "csrf_token": get_token(request),
                  "initialization_required": not get_user_model().objects.filter(username=settings.BOOKMARK_OWNER).exists(),
                  "auth_mode": auth_mode(), "password_login_enabled": settings.BOOKMARK_AUTH_DEBUG}
        from runtime.models import PasskeyCredential, FileKeyCredential
        from runtime.authentication import passkey_availability
        from runtime.auth_sessions import session_details
        from urllib.parse import urlsplit
        result.update(passkey_availability(request))
        result["passkey_configured"] = PasskeyCredential.objects.filter(user__username=settings.BOOKMARK_OWNER,
            rp_id=urlsplit(result["passkey_origin"] or f"{request.scheme}://{request.get_host()}").hostname).exists()
        result["file_key_configured"] = FileKeyCredential.objects.filter(user__username=settings.BOOKMARK_OWNER).exists()
        result.update(session_details(request))
        result["maintenance_mode"] = queue.runtime_state().maintenance_mode
        if owner:
            state = queue.runtime_state()
            result.update(active_generation=state.active_generation, library_epoch=state.library_epoch, maintenance_mode=state.maintenance_mode)
        return ok(result)
    if route == "session/login":
        require_method(request, "POST")
        if not settings.BOOKMARK_AUTH_DEBUG:
            raise ApiError("PASSWORD_LOGIN_DISABLED", "请使用 Passkey 或私钥文件登录", 403)
        data = payload(request)
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 1024:
            raise ApiError("INVALID_PASSWORD", "请输入密码")
        user = authenticate(request=request, username=settings.BOOKMARK_OWNER, password=password)
        if not user:
            raise ApiError("LOGIN_FAILED", "密码不正确", 401)
        login(request, user)
        from runtime.auth_sessions import establish_debug_session
        establish_debug_session(request)
        return ok({"authenticated": True})
    if route == "session/passkey/grants":
        require_method(request, "POST")
        from runtime.authentication import issue_device_grant
        return ok(issue_device_grant(request), 201)
    if route.startswith("session/passkey/"):
        require_method(request, "POST")
        from runtime.authentication import passkey_request
        return ok(passkey_request(request, route.removeprefix("session/passkey/"), payload(request)))
    if route.startswith("session/key/"):
        require_method(request, "POST")
        from runtime.authentication import file_key_request
        return ok(file_key_request(request, route.removeprefix("session/key/"), payload(request)))
    if not request.user.is_authenticated or request.user.username != settings.BOOKMARK_OWNER:
        raise ApiError("AUTH_REQUIRED", "请先登录", 401)
    state = queue.runtime_state()
    maintenance_allowed = parts[0] in ("session", "runtime", "backups", "restores", "maintenance-operations", "data")
    if state.maintenance_mode and not maintenance_allowed:
        raise ApiError("MAINTENANCE", "收藏库正在维护，请稍后重试", 503)
    if state.maintenance_mode and parts[0] in {"backups", "restores"} and request.method != "GET":
        if MaintenanceOperation.objects.filter(pk=state.active_maintenance_id, kind__in=["clear", "reset"]).exists():
            raise ApiError("MAINTENANCE_BUSY", "正在清理数据，请等待操作完成", 409)
    if route in {"data/clear", "data/reset"}:
        require_method(request, "POST")
        from runtime.data_reset import create_data_operation
        from runtime.maintenance import serialize_operation
        data = payload(request)
        return idempotent(request, data, lambda: ok(serialize_operation(create_data_operation(parts[1], data)), 202), runtime=True)
    if route == "session/logout":
        require_method(request, "POST")
        logout(request)
        return ok({"authenticated": False})
    if route == "session/passkeys":
        require_method(request, "GET")
        from runtime.authentication import passkey_inventory
        return ok(passkey_inventory(request))
    if len(parts) == 3 and parts[:2] == ["session", "passkeys"]:
        require_method(request, "PATCH", "DELETE")
        from runtime.authentication import rename_passkey, delete_passkey
        if request.method == "PATCH":
            return ok(rename_passkey(request, parts[2], payload(request)))
        return ok(delete_passkey(request, parts[2]))
    if route == "runtime/status":
        require_method(request, "GET")
        return ok(runtime_status())
    if route == "capture-network":
        require_method(request, "GET", "PATCH")
        from capture.proxy_config import status, save_config
        from capture.models import CaptureError
        try:
            if request.method == "PATCH":
                if settings.BOOKMARK_CAPTURE_MODE != "development":
                    raise ApiError("CAPTURE_PROXY_CONFIG", "本机代理仅适用于本机开发采集模式")
                save_config(DATA_ROOT, payload(request))
            return ok(status(DATA_ROOT))
        except CaptureError:
            raise ApiError("CAPTURE_PROXY_CONFIG", "代理配置无效，请使用本机 HTTP 代理地址（不含账号密码）") from None
    if route == "settings":
        require_method(request, "GET", "PATCH")
        config = catalog.library_settings()
        if request.method == "PATCH":
            data = payload(request)
            with transaction.atomic(using="library"):
                config.refresh_from_db()
                catalog.require_revision(config, data)
                old_default = config.default_refresh_seconds
                config.default_refresh_seconds = bounded_int(data.get("default_refresh_seconds"), config.default_refresh_seconds, 3600, 31536000)
                if "ui_defaults" in data:
                    if not isinstance(data["ui_defaults"], dict) or len(json.dumps(data["ui_defaults"])) > 10000:
                        raise ApiError("INVALID_SETTINGS", "界面设置无效")
                    config.ui_defaults = data["ui_defaults"]
                if "active_ai_profile_id" in data:
                    if data["active_ai_profile_id"]:
                        catalog.get_entity(AIProfile, data["active_ai_profile_id"])
                    config.active_ai_profile_id = data["active_ai_profile_id"] or None
                config.revision += 1
                config.save()
                if old_default != config.default_refresh_seconds:
                    now = timezone.now()
                    for capture in CaptureState.objects.filter(bookmark__refresh_interval_seconds__isnull=True):
                        capture.next_due_at = capture.last_success_at + timedelta(seconds=config.default_refresh_seconds) if capture.last_success_at else now
                        capture.save(update_fields=["next_due_at"])
        return ok({"revision": config.revision, "tree_revision": config.tree_revision, "default_refresh_seconds": config.default_refresh_seconds, "ui_defaults": config.ui_defaults, "active_ai_profile_id": str(config.active_ai_profile_id) if config.active_ai_profile_id else None})
    if route == "bookmarks":
        require_method(request, "GET", "POST")
        if request.method == "GET":
            return list_bookmarks(request)
        data = payload(request)
        def add():
            bookmark, created = catalog.create_bookmark(data)
            result = serialize_bookmark(catalog.get_entity(Bookmark, bookmark.id), detail=True)
            result["created"] = created
            return ok(result, 201 if created else 200)
        return idempotent(request, data, add)
    if route == "bookmarks/delete-preview":
        require_method(request, "POST")
        data = payload(request)
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BOOKMARK_BATCH:
            raise ApiError("INVALID_BATCH", f"请选择 1–{MAX_BOOKMARK_BATCH} 个网站")
        result = catalog.deletion_preview("bookmark", ids)
        result.pop("digest")
        return ok(result)
    if route == "bookmarks/bulk":
        require_method(request, "POST")
        data = payload(request)
        return idempotent(request, data, lambda: ok(bulk_bookmarks(data)))
    if parts[0] == "bookmarks" and len(parts) >= 2:
        bookmark = catalog.get_entity(Bookmark, parts[1])
        if len(parts) == 2:
            require_method(request, "GET", "PATCH")
            if request.method == "PATCH":
                bookmark = catalog.update_bookmark(bookmark.id, payload(request))
            return ok(serialize_bookmark(bookmark, variant_of(request), True))
        if parts[2] == "refresh":
            require_method(request, "POST")
            if not bookmark.snapshot_enabled:
                return ok({"state": "skipped", "reason": "SNAPSHOT_DISABLED"})
            group = queue.request_refresh([bookmark.id])
            return ok(queue.group_status(group), 202)
        if parts[2] == "snapshots":
            require_method(request, "GET")
            variant = variant_of(request)
            capture = CaptureState.objects.filter(bookmark=bookmark, variant=variant).first()
            versions = list(bookmark.snapshots.filter(variant=variant).select_related("artifact").order_by("-captured_at", "-id"))
            versions, page = paginate(versions, request)
            return ok([serialize_snapshot(v, capture.current_version_id if capture else None) for v in versions], page=page)
    if route == "folders":
        require_method(request, "GET", "POST")
        if request.method == "POST":
            return ok(serialize_folder(catalog.create_folder(payload(request))), 201)
        folders = catalog.folder_map()
        counts = catalog.folder_counts(folders)
        return ok([serialize_folder(f, folders, counts) for f in sorted(folders.values(), key=lambda f: (f.position, catalog.text_key(f.name)))])
    if parts[0] == "folders" and len(parts) == 3 and parts[2] in ("refresh-preview", "refresh"):
        require_method(request, "POST")
        ids = folder_bookmark_ids(parts[1])
        if parts[2] == "refresh-preview":
            return ok({"total": len(ids), "eligible": Bookmark.objects.filter(pk__in=ids, snapshot_enabled=True).count(), "disabled": Bookmark.objects.filter(pk__in=ids, snapshot_enabled=False).count(), "active": Job.objects.filter(target_id__in=ids, kind="capture", state__in=queue.ACTIVE).count()})
        return ok(queue.group_status(queue.request_refresh(ids, source="all" if parts[1] == "all" else "folder")), 202)
    if parts[0] == "folders" and len(parts) >= 2:
        folder = catalog.get_entity(Folder, parts[1])
        if len(parts) == 2:
            require_method(request, "PATCH", "DELETE")
            data = payload(request)
            if request.method == "PATCH":
                return ok(serialize_folder(catalog.update_folder(folder.id, data)))
            catalog.require_revision(folder, data)
            return ok(catalog.delete_catalog("folder", [folder.id], data.get("preview_token")))
        if parts[2] == "delete-preview":
            require_method(request, "POST")
            result = catalog.deletion_preview("folder", [folder.id])
            result.pop("digest")
            return ok(result)
    if route == "tags":
        require_method(request, "GET", "POST")
        if request.method == "POST":
            return ok(serialize_tag(catalog.create_tag(payload(request))), 201)
        return ok([serialize_tag(t) for t in Tag.objects.annotate(count=Count("bookmarks")).order_by("name_key")])
    if parts[0] == "tags" and len(parts) == 2:
        require_method(request, "PATCH", "DELETE")
        data = payload(request)
        with transaction.atomic(using="library"):
            tag = catalog.get_entity(Tag, parts[1])
            catalog.require_revision(tag, data)
            if request.method == "DELETE":
                tag.delete()
                return ok({"deleted": True})
            tag.name, tag.name_key = catalog.clean_name(data.get("name"))
            tag.revision += 1
            tag.save()
        return ok(serialize_tag(tag))
    if route == "job-groups":
        require_method(request, "GET")
        number = bounded_int(request.GET.get("page"), 1, high=1000000)
        size = bounded_int(request.GET.get("page_size"), 48)
        groups, summary, page = queue.task_catalog(number, size)
        return ok({"groups": groups, **summary}, page=page)
    if parts[0] == "job-groups" and len(parts) == 2:
        require_method(request, "GET")
        return ok(queue.group_status(catalog.get_entity(JobGroup, parts[1])))
    if route == "jobs":
        require_method(request, "GET")
        jobs = Job.objects.filter(state__in=queue.ACTIVE).order_by("-priority", "created_at")[:100]
        return ok([{ "id": str(j.id), "kind": j.kind, "state": j.state, "variant": j.variant, "error_code": j.error_code} for j in jobs])
    if parts[0] == "jobs" and len(parts) >= 2:
        job = catalog.get_entity(Job, parts[1])
        if len(parts) == 2:
            require_method(request, "GET")
            return ok({"id": str(job.id), "kind": job.kind, "state": job.state, "error_code": job.error_code, "result": job.result, "attempt_count": job.attempt_count})
        require_method(request, "POST")
        if parts[2] == "cancel":
            Job.objects.filter(pk=job.id, state__in=queue.ACTIVE).update(state="cancelled", active_key=None, error_code="USER_CANCELLED")
            if job.kind == "import":
                ImportBatch.objects.filter(pk=job.target_id, stage="records", state__in=("queued", "running")).update(state="cancelled")
            return ok({"id": str(job.id), "state": "cancelled"})
        if parts[2] == "retry":
            new = queue.retry_job(job.id)
            return ok({"job_id": str(new.id), "state": new.state}, 202)
    if parts[0] == "snapshots" and len(parts) == 3 and parts[2] == "thumbnail":
        require_method(request, "GET", "HEAD")
        from library.preview_cache import preview_response
        return preview_response(request, catalog.get_entity(SnapshotVersion, parts[1]), generation=state.active_generation)
    if parts[0] == "snapshots" and len(parts) == 3 and parts[2] == "preview":
        require_method(request, "GET", "HEAD")
        version = catalog.get_entity(SnapshotVersion, parts[1])
        path = artifact_path(version.artifact_id, version.artifact.relative_path)
        if not path.is_file():
            raise ApiError("ARTIFACT_MISSING", "归档文件缺失", 404)
        response = FileResponse(path.open("rb"), content_type="text/html; charset=utf-8")
        response["Content-Security-Policy"] = "sandbox; default-src 'none'; script-src 'none'; connect-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline' data:; frame-src data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        response["X-Frame-Options"] = "SAMEORIGIN"
        return response
    if route in ("snapshots/delete-preview", "snapshots/bulk-delete"):
        require_method(request, "POST")
        data = payload(request)
        ids = data.get("ids", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ApiError("INVALID_BATCH", "请选择 1–100 个版本")
        with transaction.atomic(using="library"):
            versions = list(SnapshotVersion.objects.filter(pk__in=ids).order_by("id"))
            if len(versions) != len(set(ids)):
                raise ApiError("NOT_FOUND", "部分版本不存在", 404)
            if CaptureState.objects.filter(current_version_id__in=ids).exists():
                raise ApiError("CURRENT_SNAPSHOT_PROTECTED", "每种设备的最新成功版本必须保留", 409)
            runtime = queue.runtime_state()
            digest = hashlib.sha256(json.dumps({"versions": [(str(v.id), v.artifact_id) for v in versions], "generation": runtime.active_generation, "library_epoch": runtime.library_epoch}, sort_keys=True).encode()).hexdigest()
            if route.endswith("delete-preview"):
                return ok({"version_count": len(versions), "preview_token": signing.dumps({"digest": digest}, salt="history-delete")})
            try:
                token = signing.loads(data.get("preview_token"), salt="history-delete", max_age=600)
            except (signing.BadSignature, TypeError):
                raise ApiError("PREVIEW_STALE", "清理预览已失效", 409)
            if token.get("digest") != digest:
                raise ApiError("PREVIEW_STALE", "清理范围已变化", 409)
            SnapshotVersion.objects.filter(pk__in=ids).delete()
            transaction.on_commit(lambda: queue.enqueue("cleanup", priority=0, active_key="cleanup"), using="library")
        return ok({"deleted_count": len(versions)})
    if parts[0] == "ai-profiles":
        from library.classification import configure_profile
        if len(parts) == 1:
            require_method(request, "GET", "POST")
            if request.method == "GET":
                return ok([serialize_profile(p) for p in AIProfile.objects.all()])
            return ok(serialize_profile(configure_profile(payload(request))), 201)
        profile = catalog.get_entity(AIProfile, parts[1])
        if len(parts) == 2:
            require_method(request, "GET", "PATCH")
            if request.method == "PATCH":
                profile = configure_profile(payload(request), profile.id)
            return ok(serialize_profile(profile))
        if parts[2] == "test":
            require_method(request, "POST")
            job = queue.enqueue("ai_test", profile.id, priority=60, active_key=f"ai-test:{profile.id}:{profile.revision}")
            return ok({"job_id": str(job.id), "state": job.state}, 202)
    if route.startswith("classification/"):
        from library import classification
        if route == "classification/batches":
            require_method(request, "POST")
            data = payload(request)
            ids = data.get("bookmark_ids", data.get("ids", []))
            if data.get("folder_id"):
                ids = folder_bookmark_ids(data["folder_id"])
            return ok(queue.group_status(queue.request_classification(ids, data.get("profile_id"))), 202)
        if route == "classification/proposals":
            require_method(request, "GET")
            statuses = request.GET.get("status", "pending,stale").split(",")
            proposals = ClassificationProposal.objects.filter(status__in=statuses)
            if request.GET.get("import_id"):
                batch = catalog.get_entity(ImportBatch, request.GET["import_id"])
                proposals = proposals.filter(batch__job_group_id=batch.classification_group_id) if batch.classification_group_id else proposals.none()
            if request.GET.get("bookmark_id"):
                bookmark = catalog.get_entity(Bookmark, request.GET["bookmark_id"])
                proposals = proposals.filter(bookmark=bookmark)
            if request.GET.get("group_id"):
                group = catalog.get_entity(JobGroup, request.GET["group_id"])
                proposals = proposals.filter(batch__job_group=group)
            if request.GET.get("selection") == "true":
                rows = list(proposals.order_by("created_at", "id").values("id", "revision")[:classification.MAX_REVIEW_ITEMS + 1])
                if len(rows) > classification.MAX_REVIEW_ITEMS:
                    raise ApiError("INVALID_BATCH", f"一次最多选择 {classification.MAX_REVIEW_ITEMS} 项")
                return ok([{"id": str(row["id"]), "revision": row["revision"]} for row in rows])
            proposals = list(proposals.select_related("bookmark", "target_folder", "batch").prefetch_related("bookmark__sources").order_by("created_at", "id"))
            proposals, page = paginate(proposals, request)
            return ok([classification.serialize_proposal(p) for p in proposals], page=page)
        if len(parts) == 3 and parts[1] == "proposals":
            require_method(request, "PATCH")
            return ok(classification.serialize_proposal(classification.edit_proposal(parts[2], payload(request))))
        if route in ("classification/accept", "classification/reject"):
            require_method(request, "POST")
            data = payload(request)
            items = selected_items(data, classification.MAX_REVIEW_ITEMS)
            def classify():
                result = classification.accept_proposals(items, data.get("tree_revision")) if route.endswith("accept") else classification.reject_proposals(items)
                return ok(result if isinstance(result, dict) else {"updated_count": len(items)})
            return idempotent(request, data, classify)
    if route == "imports" and request.method == "GET":
        from library.importing import serialize_import
        from django.db.models import Q
        batches = ImportBatch.objects.filter(Q(state__in=("queued", "running")) | Q(stage="review", review_notified=False)).select_related("capture_group", "classification_group").order_by("created_at")
        return ok([serialize_import(batch) for batch in batches])
    if route in ("imports", "imports/preview"):
        require_method(request, "POST")
        from library.importing import preview_import, serialize_import, start_import
        uploaded = request.FILES.get("file")
        if not uploaded:
            raise ApiError("FILE_REQUIRED", "请选择浏览器导出的 HTML 文件")
        if uploaded.size > 20*1024*1024:
            raise ApiError("FILE_TOO_LARGE", "导入文件不能超过 20 MiB", 413)
        content = uploaded.read()
        def import_file():
            batch = preview_import(content)
            if route == "imports":
                start_import(batch)
            return ok(serialize_import(batch), 202 if route == "imports" else 201)
        return idempotent(request, {"file_sha256": hashlib.sha256(content).hexdigest()}, import_file)
    if parts[0] == "imports" and len(parts) >= 2:
        from library.importing import serialize_import, start_import
        batch = catalog.get_entity(ImportBatch, parts[1])
        if len(parts) == 2:
            require_method(request, "GET")
            return ok(serialize_import(batch))
        if parts[2] == "execute":
            require_method(request, "POST")
            data = payload(request)
            return idempotent(request, data, lambda: ok(serialize_import(start_import(batch)), 202))
        if parts[2] == "review-notified":
            require_method(request, "POST")
            if batch.stage != "review":
                raise ApiError("REVIEW_NOT_READY", "导入工作流程尚未完成", 409)
            ImportBatch.objects.filter(pk=batch.id).update(review_notified=True)
            return ok({"id": str(batch.id), "review_notified": True})
    if parts[0] in ("backups", "restores", "maintenance-operations"):
        from runtime import maintenance
        if route == "backups":
            require_method(request, "POST")
            data = payload(request)
            return idempotent(request, data, lambda: ok(maintenance.serialize_operation(maintenance.create_backup_operation()), 202), runtime=True)
        if route == "restores/validate":
            require_method(request, "POST")
            uploaded = request.FILES.get("file")
            if not uploaded:
                raise ApiError("FILE_REQUIRED", "请选择备份包")
            digest = hashlib.sha256()
            for chunk in uploaded.chunks():
                digest.update(chunk)
            uploaded.seek(0)
            return idempotent(request, {"package_sha256": digest.hexdigest()}, lambda: ok(maintenance.serialize_operation(maintenance.create_restore_validation(uploaded.chunks())), 202), runtime=True)
        if route == "maintenance-operations/lookup":
            require_method(request, "POST")
            data = payload(request)
            record = RuntimeIdempotency.objects.filter(owner_key="owner", method=data.get("method"), path=data.get("path"), key=data.get("idempotency_key")).first()
            if not record or not record.operation_id:
                raise ApiError("NOT_FOUND", "未找到该维护请求", 404)
            return ok(maintenance.serialize_operation(record.operation))
        if len(parts) >= 2:
            operation = catalog.get_entity(MaintenanceOperation, parts[1])
            if parts[0] == "backups" and operation.kind != "backup" or parts[0] == "restores" and operation.kind != "restore":
                raise ApiError("NOT_FOUND", "维护操作不存在", 404)
            if parts[0] == "restores" and parts[2:] == ["pre-backup", "download"]:
                require_method(request, "GET")
                path = DATA_ROOT / "backups" / f"{operation.id}.pre-restore.bookmarklib.zip"
                if not path.is_file():
                    raise ApiError("BACKUP_MISSING", "恢复前备份尚未生成", 404)
                return FileResponse(path.open("rb"), as_attachment=True, filename=path.name, content_type="application/zip")
            if len(parts) == 2:
                require_method(request, "GET")
                return ok(maintenance.serialize_operation(operation, include_confirmation=operation.kind == "restore"))
            if parts[0] == "backups" and parts[2] == "download":
                require_method(request, "GET")
                if operation.state != "succeeded":
                    raise ApiError("BACKUP_NOT_READY", "备份尚未完成", 409)
                path = DATA_ROOT / "backups" / f"{operation.id}.bookmarklib.zip"
                if not path.is_file() or not path.resolve().is_relative_to((DATA_ROOT / "backups").resolve()):
                    raise ApiError("BACKUP_MISSING", "备份文件不可用", 404)
                return FileResponse(path.open("rb"), as_attachment=True, filename=path.name, content_type="application/zip")
            if parts[0] == "restores" and parts[2] == "execute":
                require_method(request, "POST")
                data = payload(request)
                if data.get("overwrite") is not True and data.get("confirm_overwrite") is not True:
                    raise ApiError("OVERWRITE_CONFIRMATION_REQUIRED", "请确认覆盖当前收藏库")
                return idempotent(request, data, lambda: ok(maintenance.serialize_operation(maintenance.confirm_restore(operation.id, data.get("validation_token", data.get("token")), overwrite=True)), 202), runtime=True)
    raise ApiError("NOT_FOUND", "接口不存在", 404)
