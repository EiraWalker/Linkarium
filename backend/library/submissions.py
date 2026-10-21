"""Firefox's save-and-file workflow owns its authority, progress and recovery."""
from django.db import transaction
from django.utils import timezone

from . import catalog, queue
from .errors import ApiError
from .models import ExtensionSubmission, JobGroup, ClassificationProposal

ACTIVE = ("queued", "enriching", "classifying", "applying")
STALE_ERRORS = ("SUBMISSION_STALE", "PROPOSAL_STALE", "ATTEMPT_EXPIRED", "TASK_SUPERSEDED")
ERROR_MESSAGES = {"LOCAL_DEVICE_REQUIRED": "收藏已保存；请升级并绑定 Firefox 本机 AI 执行端后重新发起分类",
    "LOCAL_EXECUTOR_REQUIRED": "旧任务没有发起设备，请重新发起分类","AI_UNCONFIGURED": "请在 Linkarium 设置中配置并验证 AI 服务后重试",
    "AI_INVALID_RESULT": "AI 返回的分类无效，可以重试或在网页手动分类",
    "AI_TIMEOUT": "AI 服务响应超时，请稍后重试", "AI_RATE_LIMITED": "AI 服务繁忙，请稍后重试",
    **{code: "收藏或目录已变化，请在 Linkarium 网页处理分类" for code in STALE_ERRORS}}


def serialize(submission):
    bookmark = submission.bookmark
    job = submission.classification_job
    return {"execution_device_id": str(submission.execution_device_id) if submission.execution_device_id else None,
        "execution_state": job.execution_state if job else "",
        "execution_error": job.error_code if job else "","submission_id": str(submission.id),
        "bookmark_id": str(bookmark.id) if bookmark else None,
        "created": submission.created, "state": submission.state, "stage": submission.state,
        "group_id": str(submission.group_id) if submission.group_id else None,
        "status_url": f"/api/v1/extension/submissions/{submission.id}",
        "folder_path": catalog.folder_path(bookmark.folder_id) if bookmark else "",
        "error_code": submission.error_code, "metadata_error_code": submission.metadata_error_code,
        "error_message": ERROR_MESSAGES.get(submission.error_code, "后台分类未完成，请查看 Linkarium 后台任务" if submission.error_code else ""),
        "can_retry": bool(submission.execution_device_id) and submission.state == "needs_attention" and submission.error_code not in STALE_ERRORS and _current(submission),
        "revision": submission.revision,
        "applied_at": submission.applied_at.isoformat() if submission.applied_at else None}


@transaction.atomic(using="library")
def submit(data):
    if set(data) - {"url", "title"} or not isinstance(data.get("title", ""), str):
        raise ApiError("INVALID_INPUT", "仅支持网址和页面标题")
    from .local_ai import current_device
    device = current_device.get()
    bookmark, created = catalog.create_bookmark({**data, "is_starred": True}, schedule_initial=False)
    state = queue.runtime_state()
    submission = ExtensionSubmission.objects.create(bookmark=bookmark, created=created,
        execution_device_id=device.id if device else None,
        state="queued" if created else "already_saved", generation=state.active_generation,
        library_epoch=state.library_epoch, expected_bookmark_revision=bookmark.revision,
        expected_classification_revision=bookmark.classification_revision)
    if created:
        submission.group = JobGroup.objects.create(kind="extension", source="firefox-extension", requested_count=1)
        submission.metadata_job = queue.enqueue("metadata", bookmark.id, priority=45,
            group=submission.group, input_revision=bookmark.capture_epoch,
            active_key=f"metadata:{bookmark.id}:{bookmark.capture_epoch}")
        if bookmark.snapshot_enabled:
            queue.request_refresh([bookmark.id], source="firefox-extension", priority=40)
        submission.save()
    return submission


def get(identity):
    return catalog.get_entity(ExtensionSubmission, identity)


@transaction.atomic(using="library")
def retry(identity, data):
    submission = get(identity)
    catalog.require_revision(submission, data)
    from .local_ai import require_device
    if not submission.execution_device_id or require_device() != str(submission.execution_device_id):
        raise ApiError("DEVICE_FORBIDDEN", "只能由发起设备恢复分类", 403)
    if not submission.created or submission.state in ("classified", "already_saved"):
        raise ApiError("SUBMISSION_COMPLETE", "此收藏无需重试", 409)
    if not _current(submission) or submission.error_code in STALE_ERRORS:
        raise ApiError("SUBMISSION_STALE", "收藏或目录已变化，请在网页处理分类", 409)
    if submission.state in ACTIVE:
        return submission
    if submission.classification_job:
        if (submission.bookmark.classification_revision != submission.expected_classification_revision
                or catalog.library_settings().tree_revision != submission.expected_tree_revision):
            raise ApiError("SUBMISSION_STALE", "收藏或目录已变化，请在网页处理分类", 409)
        job = queue.retry_job(submission.classification_job_id)
        submission.classification_job = job
        submission.state = "classifying"
    else:
        submission.state = "queued"
    submission.error_code = ""
    submission.revision += 1
    submission.save()
    return submission


def _current(submission):
    state = queue.runtime_state()
    bookmark = submission.bookmark
    return (not state.maintenance_mode and submission.generation == state.active_generation
        and submission.library_epoch == state.library_epoch and bookmark is not None
        and bookmark.revision == submission.expected_bookmark_revision)


def _attention(submission, code):
    submission.state, submission.error_code = "needs_attention", code
    submission.revision += 1
    submission.save()


def supersede(bookmark_ids):
    """An explicit classification request withdraws earlier automatic authority."""
    for submission in ExtensionSubmission.objects.filter(bookmark_id__in=bookmark_ids, state__in=(*ACTIVE, "needs_attention")):
        _attention(submission, "TASK_SUPERSEDED")


def advance_submissions():
    """Called by the worker, never by status reads; survives process interruption."""
    if queue.runtime_state().maintenance_mode:
        return
    for identity in ExtensionSubmission.objects.filter(state__in=ACTIVE).values_list("id", flat=True):
        with transaction.atomic(using="library"):
            submission = get(identity)
            if submission.state not in ACTIVE:
                continue
            if not _current(submission):
                _attention(submission, "SUBMISSION_STALE")
                continue
            if submission.state in ("queued", "enriching"):
                metadata = submission.metadata_job
                if metadata and metadata.state in queue.ACTIVE:
                    if submission.state != "enriching":
                        submission.state = "enriching"
                        submission.save(update_fields=["state", "updated_at"])
                    continue
                submission.metadata_error_code = metadata.error_code if metadata else "METADATA_UNAVAILABLE"
                if not submission.execution_device_id:
                    _attention(submission, "LOCAL_DEVICE_REQUIRED")
                    continue
                submission.expected_classification_revision = submission.bookmark.classification_revision
                submission.expected_tree_revision = catalog.library_settings().tree_revision
                submission.classification_job = queue.enqueue("classification", priority=50,
                    payload={"bookmark_ids": [str(submission.bookmark_id)], "device_id": str(submission.execution_device_id),
                        "group_id": str(submission.group_id), "submission_id": str(submission.id)},
                    active_key=f"extension-classification:{submission.id}", group=submission.group)
                submission.state = "classifying"
                submission.save()
            elif not submission.classification_job or submission.classification_job.state in queue.TERMINAL:
                _attention(submission, submission.classification_job.error_code if submission.classification_job else "TASK_FAILED")



def annotate_groups(groups):
    """Do not report metadata success as successful automatic filing."""
    indexed = {str(row.get("id", row.get("group_id"))): row for row in groups if row.get("kind") == "extension"}
    if not indexed:
        return
    for submission in ExtensionSubmission.objects.filter(group_id__in=indexed).select_related("bookmark"):
        row = indexed[str(submission.group_id)]
        row["state"] = ("succeeded" if submission.state == "classified" else
            "failed" if submission.state == "needs_attention" else "queued" if submission.state == "queued" else "running")
        row["extension_submission"] = serialize(submission)
