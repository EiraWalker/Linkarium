"""Prepare databases offline without establishing the owner's identity.

Preparation owns the instance lock, sharing a caller's existing ownership. Only
an empty installation, or our interrupted preparation marker, permits automatic
migration. Existing installations always require the explicit offline command.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from django.core.management import call_command
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


class PreparationRequired(RuntimeError):
    pass


def _marker_payload(root):
    return {"format": "linkarium-database-preparation", "version": 1,
            "data_root": str(root.resolve()), "generation": "initial"}


def _empty_installation(root):
    # settings creates these two files before the preparation entrypoint runs.
    allowed = {"runtime/secret-key", "runtime/supervisor.lock"}
    for path in root.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_file() and path.relative_to(root).as_posix() not in allowed:
            return False
        if path.is_dir() and path.parent == root / "generations" and path.name != "initial":
            return False
    return True


def _has_preparation_marker(root, marker):
    if not marker.exists():
        return False
    try:
        valid = json.loads(marker.read_text(encoding="utf-8")) == _marker_payload(root)
    except (OSError, ValueError):
        valid = False
    if not valid:
        raise PreparationRequired("Preparation marker does not match this data directory; inspect it offline before running initialize.")
    return True


def _write_preparation_marker(root, marker):
    # Write before creating either database. An interrupted two-database migration
    # can then be resumed without treating arbitrary existing data as a new install.
    with marker.open("x", encoding="utf-8") as output:
        json.dump(_marker_payload(root), output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())


def _pending_migrations():
    pending = []
    for alias in ("default", "library"):
        executor = MigrationExecutor(connections[alias])
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            pending.append(alias)
    return pending


def _protect_incomplete_existing(root, library_path):
    """Do not turn a damaged existing collection into a new empty installation."""
    from django.conf import settings

    state_path = root / "state.sqlite3"
    generations = root / "generations"
    has_owner, generation = False, None
    if state_path.is_file():
        try:
            with closing(sqlite3.connect(f"file:{state_path.as_posix()}?mode=ro", uri=True)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "auth_user" in tables:
                    has_owner = connection.execute("SELECT 1 FROM auth_user WHERE username=?", (settings.BOOKMARK_OWNER,)).fetchone() is not None
                if "runtime_runtimestate" in tables:
                    row = connection.execute("SELECT active_generation FROM runtime_runtimestate WHERE id=1").fetchone()
                    generation = row[0] if row else None
        except sqlite3.DatabaseError:
            raise PreparationRequired("Existing state database is unreadable; restore or repair it offline before preparation.") from None
    elif any(generations.glob("*/library.sqlite3")):
        raise PreparationRequired("Existing library has no state database; restore its matching state database before preparation.")
    if generation is None and any(path.is_dir() and path.name != "initial" for path in generations.iterdir()):
        raise PreparationRequired("Existing generations have no active library pointer; restore or repair the state database before preparation.")
    artifacts = library_path.parent / "artifacts"
    if not library_path.is_file() and (has_owner or generation not in {None, "initial"}
                                       or any(path.is_file() for path in artifacts.rglob("*"))):
        raise PreparationRequired("Existing collection is missing its library database; restore or repair that database before preparation.")


def prepare_databases(*, automatic=False):
    """Exclusively prepare both stores without creating owner credentials.

    Automatic startup never upgrades an existing installation. The explicit
    offline initialize command can apply migrations and repair missing defaults;
    neither path creates, replaces or resets login credentials.
    """
    from config.paths import DATA_ROOT
    from runtime.instance_lock import InstanceLock

    with InstanceLock(DATA_ROOT / "runtime" / "supervisor.lock"):
        _prepare_databases(automatic=automatic)


def _prepare_databases(*, automatic):
    from config.paths import DATA_ROOT, generation_root
    from library.models import LibrarySettings
    from runtime.auth_grants import get_auth_state
    from runtime.models import AuthState, RuntimeState

    marker = DATA_ROOT / "runtime" / "preparing-databases.json"
    resuming = _has_preparation_marker(DATA_ROOT, marker)
    if resuming and Path(connections["library"].settings_dict["NAME"]).parent != DATA_ROOT / "generations" / "initial":
        raise PreparationRequired("Preparation marker does not match the active library generation; inspect the directory offline.")
    fresh = not resuming and _empty_installation(DATA_ROOT)
    if fresh:
        _write_preparation_marker(DATA_ROOT, marker)
    elif not resuming:
        _protect_incomplete_existing(DATA_ROOT, Path(connections["library"].settings_dict["NAME"]))
    can_prepare = not automatic or fresh or resuming
    database_paths = [connections[alias].settings_dict["NAME"] for alias in ("default", "library")]
    if automatic and not can_prepare and any(not Path(path).is_file() for path in database_paths):
        raise PreparationRequired("Incomplete data directory; stop services, back it up and run manage.py initialize for offline preparation.")
    pending = _pending_migrations()
    if pending and not can_prepare:
        raise PreparationRequired("Database upgrade required; stop services, back up the data directory, then run manage.py initialize before restarting.")
    if can_prepare:
        for alias in ("default", "library"):
            call_command("migrate", database=alias, interactive=False, verbosity=0)
        state, _ = RuntimeState.objects.using("default").get_or_create(pk=1)
        expected = generation_root(state.active_generation) / "library.sqlite3"
        if Path(connections["library"].settings_dict["NAME"]).resolve() != expected.resolve():
            raise PreparationRequired("Library generation changed during preparation; restart the offline initialize command.")
        LibrarySettings.objects.using("library").get_or_create(pk=1)
        get_auth_state()
        marker.unlink(missing_ok=True)
    elif not (RuntimeState.objects.using("default").filter(pk=1).exists()
              and LibrarySettings.objects.using("library").filter(pk=1).exists()
              and AuthState.objects.using("default").filter(pk=1).exists()):
        raise PreparationRequired("Database defaults are incomplete; stop services, back up the data directory and run manage.py initialize.")
