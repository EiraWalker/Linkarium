"""Database-backed, single-consumer work queue with explicit execution identity."""
from datetime import timedelta
import uuid
from django.db import transaction, IntegrityError
from django.utils import timezone
from runtime.models import RuntimeState
from .catalog import library_settings
from .errors import ApiError
from .models import Job, JobAttempt, JobGroup, JobGroupMember, Bookmark, CaptureState, AIProfile

ACTIVE = ("queued", "running", "retry_wait")
TERMINAL = ("succeeded", "failed", "cancelled")


def runtime_state():
    return RuntimeState.objects.get_or_create(pk=1)[0]


@transaction.atomic(using="library")
def enqueue(kind, target_id=None, variant="", priority=40, payload=None, group=None, input_revision=1, active_key=None):
    state = runtime_state()
    if state.maintenance_mode:
        raise ApiError("MAINTENANCE", "收藏库正在维护，请稍后重试", 503)
    existing = Job.objects.filter(active_key=active_key).first() if active_key else None
    if existing and existing.state in ("queued", "retry_wait") and (priority > existing.priority or priority >= 60):
        existing.priority = max(priority, existing.priority)
        if priority >= 60:
            existing.available_at = timezone.now()
        existing.save(update_fields=["priority", "available_at", "updated_at"])
    job = existing or Job.objects.create(kind=kind, target_id=target_id, variant=variant, priority=priority, payload=payload or {}, input_revision=input_revision, active_key=active_key, available_at=timezone.now(), generation=state.active_generation, library_epoch=state.library_epoch)
    if group:
        JobGroupMember.objects.get_or_create(group=group, job=job, defaults={"bookmark_id": target_id if kind in ("capture", "metadata") else None})
    return job


def request_refresh(bookmark_ids, source="manual", priority=60):
    ids = set(str(i) for i in bookmark_ids)
    bookmarks = list(Bookmark.objects.filter(id__in=ids))
    group = JobGroup.objects.create(kind="refresh", source=source, requested_count=len(bookmarks))
    for bookmark in bookmarks:
        if not bookmark.snapshot_enabled:
            continue
        for variant in ("desktop", "mobile"):
            enqueue("capture", bookmark.id, variant, priority, group=group, input_revision=bookmark.capture_epoch, active_key=f"capture:{bookmark.id}:{variant}:{bookmark.capture_epoch}")
    return group


def retry_failed_captures(*, execute=False, expected_generation=None, expected_library_epoch=None):
    """Plan or enqueue only currently failed device snapshots, preserving history.

    A recovery batch owns new jobs instead of moving memberships from historical
    batches. Holding the runtime lock before the library transaction prevents a
    maintenance/generation switch while the plan is rechecked and enqueued.
    """
    initial = RuntimeState.objects.filter(pk=1).first()
    if initial is None:
        raise ApiError("NOT_INITIALIZED", "请先初始化收藏库", 409)
    generation = expected_generation if expected_generation is not None else initial.active_generation
    epoch = expected_library_epoch if expected_library_epoch is not None else initial.library_epoch

    def plan(state):
        if state.maintenance_mode:
            raise ApiError("MAINTENANCE", "收藏库正在维护，请稍后重试", 503)
        if state.active_generation != generation or state.library_epoch != epoch:
            raise ApiError("ATTEMPT_EXPIRED", "收藏库已切换，请重新检查失败快照", 409)
        # Long-lived API/CLI processes must not enqueue against a generation
        # opened before the runtime switched. Django's test DBs are in memory.
        from pathlib import Path
        from django.db import connections
        from config.paths import generation_root
        connection = connections["library"]
        database = connection.settings_dict["NAME"]
        if not connection.is_in_memory_db() and Path(database).resolve() != (generation_root(generation) / "library.sqlite3").resolve():
            raise ApiError("ATTEMPT_EXPIRED", "收藏库连接已失效，请重新连接", 409)
        failures = list(CaptureState.objects.filter(bookmark__snapshot_enabled=True,
            variant__in=("desktop", "mobile")).exclude(last_error_code="")
            .select_related("bookmark").order_by("bookmark_id", "variant"))
        keys = {f"capture:{item.bookmark_id}:{item.variant}:{item.bookmark.capture_epoch}": item for item in failures}
        active = {job.active_key: job for job in Job.objects.filter(active_key__in=keys)}
        pending = []
        for key, item in keys.items():
            existing = active.get(key)
            if existing:
                if (existing.state not in ACTIVE or existing.kind != "capture"
                        or existing.target_id != item.bookmark_id or existing.variant != item.variant
                        or existing.generation != generation or existing.library_epoch != epoch
                        or existing.input_revision != item.bookmark.capture_epoch):
                    raise ApiError("ATTEMPT_EXPIRED", "存在已失效的快照任务，请先检查任务状态", 409)
                continue
            pending.append((key, item))
        report = {"execute": execute, "generation": generation, "library_epoch": epoch,
            "failed_devices": len(failures), "failed_websites": len({item.bookmark_id for item in failures}),
            "already_active_devices": len(active), "to_queue_devices": len(pending),
            "to_queue_websites": len({item.bookmark_id for _, item in pending}),
            "queued_devices": 0, "group_id": None}
        return pending, report

    if not execute:
        return plan(initial)[1]
    with transaction.atomic(using="default"):
        locked = RuntimeState.objects.select_for_update().get(pk=1)
        with transaction.atomic(using="library"):
            pending, report = plan(locked)
            if not pending:
                return report
            group = JobGroup.objects.create(kind="refresh", source="failed-capture-recovery",
                requested_count=report["to_queue_websites"])
            for key, item in pending:
                enqueue("capture", item.bookmark_id, item.variant, priority=60, group=group,
                    input_revision=item.bookmark.capture_epoch, active_key=key)
            report.update(queued_devices=len(pending), group_id=str(group.id))
            return report


@transaction.atomic(using="library")
def retry_job(identity):
    from .catalog import get_entity
    job = get_entity(Job, identity)
    if job.state in ("queued", "retry_wait"):
        state = runtime_state()
        if state.maintenance_mode:
            raise ApiError("MAINTENANCE", "收藏库正在维护，请稍后重试", 503)
        if job.generation != state.active_generation or job.library_epoch != state.library_epoch:
            raise ApiError("ATTEMPT_EXPIRED", "任务已失效，请重新提交", 409)
        if job.kind in ("capture", "metadata"):
            bookmark = get_entity(Bookmark, job.target_id)
            if job.kind == "capture" and not bookmark.snapshot_enabled:
                raise ApiError("SNAPSHOT_DISABLED", "该网站已关闭快照", 409)
            if bookmark.capture_epoch != job.input_revision:
                raise ApiError("ATTEMPT_EXPIRED", "网页采集设置已变化，请重新刷新", 409)
        # Repeated clicks on the same waiting job never create another job.
        if job.state == "queued":
            return job
        now = timezone.now()
        changed = Job.objects.filter(pk=job.id, state="retry_wait").update(
            state="queued", available_at=now, priority=max(60, job.priority),
            active_attempt_id=None, error_code="", updated_at=now)
        if not changed:
            raise ApiError("JOB_ACTIVE", "任务已开始执行", 409)
        job.refresh_from_db()
        return job
    if job.state not in TERMINAL:
        raise ApiError("JOB_ACTIVE", "任务仍在执行", 409)
    if job.kind in ("capture", "metadata"):
        bookmark = get_entity(Bookmark, job.target_id)
        if job.kind == "capture" and not bookmark.snapshot_enabled:
            raise ApiError("SNAPSHOT_DISABLED", "该网站已关闭快照", 409)
        new = enqueue(job.kind, job.target_id, job.variant, 60, input_revision=bookmark.capture_epoch,
                      active_key=f"{job.kind}:{bookmark.id}:{job.variant}:{bookmark.capture_epoch}")
    else:
        new = enqueue(job.kind, job.target_id, priority=60, payload=job.payload,
                      active_key=f"retry:{job.id}:{runtime_state().library_epoch}")
    # Keep the website/batch denominator unchanged and let every affected
    # progress dialog follow the replacement, including shared capture jobs.
    from .models import JobGroupMember
    for member in list(job.memberships.all()):
        JobGroupMember.objects.get_or_create(group_id=member.group_id, job=new,
                                            defaults={"bookmark_id": member.bookmark_id})
        member.delete()
    return new


def initial_bookmark(identity):
    bookmark = Bookmark.objects.filter(pk=identity).first()
    if not bookmark:
        return
    enqueue("metadata", identity, priority=45, input_revision=bookmark.capture_epoch, active_key=f"metadata:{identity}:{bookmark.capture_epoch}")
    if bookmark.snapshot_enabled:
        request_refresh([identity], source="new", priority=40)
    settings = library_settings()
    if settings.active_ai_profile_id:
        from .classification import verified_client
        try:
            verified_client(settings.active_ai_profile_id)
        except Exception:
            return
        request_classification([identity], settings.active_ai_profile_id, priority=30)


@transaction.atomic(using="library")
def request_classification(bookmark_ids, profile_id=None, priority=50):
    ids = list(dict.fromkeys(str(i) for i in bookmark_ids))
    if not ids or len(ids) > 1000:
        raise ApiError("INVALID_BATCH", "请选择 1–1000 个网站")
    if Bookmark.objects.filter(pk__in=ids).count() != len(ids):
        raise ApiError("NOT_FOUND", "部分网站不存在", 404)
    profile_id = profile_id or library_settings().active_ai_profile_id
    if not profile_id:
        raise ApiError("AI_UNCONFIGURED", "请先配置并验证 AI 服务")
    from .classification import verified_client
    verified_client(profile_id)
    from .submissions import supersede
    supersede(ids)
    # A new review replaces older work fully covered by this request. Otherwise
    # a late response from a previous full-library run could overwrite it.
    selected = set(ids)
    for old in Job.objects.filter(kind="classification", state__in=ACTIVE):
        previous = set(old.payload.get("bookmark_ids", []))
        if previous and previous <= selected:
            Job.objects.filter(pk=old.id, state__in=ACTIVE).update(
                state="cancelled", active_key=None, error_code="TASK_SUPERSEDED")
    from .models import ClassificationProposal
    ClassificationProposal.objects.filter(bookmark_id__in=ids, status="pending").update(status="stale")
    group = JobGroup.objects.create(kind="classification", source="manual", requested_count=len(ids))
    for offset in range(0, len(ids), 10):
        enqueue("classification", priority=priority, payload={"bookmark_ids": ids[offset:offset+10], "profile_id": str(profile_id), "group_id": str(group.id)}, group=group)
    return group


def schedule_due():
    now = timezone.now()
    due = CaptureState.objects.filter(bookmark__snapshot_enabled=True, bookmark__auto_refresh_paused=False, next_due_at__lte=now).select_related("bookmark")
    for state in due:
        bookmark = state.bookmark
        enqueue("capture", bookmark.id, state.variant, 10, input_revision=bookmark.capture_epoch, active_key=f"capture:{bookmark.id}:{state.variant}:{bookmark.capture_epoch}")


@transaction.atomic(using="library")
def claim_next(prefer_background=False):
    if runtime_state().maintenance_mode:
        return None
    qs = Job.objects.filter(state__in=("queued", "retry_wait"), available_at__lte=timezone.now())
    if prefer_background and qs.filter(priority__lte=10).exists():
        qs = qs.filter(priority__lte=10)
    job = qs.order_by("-priority", "available_at", "created_at").first()
    if not job:
        return None
    now = timezone.now()
    attempt = JobAttempt.objects.create(job=job, attempt_no=job.attempt_count+1, heartbeat_at=now, lease_expires_at=now+timedelta(seconds=180), generation=job.generation, library_epoch=job.library_epoch, capture_epoch=job.input_revision, input_revision=job.input_revision)
    changed = Job.objects.filter(pk=job.id, state__in=("queued", "retry_wait")).update(state="running", attempt_count=job.attempt_count+1, active_attempt_id=attempt.id)
    if not changed:
        raise ApiError("CLAIM_CONFLICT", "任务已被领取", 409)
    job.refresh_from_db()
    return job, attempt


def execution_valid(job, attempt):
    state = runtime_state()
    return not state.maintenance_mode and state.active_generation == job.generation and state.library_epoch == job.library_epoch and Job.objects.filter(pk=job.id, state="running", active_attempt_id=attempt.id).exists() and JobAttempt.objects.filter(pk=attempt.id, state="running", lease_token=attempt.lease_token, lease_expires_at__gt=timezone.now()).exists()


@transaction.atomic(using="library")
def finish(job, attempt, result=None, error_code=None, retryable=False, allow_expired=False):
    current = Job.objects.filter(pk=job.id).first()
    if not current or current.active_attempt_id != attempt.id:
        return
    saved_attempt = JobAttempt.objects.filter(pk=attempt.id, lease_token=attempt.lease_token, state="running").first()
    if not saved_attempt:
        return
    now = timezone.now()
    if current.state == "cancelled":
        JobAttempt.objects.filter(pk=attempt.id, lease_token=attempt.lease_token).update(state="cancelled", finished_at=now)
        if job.kind == "import":
            from .models import ImportBatch
            ImportBatch.objects.filter(pk=job.target_id, state="running").update(state="cancelled")
        return
    if current.state != "running":
        return
    state = runtime_state()
    if state.active_generation != job.generation or state.library_epoch != job.library_epoch:
        return
    if saved_attempt.lease_expires_at <= now and not (allow_expired and error_code):
        return
    if error_code:
        terminal = "retry_wait" if retryable and current.attempt_count < current.max_attempts else "failed"
    else:
        terminal = "succeeded"
    JobAttempt.objects.filter(pk=attempt.id, lease_token=attempt.lease_token).update(state="failed" if terminal == "retry_wait" else terminal, error_code=error_code or "", finished_at=now)
    updates = {"state": terminal, "error_code": error_code or "", "result": result or {}}
    if terminal in TERMINAL:
        updates["active_key"] = None
    if terminal == "retry_wait":
        updates["available_at"] = now + timedelta(seconds=60 if current.attempt_count == 1 else 300)
    Job.objects.filter(pk=job.id, active_attempt_id=attempt.id).update(**updates)
    if job.kind == "capture" and error_code:
        bookmark = Bookmark.objects.filter(pk=job.target_id).first()
        if bookmark and bookmark.capture_epoch == job.input_revision:
            state, _ = CaptureState.objects.get_or_create(bookmark=bookmark, variant=job.variant)
            state.last_attempt_at, state.last_error_code = now, error_code
            state.failure_count += 1
            if terminal in TERMINAL:
                state.next_due_at = now + timedelta(seconds=bookmark.refresh_interval_seconds or library_settings().default_refresh_seconds)
            state.save()
    if job.kind == "import" and terminal == "failed":
        from .models import ImportBatch
        ImportBatch.objects.filter(pk=job.target_id, state="running").update(state="failed")


def group_state(states):
    active = sum(states[s] for s in ACTIVE)
    return ("running" if states["running"] else "queued" if states["queued"] else "retry_wait") if active else "partial" if states["succeeded"] and (states["failed"] or states["cancelled"]) else "failed" if states["failed"] else "cancelled" if states["cancelled"] and not states["succeeded"] else "skipped" if not sum(states.values()) else "succeeded"


def task_catalog(number=1, size=48):
    """Select the history page before building detailed group summaries."""
    from django.db.models import Count, Exists, OuterRef, Q, Subquery
    from .models import ClassificationProposal
    memberships = JobGroupMember.objects.filter(group_id=OuterRef('pk'))
    groups = JobGroup.objects.alias(
        has_running=Exists(memberships.filter(job__state='running')),
        has_active=Exists(memberships.filter(job__state__in=ACTIVE)))
    total = groups.count()
    active_count = groups.filter(has_active=True).count()
    newest = groups.filter(has_active=True) if active_count else groups
    current_id = newest.order_by('-created_at', '-id').values_list('id', flat=True).first()
    selected = list(groups.order_by('-has_running', '-has_active', '-created_at', '-id')
                    .values_list('id', flat=True)[(number-1)*size:number*size])
    needed = set(selected)
    if current_id:
        needed.add(current_id)
    pending = ClassificationProposal.objects.filter(batch__job_group_id=OuterRef("pk"), status="pending").values("batch__job_group_id").annotate(n=Count("id")).values("n")
    counts = {f"n_{state}": Count("members", filter=Q(members__job__state=state)) for state in (*ACTIVE, *TERMINAL)}
    details = {}
    for group in JobGroup.objects.filter(pk__in=needed).annotate(**counts, pending_count=Subquery(pending)):
        states = {state: getattr(group, f"n_{state}") for state in (*ACTIVE, *TERMINAL)}
        details[group.id] = {"id": str(group.id), "kind": group.kind, "source": group.source,
                     "created_at": group.created_at.isoformat(), "requested_count": group.requested_count,
                     "state": group_state(states), "total": sum(states.values()),
                     "completed": sum(states[s] for s in TERMINAL), "counts": states,
                     "pending_count": group.pending_count or 0}
    from .submissions import annotate_groups
    annotate_groups(details.values())
    return [details[identity] for identity in selected if identity in details], {
                  "active_count": active_count, "current_group": details.get(current_id),
                  "completion_revision": JobAttempt.objects.filter(finished_at__isnull=False).count(),
                  "pending_proposals": ClassificationProposal.objects.filter(status="pending").count()}, {
                  "number": number, "size": size, "total": total}


def group_status(group):
    jobs = [member.job for member in group.members.select_related("job")]
    states = {state: sum(job.state == state for job in jobs) for state in (*ACTIVE, *TERMINAL)}
    state = group_state(states)
    completed = states["succeeded"] + states["failed"] + states["cancelled"]
    result = {"id": str(group.id), "group_id": str(group.id), "kind": group.kind, "state": state, "requested_count": group.requested_count, "total": len(jobs), "completed": completed, "counts": states, "jobs": [{"id": str(j.id), "bookmark_id": str(j.target_id) if j.target_id else None, "variant": j.variant, "state": j.state, "error_code": j.error_code} for j in jobs], "skipped": max(0, group.requested_count - len({j.target_id for j in jobs if j.target_id})) if group.kind == "refresh" else 0}
    if group.kind == "classification":
        from .models import ClassificationProposal, ClassificationTaxonomy
        result["phase"] = "classifying" if ClassificationTaxonomy.objects.exists() else "planning"
        result["pending_count"] = ClassificationProposal.objects.filter(batch__job_group=group, status="pending").count()
        sites = {state: set() for state in (*ACTIVE, *TERMINAL)}
        for job, row in zip(jobs, result["jobs"]):
            identities = job.payload.get("bookmark_ids", [])
            row["website_count"] = len(identities)
            sites[job.state].update(identities)
        result.update(website_total=group.requested_count,
                      website_completed=len(set().union(*(sites[s] for s in TERMINAL))),
                      succeeded_sites=len(sites["succeeded"]), failed_sites=len(sites["failed"]),
                      cancelled_sites=len(sites["cancelled"]), queued_sites=len(sites["queued"]),
                      running_sites=len(sites["running"]), retrying_sites=len(sites["retry_wait"]))
    if group.kind == "refresh":
        by_site = {}
        for job in jobs:
            by_site.setdefault(job.target_id, []).append(job)
        completed_sites = partial_sites = succeeded_sites = failed_sites = 0
        for site_jobs in by_site.values():
            if any(j.state in ACTIVE for j in site_jobs):
                continue
            completed_sites += 1
            successes = sum(j.state == "succeeded" for j in site_jobs)
            if successes == len(site_jobs):
                succeeded_sites += 1
            elif successes:
                partial_sites += 1
            else:
                failed_sites += 1
        result.update(completed_sites=completed_sites, partial_sites=partial_sites, succeeded_sites=succeeded_sites, failed_sites=failed_sites, device_tasks={"total": len(jobs), "completed": completed, "counts": states})
    from .submissions import annotate_groups
    annotate_groups([result])
    return result
