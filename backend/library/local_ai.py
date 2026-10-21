"""Device-owned execution of cloud-prepared classification requests."""
from contextvars import ContextVar
import hashlib
import hmac
import uuid
import json
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from runtime.models import ExecutionDevice
from .errors import ApiError
from .ai_transport import AIError

current_device = ContextVar("execution_device", default=None)
PROTOCOL = 1


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def serialize_device(device):
    return {"id": str(device.id), "name": device.name, "protocol": PROTOCOL,
            "last_seen_at": device.last_seen_at.isoformat()}


def register(user, data):
    identity = uuid.UUID(str(data.get("id")))
    proof = data.get("proof")
    name = data.get("name", "Firefox")
    if not isinstance(proof, str) or not 32 <= len(proof) <= 200 or not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise ApiError("INVALID_DEVICE", "设备身份格式无效")
    device, created = ExecutionDevice.objects.get_or_create(pk=identity,
        defaults={"user": user, "proof_digest": digest(proof), "name": name})
    if device.user_id != user.id or not hmac.compare_digest(device.proof_digest, digest(proof)):
        raise ApiError("DEVICE_FORBIDDEN", "设备身份不匹配", 403)
    device.name = name
    device.save(update_fields=["name", "last_seen_at"])
    return serialize_device(device)


def authenticate_device(request, required=False):
    identity = request.headers.get("X-Linkarium-Device")
    proof = request.headers.get("X-Linkarium-Device-Proof", "")
    if not identity:
        if required:
            raise ApiError("LOCAL_DEVICE_REQUIRED", "请在 Firefox 中启用本机 AI 执行端", 409)
        return None
    try:
        device = ExecutionDevice.objects.filter(pk=uuid.UUID(identity), user=request.user).first()
    except ValueError:
        device = None
    if not device or not hmac.compare_digest(device.proof_digest, digest(proof)):
        raise ApiError("DEVICE_FORBIDDEN", "设备身份不匹配", 403)
    return device


def require_device():
    device = current_device.get()
    if device is None:
        raise ApiError("LOCAL_DEVICE_REQUIRED", "请使用已绑定的 Firefox 本机执行端发起分类", 409)
    return str(device.id)


def require_owner(job):
    identity = require_device()
    if not job.execution_device_id or str(job.execution_device_id) != identity:
        raise ApiError("DEVICE_FORBIDDEN", "任务只能由发起设备恢复或执行", 403)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_context(request):
    from . import classification, queue, submissions
    from .models import ClassificationTaxonomy
    job = request.job
    state = queue.runtime_state()
    if state.maintenance_mode or job.generation != state.active_generation or job.library_epoch != state.library_epoch:
        raise ApiError("ATTEMPT_EXPIRED", "收藏库已切换，执行授权失效", 409)
    classification.validate_request_context(request.context)
    if request.stage == "classification":
        taxonomy = ClassificationTaxonomy.objects.filter(pk=1).first()
        if not taxonomy or taxonomy.categories != request.context["categories"]:
            raise ApiError("PROPOSAL_STALE", "共享分类体系已变化", 409)
    if job.payload.get("submission_id"):
        submission = submissions.get(job.payload["submission_id"])
        if (not submissions._current(submission) or submission.state != "classifying"
                or submission.classification_job_id != job.id
                or submission.expected_classification_revision != submission.bookmark.classification_revision
                or submission.expected_tree_revision != request.context["tree_revision"]):
            raise ApiError("SUBMISSION_STALE", "收藏已变化，请在网页处理分类", 409)


def prepare(job):
    from . import classification, taxonomy
    from .models import LocalAIRequest, ClassificationTaxonomy, Bookmark, LibrarySettings
    request = job.local_requests.filter(completed_at__isnull=True).first()
    if request:
        validate_context(request)
        return request
    from .models import Job
    from django.db.models import Q
    saved = ClassificationTaxonomy.objects.filter(pk=1).first()
    material_ids = job.payload["bookmark_ids"] if saved else Bookmark.objects.values("pk")
    if Job.objects.filter(Q(kind="metadata", state__in=("queued", "running", "retry_wait")) | Q(kind="capture", state="running"),
            target_id__in=material_ids, generation=job.generation, library_epoch=job.library_epoch).exists():
        job.execution_state = "waiting_materials"
        return None
    stage = "classification" if saved else "taxonomy"
    # A planner belongs to its original device. Other jobs wait for its checkpoint.
    if not saved and LocalAIRequest.objects.filter(stage="taxonomy", completed_at__isnull=True,
            job__state__in=("queued", "running", "retry_wait")).exclude(job=job).exists():
        return None
    if saved:
        messages, context = classification.prepare_request(job.payload["bookmark_ids"], saved.categories)
        schema = classification.RESULT_SCHEMA
    else:
        messages, context = taxonomy.prepare_request()
        context.update(revisions={str(b.id): b.classification_revision for b in Bookmark.objects.all()},
            tree_revision=LibrarySettings.objects.get(pk=1).tree_revision)
        schema = taxonomy.SCHEMA
    snapshot = {"protocol": PROTOCOL, "job_id": str(job.id), "stage": stage,
        "generation": job.generation, "library_epoch": job.library_epoch,
        "messages": messages, "schema": schema, "context": context}
    if job.payload.get("import_id"):
        from .importing import reserve_ai_request
        reserve_ai_request(job.payload["import_id"])
    return LocalAIRequest.objects.create(job=job, stage=stage, messages=messages, schema=schema,
        context=context, prompt_digest=digest(canonical(snapshot)))


def protected_bookmarks():
    """Do not publish newer capture metadata over a device's running input snapshot."""
    from .models import LocalAIRequest
    protected = set()
    for context in LocalAIRequest.objects.filter(completed_at__isnull=True, job__state="running").values_list("context", flat=True):
        protected.update(context["revisions"])
    return protected


def expire(job):
    from .models import JobAttempt
    if job.state != "running":
        return
    attempt = JobAttempt.objects.filter(pk=job.active_attempt_id).first()
    if attempt and attempt.lease_expires_at > timezone.now():
        return
    if attempt:
        JobAttempt.objects.filter(pk=attempt.id, state="running").update(state="paused", finished_at=timezone.now())
    job.state, job.execution_state, job.error_code = "queued", "paused", "LOCAL_INTERRUPTED"
    job.active_attempt_id = None
    job.save(update_fields=["state", "execution_state", "error_code", "active_attempt_id", "updated_at"])


@transaction.atomic(using="library")
def claim(data):
    from . import queue
    from .models import Job, JobAttempt
    identity = require_device()
    if queue.runtime_state().maintenance_mode:
        raise ApiError("MAINTENANCE", "收藏库正在维护", 503)
    resume = data.get("resume_request_id")
    if resume:
        from .models import LocalAIRequest
        request = LocalAIRequest.objects.select_related("job").filter(pk=resume).first()
        if not request:
            raise ApiError("NOT_FOUND", "执行记录不存在", 404)
        job = request.job
        require_owner(job)
        state = queue.runtime_state()
        if job.generation != state.active_generation or job.library_epoch != state.library_epoch:
            raise ApiError("ATTEMPT_EXPIRED", "收藏库已切换", 409)
        if request.completed_at:
            return {"completed": True, "receipt": request.receipt}
        if job.state not in queue.ACTIVE:
            raise ApiError("RESULT_INVALIDATED", "任务已结束或被替代", 409)
        if job.state == "running":
            JobAttempt.objects.filter(pk=job.active_attempt_id, state="running").update(state="paused", finished_at=timezone.now())
            job.state, job.active_attempt_id = "queued", None
            job.save(update_fields=["state", "active_attempt_id"])
    else:
        jobs = Job.objects.filter(kind="classification", execution_device_id=identity, state__in=queue.ACTIVE)
        job = jobs.filter(local_requests__stage="taxonomy", local_requests__completed_at__isnull=True).first() or jobs.order_by("-priority", "created_at").first()
        if not job:
            from .models import ImportBatch, ExtensionSubmission
            if (ImportBatch.objects.filter(execution_device_id=identity, state__in=("queued", "running")).exists()
                    or ExtensionSubmission.objects.filter(execution_device_id=identity, state__in=("queued", "enriching")).exists()):
                return {"waiting": True, "state": "waiting_materials"}
            return None
    expire(job)
    if job.execution_state == "storage_blocked" and not data.get("manual_resume"):
        return {"paused": True, "error_code": "LOCAL_STORAGE_UNAVAILABLE"}
    try:
        request = prepare(job)
        if request:
            validate_context(request)
        if resume and not data.get("has_response") and job.payload.get("import_id"):
            from .importing import reserve_ai_request
            reserve_ai_request(job.payload["import_id"])
    except (ApiError, AIError) as error:
        code = getattr(error, "code", "TASK_FAILED")
        # Persist invalidation outside an error response's rollback.
        job.state, job.execution_state, job.error_code, job.active_key = "failed", "invalidated", code, None
        job.save()
        return {"invalidated": True, "error_code": code, "job_id": str(job.id)}
    if request is None:
        if job.execution_state != "waiting_materials":
            job.execution_state = "waiting_taxonomy"
        job.save(update_fields=["execution_state"])
        return {"waiting": True, "state": job.execution_state}
    if job.state == "running":
        attempt = JobAttempt.objects.get(pk=job.active_attempt_id)
    else:
        now = timezone.now()
        attempt = JobAttempt.objects.create(job=job, attempt_no=job.attempt_count + 1,
            lease_expires_at=now + timedelta(seconds=180), heartbeat_at=now,
            generation=job.generation, library_epoch=job.library_epoch, input_revision=job.input_revision)
        job.state, job.active_attempt_id = "running", attempt.id
        job.attempt_count += 1
    job.execution_state, job.error_code = "running", ""
    job.save()
    return {"protocol": PROTOCOL, "request_id": str(request.id), "job_id": str(job.id),
        "attempt_id": str(attempt.id), "lease_token": str(attempt.lease_token),
        "prompt_digest": request.prompt_digest, "generation": job.generation, "library_epoch": job.library_epoch,
        "stage": request.stage, "messages": request.messages, "schema": request.schema,
        "timeout_seconds": 240 if request.stage == "taxonomy" else 120}


def authorized(data):
    from . import queue
    from .models import LocalAIRequest, JobAttempt
    request = LocalAIRequest.objects.select_related("job").filter(pk=data.get("request_id")).first()
    if not request:
        raise ApiError("NOT_FOUND", "执行记录不存在", 404)
    job = request.job
    require_owner(job)
    if data.get("prompt_digest") != request.prompt_digest:
        raise ApiError("PROMPT_CONFLICT", "提示词版本不匹配", 409)
    state = queue.runtime_state()
    if state.maintenance_mode or job.generation != state.active_generation or job.library_epoch != state.library_epoch:
        raise ApiError("ATTEMPT_EXPIRED", "收藏库已切换", 409)
    # An acknowledged checkpoint remains queryable without resurrecting its lease.
    if request.completed_at:
        return request, None
    attempt = JobAttempt.objects.filter(pk=data.get("attempt_id"), job=job,
        lease_token=data.get("lease_token")).first()
    if not attempt or not queue.execution_valid(job, attempt):
        raise ApiError("ATTEMPT_EXPIRED", "租约失效，请由发起设备恢复", 409)
    validate_context(request)
    return request, attempt


@transaction.atomic(using="library")
def heartbeat(data):
    request, attempt = authorized(data)
    if attempt:
        attempt.heartbeat_at = timezone.now()
        attempt.lease_expires_at = timezone.now() + timedelta(seconds=180)
        attempt.save(update_fields=["heartbeat_at", "lease_expires_at"])
        from .models import Job
        phase = data.get("state", "running")
        Job.objects.filter(pk=request.job_id).update(execution_state="waiting_upload" if phase == "waiting_upload" else "running")
    return {"completed": bool(request.completed_at)}


@transaction.atomic(using="library")
def pause(data):
    request, attempt = authorized(data)
    if attempt:
        from .models import Job
        attempt.state, attempt.finished_at = "paused", timezone.now()
        attempt.save(update_fields=["state", "finished_at"])
        allowed = {"LOCAL_API_UNAVAILABLE", "LOCAL_CONFIG_REQUIRED", "LOCAL_INTERRUPTED", "LOCAL_API_INVALID", "LOCAL_AUTH_REQUIRED", "LOCAL_STORAGE_UNAVAILABLE"}
        code = data.get("error_code")
        Job.objects.filter(pk=request.job_id).update(state="queued", execution_state="storage_blocked" if code == "LOCAL_STORAGE_UNAVAILABLE" else "paused",
            active_attempt_id=None, error_code=code if code in allowed else "LOCAL_INTERRUPTED")
    return {"paused": True}


@transaction.atomic(using="library")
def submit_result(data):
    from . import classification, queue, taxonomy, submissions
    from .ai_transport import strict_json
    from .models import ClassificationTaxonomy, ClassificationProposal, JobAttempt, Job
    request, attempt = authorized(data)
    raw = data.get("response")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 512 * 1024:
        raise ApiError("AI_INVALID_RESULT", "模型结果过大或格式无效")
    result_digest = digest(raw)
    if request.completed_at:
        if request.result_digest != result_digest:
            raise ApiError("RESULT_CONFLICT", "同一请求不能提交不同结果", 409)
        return request.receipt
    response = strict_json(raw)
    job = request.job
    if request.stage == "taxonomy":
        categories = taxonomy.validate_plan(response, request.context["site_count"])
        saved, created = ClassificationTaxonomy.objects.get_or_create(pk=1, defaults={"categories": categories})
        if not created and saved.categories != categories:
            raise ApiError("PROPOSAL_STALE", "共享分类体系已变化", 409)
        JobAttempt.objects.filter(pk=attempt.id).update(state="succeeded", finished_at=timezone.now())
        Job.objects.filter(pk=job.id).update(state="queued", execution_state="waiting_device", active_attempt_id=None)
        receipt = {"request_id": str(request.id), "stage": request.stage, "completed": True}
    else:
        result = classification.commit_result(response, request.context, job.payload.get("group_id"))
        auto = bool(job.payload.get("submission_id"))
        if auto:
            submission = submissions.get(job.payload["submission_id"])
            proposal = ClassificationProposal.objects.get(pk=result["proposal_ids"][0])
            classification.accept_proposals([{"id": str(proposal.id), "revision": proposal.revision}], proposal.expected_tree_revision)
            submission.proposal, submission.state, submission.error_code = proposal, "classified", ""
            submission.applied_at, submission.revision = timezone.now(), submission.revision + 1
            submission.save()
        receipt = {"request_id": str(request.id), "stage": request.stage, "completed": True,
            "proposal_count": result["count"], "auto_applied": auto}
        queue.finish(job, attempt, receipt)
        job.refresh_from_db()
        if job.state != "succeeded":
            raise ApiError("ATTEMPT_EXPIRED", "执行授权失效", 409)
        Job.objects.filter(pk=job.id).update(execution_state="auto_applied" if auto else "review_ready")
    receipt["result_digest"] = result_digest
    request.result_digest, request.receipt, request.completed_at = result_digest, receipt, timezone.now()
    request.save(update_fields=["result_digest", "receipt", "completed_at"])
    return receipt
