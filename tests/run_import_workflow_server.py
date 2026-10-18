"""Isolated UI fixture for workflow ordering; capture output is deterministic.

Uses the real database queue, HTTP API, snapshot publisher and AI transport.
Does not fetch user bookmarks or validate Chromium/Monolith fidelity.
"""
import os
from pathlib import Path
import secrets
import json
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["BOOKMARK_DATA_ROOT"] = os.environ.get("BOOKMARK_ACCEPTANCE_DATA_ROOT", str(ROOT / "output" / "import-workflow-data"))
ACCEPTANCE_ROOT = Path(os.environ["BOOKMARK_DATA_ROOT"]).resolve()
if not ACCEPTANCE_ROOT.is_relative_to((ROOT / 'output').resolve()):
    raise SystemExit('Workflow acceptance requires an isolated data directory below this checkout/output')
PORT = int(os.environ.get('BOOKMARK_ACCEPTANCE_PORT', '18767'))
os.environ["BOOKMARK_DEBUG"] = "1"
os.environ["BOOKMARK_AUTH_DEBUG"] = "1"
os.environ["BOOKMARK_CAPTURE_MODE"] = "development"
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from config.paths import DATA_ROOT
from library import worker
from library.errors import ApiError
from library.models import Bookmark
from supervisor import InstanceLock


def fixture_capture(job, attempt):
    time.sleep(1)
    bookmark = Bookmark.objects.get(pk=job.target_id)
    if "/failed" in bookmark.canonical_url:
        raise ApiError("CAPTURE_BLOCKED", "Controlled failure")
    path = DATA_ROOT / "work" / "capture" / str(attempt.id) / "fixture.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<!doctype html><html><body><h1>Workflow fixture {job.variant}</h1></body></html>', encoding="utf-8")
    return {"artifact_path": str(path), "title": "Workflow fixture", "text_excerpt": "Controlled page content",
            "final_url": bookmark.canonical_url, "viewport_width": 1366 if job.variant == "desktop" else 390,
            "viewport_height": 768 if job.variant == "desktop" else 844}


if __name__ == "__main__":
    call_command("initialize", no_password=True, verbosity=0)
    credential = DATA_ROOT / "runtime" / "acceptance-password"
    password = credential.read_text() if credential.exists() else secrets.token_urlsafe(24)
    credential.write_text(password)
    user, _ = get_user_model().objects.get_or_create(username="owner")
    user.set_password(password)
    user.save()
    with InstanceLock(DATA_ROOT / "runtime" / "supervisor.lock"), patch("library.worker.capture_page", fixture_capture):
        (DATA_ROOT / 'runtime' / 'acceptance-server.json').write_text(json.dumps({
            'pid': os.getpid(), 'port': PORT, 'data_root': str(DATA_ROOT),
            'fixture_capture': True, 'real_queue': True,
        }), encoding='utf-8')
        threading.Thread(target=worker.run_worker, daemon=True).start()
        print(f"Import workflow fixture: http://127.0.0.1:{PORT} pid={os.getpid()} (isolated library)", flush=True)
        call_command("run_web", host="127.0.0.1", port=PORT)
