"""ZIP64 portability and state-owned maintenance. No runtime credentials enter a package."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone as utc_timezone
from pathlib import Path
from contextlib import contextmanager, closing
from capture.models import MAX_CAPTURE_BYTES
from runtime import performance

FORMAT = "bookmarklib"
FORMAT_VERSION = 1
SCHEMA_VERSION = 1
MAX_FILES = 100000
MAX_EXPANDED_BYTES = 64 * 1024**3
MAX_DATABASE_BYTES = 1024**3
MAX_ARTIFACT_BYTES = MAX_CAPTURE_BYTES
MAX_MANIFEST_BYTES = 16 * 1024**2
# A full million-bookmark library remains supported. File-backed records share
# the archive's existing member budget; orphan rows receive their own budget.
MAX_FOLDER_ROWS = 100000
MAX_BOOKMARK_ROWS = 1000000
MAX_SNAPSHOT_ROWS = 1000000
MAX_ORPHAN_ARTIFACT_ROWS = MAX_FILES - 2
MAX_DATABASE_ROWS = 10000000
MAX_SCHEMA_OBJECTS = 512
RESTORE_CONFIRMATION_SECONDS = 3600
RESTORE_ORPHAN_SECONDS = 3600
ARTIFACT_NAME = re.compile(r"artifacts/([0-9a-f]{2})/([0-9a-f]{64})\.html\Z")


class MaintenanceError(ValueError):
    def __init__(self, code, message="Backup or restore could not be completed"):
        self.code = code
        super().__init__(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _file_name(name):
    if name in {"manifest.json", "library.sqlite3"}:
        return name
    match = ARTIFACT_NAME.fullmatch(name)
    if match and match[1] == match[2][:2]:
        return name
    raise MaintenanceError("BACKUP_UNSAFE_PATH", "Backup contains an unsupported path")


def _safe_file(root, name):
    _file_name(name)
    root = Path(root).resolve()
    result = root / name
    if result.is_symlink() or not result.resolve().is_relative_to(root):
        raise MaintenanceError("BACKUP_UNSAFE_PATH")
    current = result.parent
    while current != root:
        if current.is_symlink():
            raise MaintenanceError("BACKUP_UNSAFE_PATH")
        current = current.parent
    return result


@contextmanager
def _connect_readonly(path, immutable=False):
    uri = Path(path).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.enable_load_extension(False)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA cache_size=-8192")
    try:
        yield connection
    finally:
        connection.close()


def schema_signature(path):
    with _connect_readonly(path) as connection:
        return {(kind, name, table): " ".join(sql.split()) if sql else None
                for kind, name, table, sql in connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def _validate_database(path, *, expected_schema=None):
    with _connect_readonly(path, immutable=True) as connection:
        schema = {}
        for kind, name, table, sql in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"):
            if len(schema) >= MAX_SCHEMA_OBJECTS or (sql and len(sql) > 1024 * 1024):
                raise MaintenanceError("BACKUP_SCHEMA_UNSAFE")
            schema[kind, name, table] = " ".join(sql.split()) if sql else None
        for (kind, name, table), sql in schema.items():
            if kind not in {"table", "index"} or not (table.startswith("library_") or table == "django_migrations"):
                raise MaintenanceError("BACKUP_SCHEMA_UNSAFE", "Backup contains unsupported database objects")
            if sql and ("VIRTUAL TABLE" in sql.upper() or "CREATE TRIGGER" in sql.upper()):
                raise MaintenanceError("BACKUP_SCHEMA_UNSAFE")
        if expected_schema is not None:
            trusted = schema_signature(expected_schema) if isinstance(expected_schema, (str, Path)) else expected_schema
            if schema != trusted:
                raise MaintenanceError("BACKUP_SCHEMA_INCOMPATIBLE", "Backup schema does not match this application version")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise MaintenanceError("BACKUP_DATABASE_CORRUPT")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MaintenanceError("BACKUP_FOREIGN_KEY_INVALID")
        tables = {name for kind, name, _ in schema if kind == "table"}
        required = {"library_bookmark", "library_folder", "library_artifact", "library_snapshotversion", "library_capturestate"}
        if not required.issubset(tables):
            raise MaintenanceError("BACKUP_SCHEMA_INCOMPATIBLE")
        row_counts, total_rows = {}, 0
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            count = connection.execute("SELECT count(*) FROM " + quoted).fetchone()[0]
            row_counts[table] = count
            total_rows += count
            limit = {"library_folder": MAX_FOLDER_ROWS, "library_bookmark": MAX_BOOKMARK_ROWS,
                     "library_snapshotversion": MAX_SNAPSHOT_ROWS,
                     "library_artifact": MAX_FILES - 2 + MAX_ORPHAN_ARTIFACT_ROWS}.get(table, MAX_DATABASE_ROWS)
            if count > limit or total_rows > MAX_DATABASE_ROWS:
                raise MaintenanceError("BACKUP_ROW_LIMIT", "Backup exceeds the supported library record budget")
        # Only this bounded graph is retained. Mark proven paths once so a wide
        # or deeply shared folder tree takes O(rows * maximum depth), not O(rows²).
        folders = dict(connection.execute("SELECT id,parent_id FROM library_folder"))
        depths = {}
        for folder_id in folders:
            current, seen, chain = folder_id, set(), []
            while current is not None and current not in depths:
                if current in seen or current not in folders or len(chain) >= 32:
                    raise MaintenanceError("BACKUP_FOLDER_INVALID")
                seen.add(current)
                chain.append(current)
                current = folders[current]
            depth = depths.get(current, 0)
            for item in reversed(chain):
                depth += 1
                if depth > 32:
                    raise MaintenanceError("BACKUP_FOLDER_INVALID")
                depths[item] = depth
        if connection.execute("SELECT canonical_url FROM library_bookmark GROUP BY canonical_url HAVING count(*)>1").fetchone():
            raise MaintenanceError("BACKUP_URL_DUPLICATE")
        bad_pointer = connection.execute("""SELECT c.id FROM library_capturestate c
            JOIN library_snapshotversion s ON s.id=c.current_version_id
            WHERE c.bookmark_id<>s.bookmark_id OR c.variant<>s.variant LIMIT 1""").fetchone()
        if bad_pointer:
            raise MaintenanceError("BACKUP_CURRENT_POINTER_INVALID")
        if connection.execute("SELECT id FROM library_snapshotversion WHERE variant NOT IN ('desktop','mobile') LIMIT 1").fetchone():
            raise MaintenanceError("BACKUP_VARIANT_INVALID")
        # Orphans remain in a backed-up database even though their files are omitted.
        # Validate every row: a malicious orphan path must never reach file cleanup.
        for artifact_hash, relative_path, size in connection.execute("SELECT sha256,relative_path,byte_size FROM library_artifact"):
            if (not isinstance(artifact_hash, str) or re.fullmatch(r"[0-9a-f]{64}", artifact_hash) is None or
                    relative_path != f"artifacts/{artifact_hash[:2]}/{artifact_hash}.html" or
                    not isinstance(size, int) or size < 0 or size > MAX_ARTIFACT_BYTES):
                raise MaintenanceError("BACKUP_ARTIFACT_INVALID")
        artifacts = list(connection.execute("""SELECT DISTINCT a.sha256,a.relative_path,a.byte_size
            FROM library_artifact a JOIN library_snapshotversion s ON s.artifact_id=a.sha256"""))
        if len(artifacts) > MAX_FILES - 2 or row_counts["library_artifact"] - len(artifacts) > MAX_ORPHAN_ARTIFACT_ROWS:
            raise MaintenanceError("BACKUP_ROW_LIMIT")
        counts = {"bookmarks": row_counts["library_bookmark"],
                  "folders": len(folders), "artifacts": len(artifacts),
                  "snapshots": row_counts["library_snapshotversion"],
                  "desktop_snapshots": connection.execute("SELECT count(*) FROM library_snapshotversion WHERE variant='desktop'").fetchone()[0],
                  "mobile_snapshots": connection.execute("SELECT count(*) FROM library_snapshotversion WHERE variant='mobile'").fetchone()[0]}
    return artifacts, counts


def create_backup(generation_dir, output_path, *, expected_schema=None):
    """Call only after the supervisor confirms publishers/cleaners have stopped."""
    with performance.span("maintenance.create_backup", category="maintenance"):
        generation_dir, output_path = Path(generation_dir), Path(output_path)
        if not (generation_dir / "library.sqlite3").is_file():
            raise MaintenanceError("LIBRARY_NOT_FOUND")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            raise MaintenanceError("BACKUP_EXISTS")
        with tempfile.TemporaryDirectory(prefix="backup-", dir=output_path.parent) as temp:
            staging = Path(temp)
            database = staging / "library.sqlite3"
            with performance.span("backup.database_copy", category="maintenance"):
                with closing(sqlite3.connect(generation_dir / "library.sqlite3")) as source, closing(sqlite3.connect(database)) as target:
                    source.backup(target)
            with performance.span("backup.database_validate", category="maintenance"):
                artifacts, counts = _validate_database(database, expected_schema=expected_schema or generation_dir / "library.sqlite3")
            needed = database.stat().st_size + sum(size for _, _, size in artifacts)
            # Reserve worst-case ZIP bytes now. Validation checks actual remaining
            # extraction space after compression, without assuming an uncompressed ZIP.
            if shutil.disk_usage(output_path.parent).free < needed + 16 * 1024**2:
                raise MaintenanceError("INSUFFICIENT_DISK_SPACE")
            files = {"library.sqlite3": {"size": database.stat().st_size, "sha256": digest(database)}}
            archive_sources = {"library.sqlite3": database}
            # Publishers and cleaners remain stopped until the archive is verified.
            # Read immutable artifacts in place; only SQLite needs its own snapshot.
            with performance.span("backup.artifact_verify", category="maintenance"):
                for artifact_hash, name, size in artifacts:
                    source = _safe_file(generation_dir, name)
                    if not source.is_file() or source.stat().st_size != size or digest(source) != artifact_hash:
                        raise MaintenanceError("BACKUP_ARTIFACT_CORRUPT")
                    archive_sources[name] = source
                    files[name] = {"size": size, "sha256": artifact_hash}
            manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "schema_version": SCHEMA_VERSION,
                        "created_at": datetime.now(utc_timezone.utc).isoformat(),
                        "source_generation": generation_dir.name, "files": files, "counts": counts}
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            partial = output_path.with_name(output_path.name + ".partial")
            try:
                with performance.span("backup.compress", category="maintenance"):
                    with zipfile.ZipFile(partial, "x", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as archive:
                        archive.write(staging / "manifest.json", "manifest.json")
                        for name in files:
                            archive.write(archive_sources[name], name)
                # Verify the final compressed bytes, not merely the pre-compression staging.
                with performance.span("backup.verify", category="maintenance"):
                    with tempfile.TemporaryDirectory(prefix="verify-", dir=output_path.parent) as verification:
                        validate_package(partial, Path(verification), expected_schema=expected_schema or generation_dir / "library.sqlite3")
                os.replace(partial, output_path)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        return {"manifest": manifest, "byte_size": output_path.stat().st_size, "sha256": digest(output_path)}


def _reject_links(path):
    path = Path(path).absolute()
    for current in (path, *path.parents):
        if current.is_symlink() or current.is_junction():
            raise MaintenanceError("BACKUP_UNSAFE_PATH")
    return path


def _clear_candidate(candidate_dir, *, remove_root=False):
    """Remove only the already-owned candidate; never traverse a replacement link."""
    candidate_dir = _reject_links(candidate_dir)
    if not candidate_dir.exists():
        return
    for item in candidate_dir.iterdir():
        if item.is_symlink() or item.is_junction() or not item.is_dir():
            item.unlink(missing_ok=True) if not item.is_junction() else item.rmdir()
        else:
            shutil.rmtree(item)
    if remove_root:
        candidate_dir.rmdir()


def _isolated_validation(action, *, expected_schema=None, **arguments):
    """The supervisor receives only a bounded status, never SQLite rows."""
    from config.paths import DATA_ROOT, PROJECT_ROOT
    from .maintenance_worker import WALL_SECONDS
    request = {"action": action, **arguments}
    if expected_schema is not None:
        request["expected_schema"] = (str(Path(expected_schema).absolute()) if isinstance(expected_schema, (str, Path))
                                       else [[key, value] for key, value in expected_schema.items()])
    environment = dict(os.environ, BOOKMARK_DATA_ROOT=str(DATA_ROOT), PYTHONPATH=str(PROJECT_ROOT / "backend"))
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "runtime.maintenance_worker"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             cwd=PROJECT_ROOT, env=environment, **options)
    try:
        output, _ = child.communicate(json.dumps(request).encode(), timeout=WALL_SECONDS)
    except BaseException as error:
        if os.name != "nt":
            import signal
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            child.kill()
        child.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise MaintenanceError("RESTORE_RESOURCE_LIMIT", "Restore validation exceeded its time budget") from None
        raise
    try:
        result = json.loads(output) if len(output) <= 4096 else {}
    except (ValueError, UnicodeError):
        result = {}
    if child.returncode or not isinstance(result, dict) or result.get("ok") is not True:
        code = result.get("error", "RESTORE_RESOURCE_LIMIT") if isinstance(result, dict) else "RESTORE_RESOURCE_LIMIT"
        raise MaintenanceError(code, "Restore validation failed or exceeded its resource budget")
    return result.get("manifest")


def _manifest_summary(manifest):
    # File inventories stay in the package/candidate, not in every runtime row
    # or HTTP polling response for an operation that has already finished.
    return {key: value for key, value in manifest.items() if key != "files"}


def _candidate_manifest(candidate_dir):
    path = _safe_file(candidate_dir, "manifest.json")
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise MaintenanceError("BACKUP_MANIFEST_INVALID")
    try:
        from library.ai_transport import strict_json, AIError
        return strict_json(path.read_text(encoding="utf-8"))
    except (UnicodeError, AIError):
        raise MaintenanceError("BACKUP_MANIFEST_INVALID") from None


def validate_package(package_path, candidate_dir, *, expected_schema=None, expanded_limit=MAX_EXPANDED_BYTES,
                     expected_sha256=None, summary=False):
    """Validate untrusted bytes under OS memory/CPU limits and a wall deadline."""
    candidate_dir = _reject_links(candidate_dir)
    if candidate_dir.exists() and any(candidate_dir.iterdir()):
        raise MaintenanceError("RESTORE_CANDIDATE_NOT_EMPTY")
    candidate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        manifest = _isolated_validation("package", package=str(Path(package_path).absolute()), candidate=str(candidate_dir),
                                        expected_schema=expected_schema, expanded_limit=expanded_limit, expected_sha256=expected_sha256)
        return manifest if summary else _candidate_manifest(candidate_dir)
    except BaseException:
        _clear_candidate(candidate_dir)
        raise


def validate_database_isolated(database, *, expected_schema=None):
    _isolated_validation("database", database=str(Path(database).absolute()), expected_schema=expected_schema)


def validate_candidate(candidate_dir, *, expected_schema=None, prepare=False, summary=False):
    """Recheck preview bytes and optionally cancel restored work in the child."""
    candidate_dir = _reject_links(candidate_dir)
    manifest = _isolated_validation("candidate", candidate=str(candidate_dir), expected_schema=expected_schema, prepare=prepare)
    return manifest if summary else _candidate_manifest(candidate_dir)


def _validate_candidate(candidate_dir, *, expected_schema=None, prepare=False):
    candidate_dir = _reject_links(candidate_dir)
    manifest = _candidate_manifest(candidate_dir)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict) or len(manifest["files"]) >= MAX_FILES:
        raise MaintenanceError("BACKUP_MANIFEST_INVALID")
    for name, info in manifest["files"].items():
        candidate_file = _safe_file(candidate_dir, name)
        if candidate_file.stat().st_size != info["size"] or digest(candidate_file) != info["sha256"]:
            raise MaintenanceError("RESTORE_CANDIDATE_CHANGED")
    artifacts, counts = _validate_database(candidate_dir / "library.sqlite3", expected_schema=expected_schema)
    if (counts != manifest["counts"] or set(manifest["files"]) != {"library.sqlite3"} | {name for _, name, _ in artifacts}):
        raise MaintenanceError("BACKUP_MANIFEST_INVALID")
    for artifact_hash, name, size in artifacts:
        if manifest["files"][name] != {"sha256": artifact_hash, "size": size}:
            raise MaintenanceError("BACKUP_ARTIFACT_INVALID")
    if prepare:
        _cancel_candidate_work(candidate_dir / "library.sqlite3")
    return manifest


def _validate_package(package_path, candidate_dir, *, expected_schema=None, expanded_limit=MAX_EXPANDED_BYTES):
    with performance.span("maintenance.validate_package", category="maintenance"):
        candidate_dir = Path(candidate_dir)
        if candidate_dir.is_symlink() or (candidate_dir.exists() and any(candidate_dir.iterdir())):
            raise MaintenanceError("RESTORE_CANDIDATE_NOT_EMPTY")
        candidate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with zipfile.ZipFile(package_path) as archive:
                infos, names, expanded = archive.infolist(), set(), 0
                if len(infos) > MAX_FILES:
                    raise MaintenanceError("BACKUP_TOO_MANY_FILES")
                for info in infos:
                    _file_name(info.filename)
                    if info.filename in names:
                        raise MaintenanceError("BACKUP_DUPLICATE_PATH")
                    names.add(info.filename)
                    file_type = stat.S_IFMT(info.external_attr >> 16)
                    if file_type not in {0, stat.S_IFREG} or info.is_dir() or info.flag_bits & 1:
                        raise MaintenanceError("BACKUP_UNSAFE_ENTRY")
                    limit = MAX_MANIFEST_BYTES if info.filename == "manifest.json" else (
                        MAX_DATABASE_BYTES if info.filename == "library.sqlite3" else MAX_ARTIFACT_BYTES)
                    if info.file_size > limit:
                        raise MaintenanceError("BACKUP_ENTRY_TOO_LARGE")
                    expanded += info.file_size
                if expanded > expanded_limit:
                    raise MaintenanceError("BACKUP_EXPANSION_LIMIT")
                if not {"manifest.json", "library.sqlite3"}.issubset(names):
                    raise MaintenanceError("BACKUP_MANIFEST_INVALID")
                if shutil.disk_usage(candidate_dir).free < expanded + 16 * 1024**2:
                    raise MaintenanceError("INSUFFICIENT_DISK_SPACE")
                from library.ai_transport import strict_json, AIError
                try:
                    manifest = strict_json(archive.read("manifest.json").decode("utf-8"))
                except (UnicodeError, AIError):
                    raise MaintenanceError("BACKUP_MANIFEST_INVALID") from None
                required = {"format", "format_version", "schema_version", "created_at", "source_generation", "files", "counts"}
                if not isinstance(manifest, dict) or set(manifest) != required or manifest["format"] != FORMAT or (
                        manifest["format_version"] != FORMAT_VERSION or manifest["schema_version"] != SCHEMA_VERSION):
                    raise MaintenanceError("BACKUP_VERSION_UNSUPPORTED")
                if (not isinstance(manifest["created_at"], str) or len(manifest["created_at"]) > 80 or
                        not isinstance(manifest["source_generation"], str) or
                        re.fullmatch(r"[A-Za-z0-9_-]{1,80}", manifest["source_generation"]) is None or
                        not isinstance(manifest["counts"], dict) or
                        set(manifest["counts"]) != {"bookmarks", "folders", "artifacts", "snapshots", "desktop_snapshots", "mobile_snapshots"} or
                        any(type(count) is not int or count < 0 or count > MAX_DATABASE_ROWS for count in manifest["counts"].values())):
                    raise MaintenanceError("BACKUP_MANIFEST_INVALID")
                files = manifest["files"]
                if not isinstance(files, dict) or set(files) != names - {"manifest.json"}:
                    raise MaintenanceError("BACKUP_MANIFEST_INVALID")
                with performance.span("restore.extract_verify", category="maintenance"):
                    for name, item in files.items():
                        if not isinstance(item, dict) or set(item) != {"size", "sha256"} or not isinstance(item["size"], int) or (
                                isinstance(item["size"], bool) or item["size"] < 0 or not isinstance(item["sha256"], str) or
                                re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
                            raise MaintenanceError("BACKUP_MANIFEST_INVALID")
                        info = archive.getinfo(name)
                        if item["size"] != info.file_size:
                            raise MaintenanceError("BACKUP_HASH_MISMATCH")
                        target = _safe_file(candidate_dir, name)
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        actual_hash, actual_size = hashlib.sha256(), 0
                        with archive.open(info) as source, target.open("xb") as destination:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                actual_size += len(chunk)
                                if actual_size > item["size"]:
                                    raise MaintenanceError("BACKUP_EXPANSION_LIMIT")
                                actual_hash.update(chunk)
                                destination.write(chunk)
                        if actual_size != item["size"] or actual_hash.hexdigest() != item["sha256"]:
                            raise MaintenanceError("BACKUP_HASH_MISMATCH")
                database = candidate_dir / "library.sqlite3"
                upgraded = False
                try:
                    artifacts, counts = _validate_database(database, expected_schema=expected_schema)
                except MaintenanceError as error:
                    if error.code != "BACKUP_SCHEMA_INCOMPATIBLE" or expected_schema is None:
                        raise
                    from .schema_upgrade import upgrade_previous_library
                    upgrade_previous_library(database, expected_schema)
                    artifacts, counts = _validate_database(database, expected_schema=expected_schema)
                    upgraded = True
                expected_files = {"library.sqlite3"} | {name for _, name, _ in artifacts}
                if set(files) != expected_files or counts != manifest["counts"]:
                    raise MaintenanceError("BACKUP_MANIFEST_INVALID")
                for artifact_hash, name, size in artifacts:
                    if files[name] != {"sha256": artifact_hash, "size": size}:
                        raise MaintenanceError("BACKUP_ARTIFACT_INVALID")
                if upgraded:
                    # Source ZIP hashes were checked before migration. Bind restore
                    # confirmation to the upgraded candidate's bytes as usual.
                    manifest["files"]["library.sqlite3"] = {"size": database.stat().st_size, "sha256": digest(database)}
                (candidate_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            return manifest
        except (zipfile.BadZipFile, EOFError, sqlite3.DatabaseError, OSError, RuntimeError) as exc:
            raise MaintenanceError("BACKUP_INVALID", "Backup is corrupt, incompatible or could not be read") from exc


def _api_error(code, message, status=400):
    from library.errors import ApiError
    return ApiError(code, message, status=status)


def serialize_operation(operation, *, include_confirmation=False):
    from django.conf import settings
    value = {"id": str(operation.id), "operation_id": str(operation.id), "kind": operation.kind,
             "state": operation.state, "stage": operation.stage, "error_code": operation.error_code,
             "result": {key: val for key, val in operation.result.items() if key not in {"package_path", "candidate_path", "backup_path", "path"}},
             "created_at": operation.created_at.isoformat(), "updated_at": operation.updated_at.isoformat()}
    if include_confirmation and operation.state == "awaiting_confirmation":
        # Deterministic signed challenge returned to the authenticated owner; only its hash is persisted.
        import hmac
        token = hmac.new(settings.SECRET_KEY.encode(), (str(operation.id) + "\n" + operation.package_sha256 +
            "\n" + str(operation.expected_library_epoch)).encode(), hashlib.sha256).hexdigest()
        value["validation_token"] = token
        value["validation_expires_at"] = operation.validation_expires_at.isoformat()
    return value


def create_backup_operation():
    from .models import MaintenanceOperation, RuntimeState
    state = RuntimeState.objects.using("default").get(pk=1)
    return MaintenanceOperation.objects.using("default").create(kind="backup", state="queued",
        expected_library_epoch=state.library_epoch, old_generation=state.active_generation)


def create_restore_validation(chunks):
    from config.paths import DATA_ROOT
    from .models import MaintenanceOperation, RuntimeState
    state = RuntimeState.objects.using("default").get(pk=1)
    operation = MaintenanceOperation.objects.using("default").create(kind="restore", state="uploading",
        stage="uploaded", expected_library_epoch=state.library_epoch, old_generation=state.active_generation,
        target_generation="restore-" + uuid.uuid4().hex)
    path = DATA_ROOT / "work" / (str(operation.id) + ".bookmarklib.zip")
    chunks = [chunks] if isinstance(chunks, bytes) else chunks
    size, checksum, created = 0, hashlib.sha256(), False
    try:
        _reject_links(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as output:
            created = True
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_EXPANDED_BYTES:
                    raise MaintenanceError("BACKUP_UPLOAD_TOO_LARGE")
                output.write(chunk)
                checksum.update(chunk)
        operation.source_package, operation.package_sha256, operation.state = str(path), checksum.hexdigest(), "validating"
        operation.save(using="default", update_fields=["source_package", "package_sha256", "state", "updated_at"])
        return operation
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        MaintenanceOperation.objects.using("default").filter(pk=operation.id).update(state="failed", error_code="UPLOAD_FAILED")
        raise


def confirm_restore(operation_id, token, *, overwrite=False):
    from django.db import transaction
    from django.utils import timezone
    from .models import MaintenanceOperation, RuntimeState
    with transaction.atomic(using="default"):
        operation = MaintenanceOperation.objects.using("default").get(pk=operation_id)
        state = RuntimeState.objects.using("default").get(pk=1)
        if operation.state in {"queued", "running", "succeeded"}:
            return operation
        if not overwrite or operation.state != "awaiting_confirmation" or (
                operation.expected_library_epoch != state.library_epoch or not operation.validation_expires_at or
                timezone.now() >= operation.validation_expires_at or not isinstance(token, str) or
                not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), operation.validation_token_hash)):
            raise _api_error("RESTORE_CONFIRMATION_INVALID", "恢复确认已过期或收藏库已变化", 409)
        operation.state, operation.stage = "queued", "confirmed"
        operation.save(using="default", update_fields=["state", "stage", "updated_at"])
        return operation


def _claim(operation_id, *, validating=False):
    from django.db import IntegrityError, transaction
    from .models import MaintenanceOperation
    token = uuid.uuid4()
    try:
        with transaction.atomic(using="default"):
            count = MaintenanceOperation.objects.using("default").filter(pk=operation_id,
                state="validating" if validating else "queued", active_key__isnull=True).update(
                    state="running", active_key="maintenance", claim_token=token)
            if not count:
                return None
    except IntegrityError:
        return None
    return MaintenanceOperation.objects.using("default").get(pk=operation_id)


def _set_stage(operation, stage, **fields):
    from django.utils import timezone
    from .models import MaintenanceOperation
    fields.setdefault("updated_at", timezone.now())
    MaintenanceOperation.objects.using("default").filter(pk=operation.id, claim_token=operation.claim_token).update(
        stage=stage, **fields)
    operation.stage = stage
    _record_stage(operation, stage)


def _record_stage(operation, stage):
    """Observe existing stage boundaries without changing persisted state."""
    if not performance.enabled():
        return
    now = time.monotonic()
    previous = getattr(operation, "_performance_stage", None)
    started = getattr(operation, "_performance_stage_started", None)
    if previous is not None and started is not None:
        performance.emit("maintenance.stage", category="maintenance", maintenance_id=str(operation.id),
            kind=operation.kind, stage=previous, duration_ms=(now - started) * 1000)
    operation._performance_stage, operation._performance_stage_started = stage, now
    performance.emit("maintenance.stage_enter", category="maintenance", maintenance_id=str(operation.id),
        kind=operation.kind, stage=stage)


def _cancel_candidate_work(database):
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("UPDATE library_job SET state='cancelled',error_code='cancelled_by_restore',active_key=NULL,active_attempt_id=NULL WHERE state IN ('queued','running','retry_wait')")
        connection.execute("UPDATE library_jobattempt SET state='cancelled',error_code='cancelled_by_restore',lease_expires_at=CURRENT_TIMESTAMP WHERE state IN ('running','queued','retry_wait')")
        connection.execute("DELETE FROM library_libraryidempotency")
        connection.execute("UPDATE library_aiprofile SET enabled=0")
        connection.execute("UPDATE library_importbatch SET state='cancelled',review_notified=1 WHERE state IN ('queued','running')")
        connection.commit()
        if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise MaintenanceError("RESTORE_CHECKPOINT_FAILED")


def _complete_operation(operation, result):
    from django.db import transaction
    from .models import MaintenanceOperation, RuntimeState
    with transaction.atomic(using="default"):
        RuntimeState.objects.using("default").filter(pk=1).update(maintenance_mode=False, active_maintenance_id=None)
        MaintenanceOperation.objects.using("default").filter(pk=operation.id, claim_token=operation.claim_token).update(
            state="succeeded", stage="completed", result=result, active_key=None, claim_token=None)
    _record_stage(operation, "completed")


def process_operation(operation_id, *, stop_services, start_services, ready_services):
    """Called by the single-instance supervisor, not by the library worker."""
    from datetime import timedelta
    from django.conf import settings
    from django.db import transaction
    from django.utils import timezone
    from config.paths import DATA_ROOT, generation_root
    from .models import MaintenanceOperation, ProviderSecret, RuntimeState
    current = MaintenanceOperation.objects.using("default").get(pk=operation_id)
    validating = current.state == "validating"
    operation = _claim(operation_id, validating=validating)
    if operation is None:
        return False
    with performance.trace("maintenance.operation", category="maintenance", maintenance_id=str(operation.id), kind=operation.kind) as timing:
        if operation.kind in {"clear", "reset"}:
            from .data_reset import process_data_operation
            return process_data_operation(operation, stop_services=stop_services,
                start_services=start_services, ready_services=ready_services)
        stopped = switched = False
        old_generation = RuntimeState.objects.using("default").get(pk=1).active_generation
        try:
            if validating:
                _set_stage(operation, "validating_package")
                source = Path(operation.source_package)
                candidate = generation_root(operation.target_generation)
                manifest = validate_package(source, candidate,
                    expected_schema=generation_root(old_generation) / "library.sqlite3", expected_sha256=operation.package_sha256,
                    summary=True)
                import hmac
                token = hmac.new(settings.SECRET_KEY.encode(), (str(operation.id) + "\n" + operation.package_sha256 +
                    "\n" + str(operation.expected_library_epoch)).encode(), hashlib.sha256).hexdigest()
                _set_stage(operation, "awaiting_confirmation", state="awaiting_confirmation", active_key=None,
                    claim_token=None, validation_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    validation_expires_at=timezone.now() + timedelta(seconds=RESTORE_CONFIRMATION_SECONDS), result={"manifest": manifest})
                return True
            state = RuntimeState.objects.using("default").get(pk=1)
            if operation.expected_library_epoch != state.library_epoch:
                raise MaintenanceError("RESTORE_LIBRARY_CHANGED")
            RuntimeState.objects.using("default").filter(pk=1).update(maintenance_mode=True, active_maintenance_id=operation.id)
            _set_stage(operation, "stopping_services", old_generation=old_generation)
            stop_services()
            stopped = True
            if operation.kind == "backup":
                _set_stage(operation, "creating_backup")
                output = DATA_ROOT / "backups" / (str(operation.id) + ".bookmarklib.zip")
                result = create_backup(generation_root(old_generation), output)
                result["manifest"] = _manifest_summary(result["manifest"])
                result["package_path"] = str(output)
                _set_stage(operation, "starting_services", result=result)
                start_services()
                stopped = False
                if not ready_services():
                    raise MaintenanceError("BACKUP_RESTART_FAILED")
            elif operation.kind == "restore":
                _set_stage(operation, "creating_pre_restore_backup")
                prebackup = DATA_ROOT / "backups" / (str(operation.id) + ".pre-restore.bookmarklib.zip")
                if not prebackup.exists():
                    create_backup(generation_root(old_generation), prebackup)
                candidate = generation_root(operation.target_generation)
                _set_stage(operation, "preparing_candidate")
                # Confirmation does not make SQLite trusted: revalidation and
                # imported-job cancellation share the same process budgets.
                manifest = validate_candidate(candidate,
                    expected_schema=generation_root(old_generation) / "library.sqlite3", prepare=True, summary=True)
                from django.contrib.sessions.models import Session
                with transaction.atomic(using="default"):
                    state = RuntimeState.objects.using("default").get(pk=1)
                    if state.library_epoch != operation.expected_library_epoch:
                        raise MaintenanceError("RESTORE_LIBRARY_CHANGED")
                    state.active_generation, state.library_epoch = operation.target_generation, state.library_epoch + 1
                    state.save(using="default")
                    Session.objects.using("default").all().delete()
                    ProviderSecret.objects.using("default").all().update(verified=False, verified_library_epoch=None)
                    _set_stage(operation, "generation_switched")
                switched = True
                start_services()
                stopped = False
                if not ready_services():
                    raise MaintenanceError("RESTORE_START_FAILED")
                result = {"manifest": manifest, "pre_restore_backup": prebackup.name, "requires_login": True}
            else:
                raise MaintenanceError("MAINTENANCE_KIND_UNSUPPORTED")
            _complete_operation(operation, result)
            return True
        except BaseException as exc:
            code = getattr(exc, "code", "MAINTENANCE_FAILED")
            timing.set_fields(result_state="handled_error", error_code=code)
            try:
                if switched:
                    stop_services()
                    with transaction.atomic(using="default"):
                        state = RuntimeState.objects.using("default").get(pk=1)
                        state.active_generation, state.library_epoch = old_generation, state.library_epoch + 1
                        state.save(using="default")
                    _set_stage(operation, "rolling_back")
                    start_services()
                    if not ready_services():
                        raise MaintenanceError("RESTORE_ROLLBACK_FAILED")
                elif stopped:
                    start_services()
                RuntimeState.objects.using("default").filter(pk=1).update(maintenance_mode=False, active_maintenance_id=None)
                _set_stage(operation, "failed_rolled_back" if switched else "failed", state="failed", error_code=code,
                    active_key=None, claim_token=None)
            except BaseException:
                _set_stage(operation, "rollback_failed", state="failed", error_code="RESTORE_ROLLBACK_FAILED", active_key=None, claim_token=None)
            return False
        finally:
            # Staging is not part of the durable backup/rollback contract.
            # Cleanup failures are retried by the supervisor's periodic sweep.
            try:
                cleanup_restore_data()
            except (OSError, MaintenanceError):
                performance.emit("maintenance.cleanup_failed", category="maintenance", maintenance_id=str(operation.id))


from .maintenance_retention import cleanup_restore_data, cancel_restore
