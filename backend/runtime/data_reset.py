"""Confirmed, resumable data erasure owned by the maintenance supervisor."""
import os
import secrets
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from django.db import connections, transaction

from .maintenance import MaintenanceError, _api_error, _complete_operation, _set_stage, _validate_database
from .models import MaintenanceOperation, ProviderSecret, RuntimeIdempotency, RuntimeState

CONFIRMATIONS = {"clear": "清空仓库", "reset": "重置数据"}


def create_data_operation(kind, data):
    if kind not in CONFIRMATIONS or data.get("confirmation") != CONFIRMATIONS[kind]:
        raise _api_error("DATA_CONFIRMATION_REQUIRED", "请输入操作名称以确认删除")
    with transaction.atomic(using="default"):
        state = RuntimeState.objects.using("default").get(pk=1)
        if data.get("library_epoch") != state.library_epoch:
            raise _api_error("DATA_LIBRARY_CHANGED", "收藏库已变化，请刷新页面后重新确认", 409)
        if state.maintenance_mode or MaintenanceOperation.objects.using("default").filter(
                state__in=["uploading", "validating", "queued", "running"]).exists():
            raise _api_error("MAINTENANCE_BUSY", "请先完成当前备份、恢复或数据维护操作", 409)
        operation = MaintenanceOperation.objects.using("default").create(
            kind=kind, old_generation=state.active_generation,
            target_generation=kind + "-" + uuid.uuid4().hex,
            expected_library_epoch=state.library_epoch)
        # Fence writes as soon as the confirmation is accepted, before services stop.
        state.maintenance_mode, state.active_maintenance_id = True, operation.id
        state.save(using="default", update_fields=["maintenance_mode", "active_maintenance_id"])
        return operation


def _prepare_empty_library(operation):
    from config.paths import generation_root
    from library.models import LibrarySettings
    source = generation_root(operation.old_generation) / "library.sqlite3"
    candidate = generation_root(operation.target_generation)
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    (candidate / "artifacts").mkdir(exist_ok=True, mode=0o700)
    database = candidate / "library.sqlite3"
    with closing(sqlite3.connect(source)) as old, closing(sqlite3.connect(database)) as new:
        old.backup(new)
        new.execute("PRAGMA foreign_keys=ON")
        new.execute("PRAGMA secure_delete=ON")
        new.execute("BEGIN IMMEDIATE")
        new.execute("PRAGMA defer_foreign_keys=ON")
        keep = {"library_librarysettings", "library_aiprofile"} if operation.kind == "clear" else set()
        tables = [row[0] for row in new.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0].startswith("library_") and row[0] not in keep]
        for table in tables:
            new.execute('DELETE FROM "' + table.replace('"', '""') + '"')
        if operation.kind == "reset":
            fields = LibrarySettings._meta.local_fields
            columns = ",".join('"' + field.column + '"' for field in fields)
            defaults = [field.get_db_prep_save(field.get_default(), connections["library"]) for field in fields]
            new.execute(f"INSERT INTO library_librarysettings ({columns}) VALUES ({','.join('?' for _ in fields)})", defaults)
        else:
            new.execute("UPDATE library_librarysettings SET revision=revision+1,tree_revision=tree_revision+1")
        if new.execute("PRAGMA foreign_key_check").fetchone():
            raise MaintenanceError("DATA_DATABASE_INVALID")
        new.commit()
        # The copied database must not retain deleted content in unused pages.
        new.execute("VACUUM")
    _validate_database(database, expected_schema=source)


def _remove_managed_path(path):
    from config.paths import DATA_ROOT
    root, path = DATA_ROOT.resolve(), Path(path)
    if path.is_symlink() or path.is_junction():
        raise MaintenanceError("DATA_PATH_UNSAFE")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root or resolved.relative_to(root).parts[0] not in {"generations", "backups", "work"}:
        raise MaintenanceError("DATA_PATH_UNSAFE")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _erase_files(operation):
    from config.paths import DATA_ROOT, generation_root
    generations = DATA_ROOT / "generations"
    if generations.is_symlink() or generations.is_junction() or not generations.resolve().is_relative_to(DATA_ROOT.resolve()):
        raise MaintenanceError("DATA_PATH_UNSAFE")
    for path in generations.iterdir():
        if path != generation_root(operation.target_generation):
            _remove_managed_path(path)
    for name in ("backups", "work"):
        _remove_managed_path(DATA_ROOT / name)


def _erase_runtime_data(operation):
    with transaction.atomic(using="default"):
        RuntimeIdempotency.objects.using("default").exclude(operation_id=operation.id).delete()
        MaintenanceOperation.objects.using("default").exclude(pk=operation.id).delete()
        if operation.kind == "clear":
            # The same trusted AI profiles survive this local clear; no imported endpoints.
            ProviderSecret.objects.using("default").filter(verified=True,
                verified_library_epoch=operation.expected_library_epoch).update(
                    verified_library_epoch=RuntimeState.objects.using("default").get(pk=1).library_epoch)
            return
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        from django.contrib.sessions.models import Session
        Session.objects.using("default").all().delete()
        ProviderSecret.objects.using("default").all().delete()
        RuntimeIdempotency.objects.using("default").all().delete()
        get_user_model().objects.using("default").all().delete()
        Group.objects.using("default").all().delete()
        for model in apps.get_app_config("axes").get_models():
            model.objects.using("default").all().delete()
        RuntimeState.objects.using("default").filter(pk=1).update(worker_heartbeat=None)
    with connections["default"].cursor() as cursor:
        cursor.execute("VACUUM")


def _rotate_signing_key():
    from config.paths import DATA_ROOT
    from django.conf import settings
    directory = DATA_ROOT / "runtime"
    directory.mkdir(exist_ok=True, mode=0o700)
    temporary = directory / ("reset-key-" + uuid.uuid4().hex)
    value = secrets.token_urlsafe(64)
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, directory / "secret-key")
    finally:
        temporary.unlink(missing_ok=True)
    # Explicit deployment configuration remains owned by the host operator.
    if not os.environ.get("DJANGO_SECRET_KEY"):
        settings.SECRET_KEY = value


def process_data_operation(operation, *, stop_services, start_services, ready_services):
    from config.paths import generation_root, prepare_paths
    irreversible = operation.result.get("erasure_started", False)
    switched = RuntimeState.objects.using("default").get(pk=1).active_generation == operation.target_generation
    try:
        stop_services()
        if not switched:
            _set_stage(operation, "preparing_empty_library")
            _prepare_empty_library(operation)
            with transaction.atomic(using="default"):
                state = RuntimeState.objects.using("default").get(pk=1)
                if state.library_epoch != operation.expected_library_epoch:
                    raise MaintenanceError("DATA_LIBRARY_CHANGED")
                state.active_generation, state.library_epoch = operation.target_generation, state.library_epoch + 1
                state.save(using="default", update_fields=["active_generation", "library_epoch"])
                _set_stage(operation, "generation_switched")
            switched = True
        if not irreversible:
            # Verify the empty library can serve requests before permanently erasing the old one.
            start_services()
            if not ready_services():
                raise MaintenanceError("DATA_START_FAILED")
            stop_services()
            _set_stage(operation, "erasing_data", result={"erasure_started": True})
            irreversible = True
        _erase_files(operation)
        _erase_runtime_data(operation)
        if operation.kind == "reset":
            _rotate_signing_key()
        prepare_paths()
        _set_stage(operation, "starting_services")
        start_services()
        if not ready_services():
            raise MaintenanceError("DATA_START_FAILED")
        MaintenanceOperation.objects.using("default").filter(pk=operation.id).update(error_code="")
        _complete_operation(operation, {"requires_initialization": operation.kind == "reset"})
        return True
    except Exception as error:
        code = getattr(error, "code", "DATA_ERASURE_FAILED")
        if irreversible:
            # Never roll back to user data after erasure has begun. Resume on supervisor restart.
            _set_stage(operation, "erasure_interrupted", error_code=code)
            return False
        try:
            stop_services()
            if switched:
                with transaction.atomic(using="default"):
                    state = RuntimeState.objects.using("default").get(pk=1)
                    state.active_generation, state.library_epoch = operation.old_generation, state.library_epoch + 1
                    state.save(using="default", update_fields=["active_generation", "library_epoch"])
            _remove_managed_path(generation_root(operation.target_generation))
            start_services()
            if not ready_services():
                raise MaintenanceError("DATA_ROLLBACK_FAILED")
            with transaction.atomic(using="default"):
                RuntimeState.objects.using("default").filter(pk=1).update(maintenance_mode=False, active_maintenance_id=None)
                MaintenanceOperation.objects.using("default").filter(pk=operation.id).update(
                    state="failed", stage="failed_rolled_back", error_code=code, active_key=None, claim_token=None)
        except Exception:
            _set_stage(operation, "rollback_failed", error_code="DATA_ROLLBACK_FAILED")
        return False
