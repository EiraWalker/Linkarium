"""Durable browser import: records, dual-device captures, then human-reviewed classification."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 100000
MAX_EXECUTION_ENTRIES = 1000
MAX_CAPTURE_JOBS = 2000
MAX_AI_REQUESTS = 101  # One shared taxonomy request plus 100 ten-site batches.


def _ai_budget(count):
    return (count + 9) // 10 + bool(count)


def reserve_ai_request(batch_id):
    from django.core import signing
    from django.db.models import F
    from .models import ImportBatch
    from .ai_transport import AIError
    from . import queue
    batch = ImportBatch.objects.using('library').filter(pk=batch_id).first()
    try:
        authorization = signing.loads(batch.execution_authorization, salt='import-execution-v1') if batch else {}
    except (signing.BadSignature, TypeError):
        authorization = {}
    if (not batch or authorization.get('batch_id') != str(batch.id)
            or authorization.get('library_epoch') != queue.runtime_state().library_epoch
            or type(authorization.get('ai_requests')) is not int
            or not 0 <= authorization['ai_requests'] <= MAX_AI_REQUESTS
            or authorization['ai_requests'] != batch.ai_budget):
        raise AIError('AI_REQUEST_QUOTA', '本次导入没有有效的 AI 请求配额')
    updated = ImportBatch.objects.using('library').filter(pk=batch_id,
        ai_budget=authorization['ai_requests'], ai_requests_used__lt=F('ai_budget')).update(ai_requests_used=F('ai_requests_used') + 1)
    if updated != 1:
        raise AIError('AI_REQUEST_QUOTA', '本次导入的 AI 请求配额已用完')


def _preview_digest(batch):
    digest = hashlib.sha256(str(batch.id).encode())
    for row in batch.entries.order_by('id').values_list('id', 'url', 'title', 'original_folder_path', 'status', 'error_code').iterator(chunk_size=500):
        status, error = row[-2:]
        # Execution can discover a URL created after preview; that skip is still
        # the same authorized entry. An initially invalid/duplicate row cannot
        # become executable without changing the confirmation digest.
        eligibility = 'eligible' if status in ('pending', 'imported') or (status == 'duplicate' and error == 'URL_EXISTS') else status
        digest.update(json.dumps((*row[:-2], eligibility), default=str, ensure_ascii=False, separators=(',', ':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def import_plan(batch):
    from django.core import signing
    from . import queue
    count = batch.entries.filter(status__in=('pending', 'imported')).count()
    plan = {'batch_id': str(batch.id), 'digest': _preview_digest(batch),
            'library_epoch': queue.runtime_state().library_epoch,
            'entries': count, 'captures': count * 2, 'ai_requests': _ai_budget(count)}
    return {**plan, 'can_execute': count <= MAX_EXECUTION_ENTRIES,
            'entry_limit': MAX_EXECUTION_ENTRIES,
            'confirmation_token': signing.dumps(plan, salt='import-execution-v1', compress=True)}


class ImportError(ValueError):
    def __init__(self, code, message="Bookmark file could not be imported"):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ParsedBookmark:
    url: str
    title: str
    original_folder_path: list[str]
    error_code: str = ""


def canonicalize_import_url(url: str) -> str:
    """Same conservative identity rules as catalog; public reachability is separate."""
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ImportError("INVALID_URL")
    try:
        p = urlsplit(url.strip())
        if p.scheme not in {"http", "https"} or not p.hostname or p.username is not None or p.password is not None:
            raise ValueError
        host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if ":" in host:
            host = "[" + host + "]"
        port = p.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if port and port != {"http": 80, "https": 443}[p.scheme]:
            host += ":" + str(port)
        return urlunsplit((p.scheme.lower(), host, p.path or "/", p.query, p.fragment))
    except (ValueError, UnicodeError):
        raise ImportError("INVALID_URL") from None


class _NetscapeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries: list[ParsedBookmark] = []
        self.path: list[str] = []
        self.levels: list[bool] = []
        self.pending_folder: str | None = None
        self.text_kind: str | None = None
        self.text: list[str] = []
        self.href: str = ""
        self.anchor_path: list[str] = []
        self.seen_bookmark_marker = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "h3":
            self.text_kind, self.text = "folder", []
        elif tag == "a":
            self.text_kind, self.text = "bookmark", []
            self.href = attributes.get("href") or ""
            self.anchor_path = list(self.path)
            self.seen_bookmark_marker = True
        elif tag == "dl":
            added = self.pending_folder is not None
            if added:
                self.path.append(self.pending_folder)
                self.pending_folder = None
            self.levels.append(added)
            if len(self.levels) > 64:
                raise ImportError("IMPORT_DEPTH_LIMIT")

    def handle_data(self, data):
        if self.text_kind:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "h3" and self.text_kind == "folder":
            self.pending_folder = "".join(self.text).strip()[:100]
            self.text_kind = None
        elif tag == "a" and self.text_kind == "bookmark":
            title = "".join(self.text).strip()[:500]
            try:
                canonicalize_import_url(self.href)
                error = ""
            except ImportError as exc:
                error = exc.code
            self.entries.append(ParsedBookmark(self.href.strip(), title, self.anchor_path, error))
            self.text_kind = None
            if len(self.entries) > MAX_ENTRIES:
                raise ImportError("IMPORT_ENTRY_LIMIT")
        elif tag == "dl" and self.levels:
            if self.levels.pop():
                self.path.pop()


def parse_bookmark_html(content: bytes) -> list[ParsedBookmark]:
    if not isinstance(content, bytes) or len(content) > MAX_IMPORT_BYTES:
        raise ImportError("IMPORT_TOO_LARGE")
    # Chrome and current Firefox export UTF-8. A BOM is accepted; arbitrary
    # decoding replacement would silently corrupt URL identity or user names.
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise ImportError("IMPORT_ENCODING", "Export the bookmark file as UTF-8") from None
    parser = _NetscapeParser()
    parser.feed(text)
    parser.close()
    if not parser.seen_bookmark_marker and "NETSCAPE-Bookmark-file" not in text[:4096]:
        raise ImportError("IMPORT_FORMAT", "Expected a Chrome or Firefox bookmark HTML export")
    return parser.entries


def preview_import(content: bytes, source_type: str = "browser_html"):
    from django.db import transaction
    from .models import Bookmark, ImportBatch, ImportEntry
    parsed = parse_bookmark_html(content)
    existing = set(Bookmark.objects.using("library").values_list("canonical_url", flat=True))
    seen = set(existing)
    rows = []
    duplicate = invalid = 0
    for item in parsed:
        if item.error_code:
            invalid += 1
            state = "invalid"
        else:
            canonical = canonicalize_import_url(item.url)
            state = "duplicate" if canonical in seen else "pending"
            duplicate += int(state == "duplicate")
            seen.add(canonical)
        rows.append((item, state))
    with transaction.atomic(using="library"):
        batch = ImportBatch.objects.using("library").create(
            state="previewed", source_type=source_type, total=len(parsed),
            valid=len(parsed) - invalid - duplicate, duplicate=duplicate, invalid=invalid)
        ImportEntry.objects.using("library").bulk_create([
            ImportEntry(batch=batch, url=item.url, title=item.title,
                        original_folder_path=item.original_folder_path,
                        status=state, error_code=item.error_code)
            for item, state in rows
        ], batch_size=500)
    return batch


def execute_import(batch_id, *, create_callback=None, chunk_size=100, execution_guard=None):
    """Worker entry point. Every processed entry is committed with its bookmark."""
    from django.db import IntegrityError, transaction
    from .models import Bookmark, BookmarkSource, ImportBatch, ImportEntry, JobGroup
    from . import queue
    def check_execution():
        if execution_guard is not None and not execution_guard():
            from .errors import ApiError
            raise ApiError("ATTEMPT_EXPIRED", "任务已取消或失效", status=409)
    check_execution()
    batch = ImportBatch.objects.using("library").get(pk=batch_id)
    if batch.state == "succeeded":
        return batch
    if batch.stage == 'records':
        from django.core import signing
        from .errors import ApiError
        try:
            authorized = signing.loads(batch.execution_authorization, salt='import-execution-v1')
        except (signing.BadSignature, TypeError):
            raise ApiError('IMPORT_CONFIRMATION_REQUIRED', '请重新预览并确认导入工作量', 409) from None
        actual = batch.entries.filter(status__in=('pending', 'imported')).count()
        if (authorized.get('batch_id') != str(batch.id) or authorized.get('digest') != _preview_digest(batch)
                or authorized.get('library_epoch') != queue.runtime_state().library_epoch
                or type(authorized.get('entries')) is not int or not 0 <= actual <= authorized['entries'] <= MAX_EXECUTION_ENTRIES
                or batch.capture_budget != authorized.get('captures') or batch.capture_budget != 2 * authorized['entries']
                or batch.ai_budget != authorized.get('ai_requests') or batch.ai_budget != _ai_budget(authorized['entries'])):
            raise ApiError('IMPORT_CONFIRMATION_REQUIRED', '请重新预览并确认导入工作量', 409)
    with transaction.atomic(using="library"):
        check_execution()
        if batch.stage != "records":
            return batch
        if not batch.capture_group_id:
            batch.capture_group = JobGroup.objects.create(kind="refresh", source="import", requested_count=batch.valid)
        batch.state = "running"
        batch.save(update_fields=["state", "capture_group"])
    if create_callback is None:
        from .catalog import create_bookmark
        create_callback = lambda data: create_bookmark(data, schedule_initial=False)
    def enqueue_captures(bookmark):
        if bookmark.snapshot_enabled:
            from .models import CaptureState
            for variant in ('desktop', 'mobile'):
                if CaptureState.objects.filter(bookmark=bookmark, variant=variant, current_version__isnull=False).exists():
                    continue
                queue.enqueue('capture', bookmark.id, variant, 40, group=batch.capture_group,
                    input_revision=bookmark.capture_epoch,
                    active_key=f'capture:{bookmark.id}:{variant}:{bookmark.capture_epoch}')
    # Resume partial legacy imports and interrupted record creation without
    # dropping previously imported sites or duplicating their live capture jobs.
    for entry in batch.entries.filter(status='imported', bookmark__isnull=False).select_related('bookmark').iterator(chunk_size=100):
        with transaction.atomic(using='library'):
            check_execution()
            enqueue_captures(entry.bookmark)
    while True:
        entry_ids = list(ImportEntry.objects.using("library").filter(
            batch_id=batch_id, status="pending").order_by("id").values_list("id", flat=True)[:chunk_size])
        if not entry_ids:
            break
        for entry_id in entry_ids:
            with transaction.atomic(using="library"):
                check_execution()
                entry = ImportEntry.objects.using("library").get(pk=entry_id)
                if entry.status != "pending":
                    continue
                canonical = canonicalize_import_url(entry.url)
                if Bookmark.objects.using("library").filter(canonical_url=canonical).exists():
                    entry.status, entry.error_code = "duplicate", "URL_EXISTS"
                    entry.save(using="library", update_fields=["status", "error_code"])
                    continue
                try:
                    with transaction.atomic(using="library"):
                        result = create_callback({"url": entry.url, "title": entry.title,
                                                  "is_starred": False, "folder_id": None})
                        bookmark = result[0] if isinstance(result, tuple) else result
                        if isinstance(result, tuple) and not result[1]:
                            entry.status, entry.error_code = "duplicate", "URL_EXISTS"
                            entry.save(using="library", update_fields=["status", "error_code"])
                            continue
                        BookmarkSource.objects.using("library").create(
                            bookmark=bookmark, import_batch=batch,
                            source_folder_path=entry.original_folder_path, source_title=entry.title)
                        entry.bookmark, entry.status = bookmark, "imported"
                        entry.save(using="library", update_fields=["bookmark", "status"])
                        enqueue_captures(bookmark)
                except IntegrityError:
                    # Another add/import won URL uniqueness. Never modify it.
                    if not Bookmark.objects.using("library").filter(canonical_url=canonical).exists():
                        raise
                    entry.status, entry.error_code = "duplicate", "URL_EXISTS"
                    entry.save(using="library", update_fields=["status", "error_code"])
    entries = ImportEntry.objects.using("library").filter(batch_id=batch_id)
    with transaction.atomic(using="library"):
        check_execution()
        ImportBatch.objects.using("library").filter(pk=batch_id).update(
            state="running", stage="captures", duplicate=entries.filter(status="duplicate").count(),
            invalid=entries.filter(status="invalid").count(), valid=entries.filter(status="imported").count())
    return ImportBatch.objects.using("library").get(pk=batch_id)


def serialize_import(batch):
    from .models import ImportEntry, Job
    from . import queue
    entries = ImportEntry.objects.using("library").filter(batch=batch)
    imported = entries.filter(status="imported").count()
    capture = queue.group_status(batch.capture_group) if batch.capture_group_id else None
    classification = queue.group_status(batch.classification_group) if batch.classification_group_id else None
    fraction = lambda group: group["completed"] / max(1, group["total"]) if group else 0
    percent = 100 if batch.stage == "review" else (
        80 + int(19 * fraction(classification)) if batch.stage == "classification" else
        10 + int(70 * fraction(capture)) if batch.stage == "captures" else
        int(10 * (batch.total - entries.filter(status="pending").count()) / max(1, batch.total)))
    capture_jobs = Job.objects.filter(memberships__group_id=batch.capture_group_id, kind="capture") if capture else Job.objects.none()
    failed_ids = set(capture_jobs.filter(state__in=("failed", "cancelled")).values_list("target_id", flat=True))
    result = {"id": str(batch.id), "state": batch.state, "source_type": batch.source_type,
            "total": batch.total, "valid": batch.valid, "duplicate": batch.duplicate,
            "invalid": batch.invalid, "imported": imported,
            "pending": entries.filter(status="pending").count(),
            "stage": batch.stage, "progress": percent,
            "capture_total": capture["total"] if capture else 0,
            "capture_completed": capture["completed"] if capture else 0,
            "capture_failed_sites": len(failed_ids),
            "ai_status": batch.ai_status, "ai_error_code": batch.ai_error_code,
            "classification_total": classification["total"] if classification else 0,
            "classification_completed": classification["completed"] if classification else 0,
            "review_ready": batch.stage == "review", "review_notified": batch.review_notified,
            "exceeds_design_capacity": batch.total > 1000}
    if batch.state in ('preview', 'previewed'):
        result['execution_plan'] = import_plan(batch)
        result['valid'] = result['execution_plan']['entries']
    return result


def start_import(batch, confirmation_token=None):
    """Idempotently hand a parsed batch to the worker; repeated requests never restart it."""
    from django.db import transaction
    from . import queue
    from .errors import ApiError
    from django.core import signing
    with transaction.atomic(using="library"):
        batch.refresh_from_db()
        if batch.state in ("preview", "previewed") and batch.stage == "records":
            try:
                confirmed = signing.loads(confirmation_token, salt='import-execution-v1', max_age=900)
            except (signing.BadSignature, TypeError):
                raise ApiError('IMPORT_CONFIRMATION_REQUIRED', '请先预览并确认导入工作量', 409) from None
            expected = {key: value for key, value in import_plan(batch).items()
                        if key in ('batch_id', 'digest', 'library_epoch', 'entries', 'captures', 'ai_requests')}
            if confirmed != expected:
                raise ApiError('IMPORT_PREVIEW_CHANGED', '导入内容已变化，请重新预览', 409)
            if confirmed['entries'] > MAX_EXECUTION_ENTRIES:
                raise ApiError('IMPORT_WORKLOAD_LIMIT', f'每次最多执行 {MAX_EXECUTION_ENTRIES} 个网站的抓取和分类，请拆分文件', 409)
            if type(confirmed['captures']) is not int or confirmed['captures'] > MAX_CAPTURE_JOBS or confirmed['ai_requests'] > MAX_AI_REQUESTS:
                raise ApiError('IMPORT_WORKLOAD_LIMIT', '导入工作量超过配额', 409)
            if type(batch).objects.exclude(pk=batch.pk).filter(state__in=('queued', 'running')).exists():
                raise ApiError('IMPORT_BUSY', '已有导入正在执行，请等待完成', 409)
            batch.confirmation_digest = confirmed['digest']
            batch.execution_authorization = confirmation_token
            batch.valid = confirmed['entries']
            batch.capture_budget, batch.ai_budget = confirmed['captures'], confirmed['ai_requests']
            queue.enqueue("import", batch.id, priority=60, active_key=f"import:{batch.id}")
            batch.state = "queued"
            batch.save(update_fields=["state", 'confirmation_digest', 'execution_authorization', 'valid', 'capture_budget', 'ai_budget'])
    return batch


def _manual_review(batch, bookmark_ids):
    """Missing AI output becomes editable, unclassified rows, never an automatic move."""
    from .catalog import library_settings
    from .models import Bookmark, ClassificationBatch, ClassificationProposal, JobGroup
    if not batch.classification_group_id:
        batch.classification_group = JobGroup.objects.create(kind="classification", source="import", requested_count=len(bookmark_ids))
    existing = set(ClassificationProposal.objects.filter(batch__job_group=batch.classification_group).values_list("bookmark_id", flat=True))
    missing = Bookmark.objects.filter(pk__in=bookmark_ids).exclude(pk__in=existing)
    revision = library_settings().tree_revision
    review = ClassificationBatch.objects.create(profile=None, job_group=batch.classification_group, tree_revision=revision)
    ClassificationProposal.objects.bulk_create([
        ClassificationProposal(batch=review, bookmark=bookmark, expected_classification_revision=bookmark.classification_revision,
                               expected_tree_revision=revision, reason="未进行 AI 分类，请人工选择分类目标。")
        for bookmark in missing
    ])


def advance_imports():
    """Worker-owned stage transitions are atomic and recoverable after process restart."""
    from django.db import transaction
    from . import queue
    from .catalog import library_settings
    from .classification import verified_client
    from .models import ImportBatch, Job, JobGroup
    if queue.runtime_state().maintenance_mode:
        return
    for identity in ImportBatch.objects.filter(state="running", stage__in=("captures", "classification")).values_list("id", flat=True):
        with transaction.atomic(using="library"):
            if queue.runtime_state().maintenance_mode:
                return
            batch = ImportBatch.objects.get(pk=identity)
            ids = list(batch.entries.filter(status="imported", bookmark__isnull=False).values_list("bookmark_id", flat=True))
            if len(ids) > MAX_EXECUTION_ENTRIES or _ai_budget(len(ids)) > batch.ai_budget:
                batch.ai_status, batch.ai_error_code = 'skipped', 'IMPORT_WORKLOAD_LIMIT'
                batch.state = 'failed'
                batch.save()
                continue
            if batch.stage == "captures":
                if Job.objects.filter(memberships__group_id=batch.capture_group_id, state__in=queue.ACTIVE).exists():
                    continue
                profile_id = library_settings().active_ai_profile_id
                try:
                    if ids:
                        verified_client(profile_id)
                except Exception as error:
                    batch.ai_status, batch.ai_error_code = "skipped", getattr(error, "code", "AI_UNAVAILABLE")
                else:
                    if ids:
                        group = JobGroup.objects.create(kind="classification", source="import", requested_count=len(ids))
                        batch.classification_group = group
                        for offset in range(0, len(ids), 10):
                            job = queue.enqueue("classification", priority=30, payload={"bookmark_ids": [str(i) for i in ids[offset:offset+10]],
                                "profile_id": str(profile_id), "group_id": str(group.id), "import_id": str(batch.id)}, group=group)
                            Job.objects.filter(pk=job.id).update(max_attempts=1)
                        batch.stage, batch.ai_status = "classification", "running"
                        batch.save()
                        continue
                    batch.ai_status = "skipped"
            elif batch.stage == "classification":
                jobs = Job.objects.filter(memberships__group_id=batch.classification_group_id)
                error = jobs.filter(state__in=("failed", "cancelled")).first()
                if error:
                    # Stop further automatic calls after the provider becomes unavailable.
                    jobs.filter(state__in=("queued", "retry_wait")).update(state="cancelled", active_key=None, error_code="AI_UNAVAILABLE")
                    batch.ai_status, batch.ai_error_code = "partial" if jobs.filter(state="succeeded").exists() else "skipped", error.error_code
                if jobs.filter(state__in=queue.ACTIVE).exists():
                    continue
                if not error:
                    batch.ai_status = "succeeded"
            else:
                continue
            _manual_review(batch, ids)
            batch.state, batch.stage = "succeeded", "review"
            batch.save()
