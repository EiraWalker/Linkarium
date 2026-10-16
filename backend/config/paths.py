"""Resolve a library generation before Django opens its connections."""
import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("BOOKMARK_DATA_ROOT", PROJECT_ROOT / "data")).resolve()


def current_generation():
    database = DATA_ROOT / "state.sqlite3"
    if database.exists():
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
            try:
                row = connection.execute("SELECT active_generation FROM runtime_runtimestate WHERE id=1").fetchone()
            except sqlite3.OperationalError:
                row = None
        if row:
            return row[0]
    return "initial"


def generation_root(generation=None):
    value = generation or current_generation()
    if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
        raise ValueError("Invalid library generation")
    return DATA_ROOT / "generations" / value


def prepare_paths():
    from .storage_permissions import check_data_directory, protect_data_directory
    check_data_directory(DATA_ROOT)
    for path in (DATA_ROOT, DATA_ROOT / "runtime", DATA_ROOT / "work", DATA_ROOT / "work" / "http", DATA_ROOT / "backups", generation_root(), generation_root() / "artifacts"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect_data_directory(DATA_ROOT)


def secret_key():
    supplied = os.environ.get("DJANGO_SECRET_KEY")
    if supplied:
        return supplied
    path = DATA_ROOT / "runtime" / "secret-key"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        import secrets
        value = secrets.token_urlsafe(64)
        try:
            with path.open("x", encoding="utf-8") as output:
                output.write(value)
            path.chmod(0o600)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip()
        return value
