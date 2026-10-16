"""Own catalog invariants, revision checks and deletion impact snapshots."""
import hashlib
import json
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from django.core import signing
from django.db import transaction, IntegrityError
from django.db.models import F
from django.utils import timezone
from .errors import ApiError
from .models import Bookmark, Folder, Tag, LibrarySettings, CaptureState, Job, SnapshotVersion


def text_key(value):
    return unicodedata.normalize("NFKC", str(value)).casefold()


def normalize_url(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ApiError("INVALID_URL", "请输入有效的网址")
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
        if not host or (port is not None and not 1 <= port <= 65535):
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        if port and (parsed.scheme.lower(), port) not in (("http", 80), ("https", 443)):
            authority += f":{port}"
        canonical = urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, parsed.fragment))
        return canonical, host
    except (ValueError, UnicodeError):
        raise ApiError("INVALID_URL", "仅支持不含登录凭据的 HTTP/HTTPS 网址")


def clean_name(value):
    if not isinstance(value, str):
        raise ApiError("INVALID_NAME", "名称不能为空")
    name = value.strip()
    if not name or len(name) > 100 or any(ord(c) < 32 or c in "/\\" for c in name):
        raise ApiError("INVALID_NAME", "名称应为 1–100 字符，不能含斜杠或控制字符")
    return name, text_key(name)


def require_revision(obj, data):
    if type(data.get("revision")) is not int or data["revision"] != obj.revision:
        raise ApiError("REVISION_CONFLICT", "内容已在另一设备更新，请重新读取后再提交", 409, {"revision": obj.revision})


def get_entity(model, identity):
    try:
        return model.objects.get(pk=identity)
    except (model.DoesNotExist, ValueError, TypeError):
        raise ApiError("NOT_FOUND", "记录不存在", 404)


def library_settings():
    return LibrarySettings.objects.get_or_create(pk=1)[0]


def tree_changed():
    library_settings()
    LibrarySettings.objects.filter(pk=1).update(tree_revision=F("tree_revision") + 1)


def folder_map():
    return {str(f.id): f for f in Folder.objects.all()}


def folder_counts(folders):
    """Count each occupied folder once along its ancestor chain."""
    from django.db.models import Count
    parents = {key: str(folder.parent_id) if folder.parent_id else None for key, folder in folders.items()}
    counts = dict.fromkeys(folders, 0)
    for row in Bookmark.objects.values('folder_id').annotate(count=Count('id')):
        current = str(row['folder_id'])
        seen = set()
        while current in parents and current not in seen:
            seen.add(current)
            counts[current] += row['count']
            current = parents[current]
    return counts


def folder_path(folder_id, folders=None):
    folders = folders if folders is not None else folder_map()
    parts, seen = [], set()
    current = str(folder_id) if folder_id else None
    while current:
        if current in seen or current not in folders:
            break
        seen.add(current)
        folder = folders[current]
        parts.append(folder.name)
        current = str(folder.parent_id) if folder.parent_id else None
    return " / ".join(reversed(parts))


def descendant_ids(identity, folders=None):
    folders = folders if folders is not None else folder_map()
    identity = str(identity)
    if identity not in folders:
        raise ApiError("NOT_FOUND", "文件夹不存在", 404)
    found, frontier = {identity}, [identity]
    while frontier:
        parent = frontier.pop()
        for key, folder in folders.items():
            if str(folder.parent_id) == parent and key not in found:
                found.add(key)
                frontier.append(key)
    return found


def validate_parent(folder_id, parent_id):
    if not parent_id:
        return
    folders = folder_map()
    if str(parent_id) not in folders:
        raise ApiError("NOT_FOUND", "目标文件夹不存在", 404)
    chain, current = set(), str(parent_id)
    while current:
        if current == str(folder_id) or current in chain:
            raise ApiError("FOLDER_CYCLE", "不能移动到自己或自己的子文件夹", 409)
        chain.add(current)
        current = str(folders[current].parent_id) if folders[current].parent_id else None
    descendants = descendant_ids(folder_id, folders) if str(folder_id) in folders else set()
    subtree_depth = 1
    for key in descendants:
        depth, cursor = 1, key
        while cursor != str(folder_id):
            cursor = str(folders[cursor].parent_id)
            depth += 1
        subtree_depth = max(subtree_depth, depth)
    if len(chain) + subtree_depth > 32:
        raise ApiError("FOLDER_DEPTH", "文件夹最多支持 32 层")


@transaction.atomic(using="library")
def create_folder(data):
    name, name_key = clean_name(data.get("name"))
    parent_id = data.get("parent_id") or None
    validate_parent(None, parent_id)
    if Folder.objects.filter(parent_id=parent_id, name_key=name_key).exists():
        raise ApiError("FOLDER_EXISTS", "该位置已存在同名文件夹", 409)
    folder = Folder.objects.create(name=name, name_key=name_key, parent_id=parent_id, position=int(data.get("position", 0)))
    tree_changed()
    return folder


@transaction.atomic(using="library")
def update_folder(identity, data):
    folder = get_entity(Folder, identity)
    require_revision(folder, data)
    parent_id = data.get("parent_id", folder.parent_id) or None
    validate_parent(folder.id, parent_id)
    name, name_key = clean_name(data.get("name", folder.name))
    if Folder.objects.filter(parent_id=parent_id, name_key=name_key).exclude(pk=folder.pk).exists():
        raise ApiError("FOLDER_EXISTS", "该位置已存在同名文件夹", 409)
    folder.name, folder.name_key, folder.parent_id = name, name_key, parent_id
    # Once explicitly edited, the folder belongs to the user's organization.
    folder.created_by_ai = False
    folder.position = int(data.get("position", folder.position))
    folder.revision += 1
    folder.save()
    tree_changed()
    return folder


@transaction.atomic(using="library")
def create_tag(data):
    name, name_key = clean_name(data.get("name"))
    tag, _ = Tag.objects.get_or_create(name_key=name_key, defaults={"name": name})
    return tag


def check_bookmark_fields(data):
    for field in ("is_starred", "snapshot_enabled", "auto_refresh_paused"):
        if field in data and type(data[field]) is not bool:
            raise ApiError("INVALID_FIELD", "选项必须为布尔值", field_errors={field: "请选择有效值"})
    if "notes" in data and (not isinstance(data["notes"], str) or len(data["notes"]) > 10000):
        raise ApiError("INVALID_FIELD", "备注最多 10000 字符")
    for field in ("title", "title_override"):
        if field in data and (not isinstance(data[field], str) or len(data[field]) > 500):
            raise ApiError("INVALID_FIELD", "标题最多 500 字符")
    interval = data.get("refresh_interval_seconds")
    if interval is not None and (type(interval) is not int or not 3600 <= interval <= 31536000):
        raise ApiError("INVALID_INTERVAL", "刷新间隔应在 1 小时至 1 年之间")
    if data.get("folder_id"):
        get_entity(Folder, data["folder_id"])
    if "tag_ids" in data:
        if not isinstance(data["tag_ids"], list) or len(data["tag_ids"]) > 100:
            raise ApiError("INVALID_TAGS", "请选择有效标签")
        ids = {str(value) for value in data["tag_ids"]}
        if Tag.objects.filter(pk__in=ids).count() != len(ids):
            raise ApiError("INVALID_TAGS", "标签不存在")


@transaction.atomic(using="library")
def create_bookmark(data, *, schedule_initial=True):
    canonical, domain = normalize_url(data.get("url", data.get("original_url", "")))
    existing = Bookmark.objects.filter(canonical_url=canonical).first()
    if existing:
        return existing, False
    check_bookmark_fields(data)
    bookmark = Bookmark.objects.create(original_url=data.get("url", canonical).strip(), canonical_url=canonical, domain=domain, source_title=data.get("source_title", "")[:500], title_override=data.get("title", data.get("title_override", "")), notes=data.get("notes", ""), folder_id=data.get("folder_id") or None, is_starred=data.get("is_starred", False), snapshot_enabled=data.get("snapshot_enabled", True), auto_refresh_paused=data.get("auto_refresh_paused", False), refresh_interval_seconds=data.get("refresh_interval_seconds"))
    if "tag_ids" in data:
        bookmark.tags.set(data["tag_ids"])
    for variant in ("desktop", "mobile"):
        CaptureState.objects.create(bookmark=bookmark, variant=variant, next_due_at=timezone.now())
    from . import queue
    if schedule_initial:
        transaction.on_commit(lambda: queue.initial_bookmark(bookmark.id), using="library")
    return bookmark, True


@transaction.atomic(using="library")
def update_bookmark(identity, data):
    bookmark = get_entity(Bookmark, identity)
    require_revision(bookmark, data)
    check_bookmark_fields(data)
    capture_changed, classification_changed = False, False
    if "url" in data:
        canonical, domain = normalize_url(data["url"])
        if Bookmark.objects.filter(canonical_url=canonical).exclude(pk=bookmark.pk).exists():
            raise ApiError("URL_EXISTS", "这个网址已存在于收藏库", 409)
        capture_changed = canonical != bookmark.canonical_url
        classification_changed = capture_changed
        bookmark.original_url, bookmark.canonical_url, bookmark.domain = data["url"], canonical, domain
        if capture_changed:
            from .models import PageMetadata
            PageMetadata.objects.filter(bookmark=bookmark).delete()
            bookmark.source_title, bookmark.final_url = "", ""
    for field in ("notes", "is_starred", "snapshot_enabled", "auto_refresh_paused", "refresh_interval_seconds"):
        if field in data:
            if field == "snapshot_enabled" and data[field] != bookmark.snapshot_enabled:
                capture_changed = True
            setattr(bookmark, field, data[field])
    if "folder_id" in data:
        value = data["folder_id"] or None
        classification_changed |= str(value) != str(bookmark.folder_id)
        bookmark.folder_id = value
    if "title" in data or "title_override" in data:
        title = data.get("title", data.get("title_override"))
        classification_changed |= title != bookmark.title_override
        bookmark.title_override = title
    bookmark.revision += 1
    if capture_changed:
        bookmark.capture_epoch += 1
    if classification_changed:
        bookmark.classification_revision += 1
    bookmark.save()
    if "tag_ids" in data:
        bookmark.tags.set(data["tag_ids"])
    if capture_changed:
        cancel_bookmark_jobs([bookmark.id])
        if "url" in data:
            from .queue import enqueue
            transaction.on_commit(lambda: enqueue("metadata", bookmark.id, priority=45, input_revision=bookmark.capture_epoch, active_key=f"metadata:{bookmark.id}:{bookmark.capture_epoch}"), using="library")
        if bookmark.snapshot_enabled:
            from .queue import request_refresh
            transaction.on_commit(lambda: request_refresh([bookmark.id], source="changed"), using="library")
    if "refresh_interval_seconds" in data:
        from datetime import timedelta
        seconds = bookmark.refresh_interval_seconds or library_settings().default_refresh_seconds
        for state in bookmark.capture_states.all():
            state.next_due_at = (state.last_success_at or timezone.now()) + timedelta(seconds=seconds)
            state.save(update_fields=["next_due_at"])
    return bookmark


def cancel_bookmark_jobs(ids):
    Job.objects.filter(target_id__in=ids, state__in=["queued", "running", "retry_wait"]).update(state="cancelled", active_key=None, error_code="OBJECT_CHANGED")


def deletion_preview(kind, identities):
    identities = sorted(str(value) for value in identities)
    folders = []
    if kind == "folder":
        folders = sorted(descendant_ids(identities[0]))
        bookmarks = list(Bookmark.objects.filter(folder_id__in=folders).order_by("id"))
    elif kind == "bookmark":
        bookmarks = list(Bookmark.objects.filter(pk__in=identities).order_by("id"))
        if len(bookmarks) != len(set(identities)):
            raise ApiError("NOT_FOUND", "部分网站不存在", 404)
    else:
        raise ApiError("INVALID_ACTION", "无效操作")
    versions = list(SnapshotVersion.objects.filter(bookmark_id__in=[b.id for b in bookmarks]).order_by("id").values_list("id", "artifact_id"))
    from .queue import runtime_state
    state = runtime_state()
    payload = {"kind": kind, "ids": identities, "generation": state.active_generation, "library_epoch": state.library_epoch, "folders": [(str(f.id), f.revision) for f in Folder.objects.filter(id__in=folders).order_by("id")], "bookmarks": [(str(b.id), b.revision, b.capture_epoch) for b in bookmarks], "versions": [(str(i), a) for i, a in versions]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    artifacts = {a for _, a in versions}
    reclaimable = sum(not SnapshotVersion.objects.filter(artifact_id=a).exclude(bookmark_id__in=[b.id for b in bookmarks]).exists() for a in artifacts)
    return {"bookmark_count": len(bookmarks), "folder_count": len(folders), "version_count": len(versions), "artifact_count": reclaimable, "preview_token": signing.dumps({"kind": kind, "ids": identities, "digest": digest}, salt="catalog-delete"), "digest": digest}


@transaction.atomic(using="library")
def delete_catalog(kind, identities, token):
    try:
        saved = signing.loads(token, salt="catalog-delete", max_age=600)
    except (signing.BadSignature, TypeError):
        raise ApiError("PREVIEW_STALE", "删除预览已失效，请重新预览", 409)
    current = deletion_preview(kind, identities)
    if saved.get("digest") != current["digest"] or saved.get("kind") != kind or saved.get("ids") != sorted(str(i) for i in identities):
        raise ApiError("PREVIEW_STALE", "待删除内容已变化，请重新预览", 409)
    if kind == "folder":
        folder_ids = descendant_ids(identities[0])
        bookmark_ids = list(Bookmark.objects.filter(folder_id__in=folder_ids).values_list("id", flat=True))
        cancel_bookmark_jobs(bookmark_ids)
        Folder.objects.filter(pk=identities[0]).delete()
        tree_changed()
    else:
        cancel_bookmark_jobs(identities)
        Bookmark.objects.filter(pk__in=identities).delete()
    from .queue import enqueue
    transaction.on_commit(lambda: enqueue("cleanup", priority=0, active_key="cleanup"), using="library")
    current.pop("digest", None)
    current.pop("preview_token", None)
    return current
