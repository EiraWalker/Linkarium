"""Publish tested Monolith fixture artifacts into the isolated UI acceptance library."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
os.environ["BOOKMARK_DATA_ROOT"] = str(root / "output" / "acceptance-data")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
import django
django.setup()
from django.db import transaction
from library import catalog, queue
from library.worker import publish_snapshot
from library.models import CaptureState

reports = root / "tests" / "capture" / "results"
artifacts = {item["variant"]: Path(item["result"]["artifact_path"]) for item in json.loads((reports / "report.json").read_text(encoding="utf-8"))}
assert all(path.exists() for path in artifacts.values())
with patch("library.queue.initial_bookmark"):
    with transaction.atomic(using="library"):
        bookmark, _ = catalog.create_bookmark({"url": "https://fixture.example.test/visual", "title": "动态页面归档测试", "notes": "仅验收数据：浏览器动态 DOM、canvas、Shadow DOM 和 iframe 的 Monolith 归档。", "snapshot_enabled": True, "auto_refresh_paused": True})
        for variant, path in artifacts.items():
            job = queue.enqueue("capture", bookmark.id, variant, priority=1000, input_revision=bookmark.capture_epoch, active_key=f"acceptance:{bookmark.id}:{variant}")
            claimed = queue.claim_next()
            assert claimed and claimed[0].id == job.id
            job, attempt = claimed
            publish_snapshot(job, attempt, {"artifact_path": path, "viewport_width": 1366 if variant == "desktop" else 390, "viewport_height": 768 if variant == "desktop" else 844, "title": "DOM ready", "final_url": bookmark.canonical_url, "warnings": []})
print("Published fixture desktop/mobile HTML into isolated UI acceptance library")
