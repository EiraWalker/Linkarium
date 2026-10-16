"""Only seed the isolated UI acceptance library, using real archived fixture HTML.

No network capture occurs. Each immutable version has its own claimed attempt.
Never point this script at a user's application data directory.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "backend"))
os.environ["BOOKMARK_DATA_ROOT"] = str(root / "output" / "acceptance-data")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
import django
django.setup()
from django.db import transaction
from library import catalog, queue
from library.worker import publish_snapshot

artifacts = {
    "desktop": root / "tests/capture/results/d56ce2e2b2123d16ab70295d04aa733f1e7ae79049bf2e32bc07a509d39fc654.html",
    "mobile": root / "tests/capture/results/b7efd33c47bb4abbcbc1fb33367cd7c542e3e8670d29cf5c9908191879139736.html",
}
assert all(path.is_file() for path in artifacts.values())
for index in range(1, 13):
    with patch("library.queue.initial_bookmark"):
        with transaction.atomic(using="library"):
            bookmark, created = catalog.create_bookmark({
                "url": f"https://fixture.example.test/budget-{index}",
                "title": f"动态页面预览 {index:02}",
                "notes": "仅验收 fixture：统一 iframe 预算，无网络采集。",
                "snapshot_enabled": True,
                "auto_refresh_paused": True,
            })
            if not created:
                continue
            for variant, path in artifacts.items():
                job = queue.enqueue("capture", bookmark.id, variant, priority=100000,
                    input_revision=bookmark.capture_epoch,
                    active_key=f"acceptance-preview:{bookmark.id}:{variant}")
                claimed = queue.claim_next()
                assert claimed and claimed[0].id == job.id
                job, attempt = claimed
                publish_snapshot(job, attempt, {
                    "artifact_path": path,
                    "viewport_width": 1366 if variant == "desktop" else 390,
                    "viewport_height": 768 if variant == "desktop" else 844,
                    "title": "DOM ready", "final_url": bookmark.canonical_url, "warnings": [],
                })
print("Seeded 12 real HTML preview fixtures in isolated acceptance library.")
