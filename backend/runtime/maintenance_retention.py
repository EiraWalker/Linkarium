"""Restore staging lifecycle, kept separate from durable user backups."""
import re

LIVE_STATES = ("uploading", "validating", "queued", "running")
RESTORE_GENERATION = re.compile(r"restore-[0-9a-f]{32}\Z")
RESTORE_UPLOAD = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.bookmarklib\.zip\Z")


def cleanup_restore_data(*, startup=False, now=None):
    """Call after interrupted-operation recovery, then periodically.

    startup=True is only for the supervisor before starting its web process:
    an uploading request then has no surviving writer. During normal operation
    live uploads, validation, activation and rollback remain protected.
    """
    from django.db import transaction
    from django.utils import timezone
    from config.paths import DATA_ROOT, generation_root
    from .maintenance import (_reject_links, _clear_candidate, RESTORE_ORPHAN_SECONDS)
    from .models import MaintenanceOperation, RuntimeState
    now = now or timezone.now()
    root = _reject_links(DATA_ROOT)
    removed = {"uploads": 0, "candidates": 0, "expired": 0}

    def remove_upload(operation):
        # Ignore persisted arbitrary paths. Names and containment are derived
        # solely from our operation UUID and configured staging root.
        path = _reject_links(root / "work" / (str(operation.id) + ".bookmarklib.zip"))
        if path.is_file():
            path.unlink()
            removed["uploads"] += 1
        if operation.source_package:
            MaintenanceOperation.objects.using("default").filter(pk=operation.id).update(source_package="")

    with transaction.atomic(using="default"):
        state = RuntimeState.objects.using("default").get(pk=1)
        if startup:
            MaintenanceOperation.objects.using("default").filter(kind="restore", state="uploading",
                active_key__isnull=True, claim_token__isnull=True).update(
                    state="failed", stage="interrupted_upload", error_code="UPLOAD_INTERRUPTED", updated_at=now)
        pending = MaintenanceOperation.objects.using("default").filter(kind="restore", state="awaiting_confirmation")
        for operation in pending.iterator(chunk_size=100):
            if (not operation.validation_expires_at or operation.validation_expires_at <= now or
                    operation.expected_library_epoch != state.library_epoch):
                count = MaintenanceOperation.objects.using("default").filter(pk=operation.id,
                    state="awaiting_confirmation", active_key__isnull=True).update(
                        state="expired", stage="validation_expired", error_code="RESTORE_CONFIRMATION_EXPIRED",
                        validation_token_hash="", updated_at=now)
                removed["expired"] += count

        # Preserve the current library, all operations using generations, and
        # both sides of a failed rollback. Keep the last successful restore's
        # previous generation as the immediately available rollback copy.
        protected = {state.active_generation}
        active = MaintenanceOperation.objects.using("default").filter(state__in=LIVE_STATES)
        for old, target in active.values_list("old_generation", "target_generation").iterator(chunk_size=100):
            protected.update((old, target))
        failed_rollbacks = MaintenanceOperation.objects.using("default").filter(stage="rollback_failed")
        for old, target in failed_rollbacks.values_list("old_generation", "target_generation").iterator(chunk_size=100):
            protected.update((old, target))
        latest = MaintenanceOperation.objects.using("default").filter(kind="restore", state="succeeded").order_by("-created_at").first()
        if latest:
            protected.add(latest.old_generation)
        for operation in MaintenanceOperation.objects.using("default").filter(kind="restore").exclude(
                state__in=LIVE_STATES).iterator(chunk_size=100):
            remove_upload(operation)
            if operation.state == "awaiting_confirmation":
                protected.add(operation.target_generation)
                continue
            if (operation.target_generation in protected or not RESTORE_GENERATION.fullmatch(operation.target_generation)):
                continue
            candidate = _reject_links(generation_root(operation.target_generation))
            if candidate.is_dir():
                _clear_candidate(candidate, remove_root=True)
                removed["candidates"] += 1

        # A crash can leave bytes before the operation metadata is committed.
        # Sweep only our exact naming patterns after a grace period, and never
        # touch active operations or unrelated work/backup files.
        upload_root = _reject_links(root / "work")
        if upload_root.is_dir():
            for path in upload_root.iterdir():
                if not RESTORE_UPLOAD.fullmatch(path.name):
                    continue
                _reject_links(path)
                operation_id = path.name.removesuffix(".bookmarklib.zip")
                if MaintenanceOperation.objects.using("default").filter(pk=operation_id).exists():
                    continue
                if path.is_file() and now.timestamp() - path.stat().st_mtime >= RESTORE_ORPHAN_SECONDS:
                    path.unlink()
                    removed["uploads"] += 1
        generations = _reject_links(root / "generations")
        if generations.is_dir():
            for path in generations.iterdir():
                if not RESTORE_GENERATION.fullmatch(path.name) or path.name in protected:
                    continue
                _reject_links(path)
                if (path.is_dir() and now.timestamp() - path.stat().st_mtime >= RESTORE_ORPHAN_SECONDS and
                        not MaintenanceOperation.objects.using("default").filter(target_generation=path.name,
                            state__in=(*LIVE_STATES, "awaiting_confirmation")).exists()):
                    _clear_candidate(path, remove_root=True)
                    removed["candidates"] += 1
    return removed


def cancel_restore(operation_id):
    """Cancel a preview or an unclaimed restore; activation cannot be interrupted."""
    from django.db import transaction
    from django.utils import timezone
    from .maintenance import _api_error
    from .models import MaintenanceOperation
    with transaction.atomic(using="default"):
        operation = MaintenanceOperation.objects.using("default").filter(pk=operation_id, kind="restore").first()
        if operation is None:
            raise _api_error("NOT_FOUND", "恢复操作不存在", 404)
        if operation.state not in {"cancelled", "expired", "failed", "succeeded"}:
            if operation.state in {"uploading", "running"} or operation.active_key or operation.claim_token:
                raise _api_error("MAINTENANCE_BUSY", "恢复正在执行，请等待操作完成", 409)
            MaintenanceOperation.objects.using("default").filter(pk=operation.id).update(
                state="cancelled", stage="cancelled", validation_token_hash="", updated_at=timezone.now())
    cleanup_restore_data()
    operation.refresh_from_db()
    return operation
