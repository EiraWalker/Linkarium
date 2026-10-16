"""Real API -> worker -> Monolith acceptance against a public example page.

Requires tests/run_acceptance_server.py with BOOKMARK_CAPTURE_DNS_MODE=public-doh.
Uses only the isolated acceptance library and its test owner, never the main data.
"""
import json
import time
import uuid
from pathlib import Path
import httpx

root = Path(__file__).resolve().parents[1]
password = (root / "output/acceptance-data/runtime/acceptance-password").read_text(encoding="utf-8")
with httpx.Client(base_url="http://127.0.0.1:18765", trust_env=False, timeout=15) as client:
    token = client.get("/api/v1/session").json()["data"]["csrf_token"]
    response = client.post("/api/v1/session/login", json={"password": password}, headers={"X-CSRFToken": token})
    response.raise_for_status()
    token = client.get("/api/v1/session").json()["data"]["csrf_token"]
    client.headers.update({"X-CSRFToken": token})
    response = client.post("/api/v1/bookmarks", json={"url": "https://example.com/", "title": "Example 公共网页验收",
        "notes": "真实公开页面：实际 API 入队、worker 动态浏览器与 Monolith 归档、双设备私有 HTML 返回。", "snapshot_enabled": True,
        "auto_refresh_paused": True})
    response.raise_for_status()
    bookmark_id = response.json()["data"]["id"]
    refresh = client.post(f"/api/v1/bookmarks/{bookmark_id}/refresh", json={}, headers={"Idempotency-Key": str(uuid.uuid4())})
    refresh.raise_for_status()
    group_id = refresh.json()["data"]["id"]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        group = client.get(f"/api/v1/job-groups/{group_id}").json()["data"]
        if group.get("finished") or group.get("state") in ("succeeded", "partial", "failed", "cancelled"):
            break
        print(json.dumps({"group_id": group_id, "counts": group.get("counts")}), flush=True)
        time.sleep(3)
    detail = client.get(f"/api/v1/bookmarks/{bookmark_id}").json()["data"]
    variants = []
    for variant in ("desktop", "mobile"):
        summary = client.get("/api/v1/bookmarks", params={"q": "example.com", "fields": "domain", "variant": variant}).json()["data"]
        bookmark = next(item for item in summary if item["id"] == bookmark_id)
        preview = bookmark["preview"]
        assert preview, {"variant": variant, "state": bookmark["capture_status"], "group": group}
        archived = client.get(preview["preview_url"])
        assert archived.status_code == 200
        assert "script-src 'none'" in archived.headers["content-security-policy"]
        assert b"Example Domain" in archived.content
        variants.append({"variant": variant, "version_id": preview["version_id"], "html_bytes": len(archived.content),
            "viewport": [preview["viewport_width"], preview["viewport_height"]], "state": bookmark["capture_status"]["state"]})
    report = {"passed": True, "url": "https://example.com/", "bookmark_id": bookmark_id, "group": group,
        "variants": variants, "scope": "real loopback HTTP API and worker, public target through explicit development public-doh; not production isolation acceptance"}
    output = root / "output/validation/public-snapshot-api.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
