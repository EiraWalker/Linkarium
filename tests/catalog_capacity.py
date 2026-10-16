"""Measure metadata queries on an isolated 1000-site catalog, not capture throughput."""
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
temp = tempfile.TemporaryDirectory(prefix="bookmark-capacity-")
os.environ["BOOKMARK_DATA_ROOT"] = temp.name
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["BOOKMARK_AUTH_DEBUG"] = "1"
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.test import Client
from library.models import Bookmark, Folder, Tag, BookmarkTag, CaptureState

call_command("initialize", no_password=True, verbosity=0)
user = get_user_model().objects.create_user("owner")
folders = [Folder(name=f"分类 {i}", name_key=f"分类 {i}") for i in range(100)]
Folder.objects.bulk_create(folders)
tags = [Tag(name=f"标签 {i}", name_key=f"标签 {i}") for i in range(10)]
Tag.objects.bulk_create(tags)
bookmarks = [Bookmark(original_url=f"https://site{i}.example.org/", canonical_url=f"https://site{i}.example.org/", domain=f"site{i}.example.org", source_title=f"参考网站 {i}", notes="设计 开发 工具", folder=folders[i % 100], auto_refresh_paused=True) for i in range(1000)]
Bookmark.objects.bulk_create(bookmarks)
BookmarkTag.objects.bulk_create([BookmarkTag(bookmark=b, tag=tags[i % 10]) for i, b in enumerate(bookmarks)])
CaptureState.objects.bulk_create([CaptureState(bookmark=b, variant=variant) for b in bookmarks for variant in ("desktop", "mobile")])
client = Client()
client.force_login(user)
report = {"sites": 1000, "folders": 100, "tags": 10, "device_states": 2000, "scope": "Django metadata request handling; no HTTP transport, actual captures or phone-memory claims", "queries": {}}
for query in ("bookmarks", "bookmarks?q=设计&fields=notes", "bookmarks?fields=folder&q=分类%209", "folders"):
    times = []
    for _ in range(21):
        started = time.perf_counter()
        response = client.get("/api/v1/" + query)
        assert response.status_code == 200, response.content
        times.append((time.perf_counter()-started)*1000)
    measured = sorted(times[1:])
    report["queries"][query] = {"p50_ms": round(statistics.median(measured), 2), "p95_ms": round(measured[18], 2)}
output = root / "output" / "validation" / "catalog-capacity.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
from django.db import connections
connections.close_all()
temp.cleanup()
