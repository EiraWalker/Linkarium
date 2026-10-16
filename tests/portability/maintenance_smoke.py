"""Run actual supervisor/API backup/restore against an isolated temporary library."""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

PROJECT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def main():
    port = 18766
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
    with tempfile.TemporaryDirectory(prefix="bookmark-maintenance-smoke-") as temp:
        root = Path(temp)
        env = {**os.environ, "BOOKMARK_DATA_ROOT": str(root / "data"), "BOOKMARK_CAPTURE_MODE": "development" if os.name == "nt" else "isolated",
               "E2E_PASSWORD": secrets.token_urlsafe(24), "PYTHONDONTWRITEBYTECODE": "1"}
        manage = str(PROJECT / "backend" / "manage.py")
        subprocess.run([PYTHON, manage, "initialize", "--no-password", "--debug"], env=env, cwd=PROJECT,
                       check=True, stdout=subprocess.DEVNULL)
        code = "import os;from django.contrib.auth import get_user_model;u,_=get_user_model().objects.get_or_create(username='owner');u.set_password(os.environ['E2E_PASSWORD']);u.save()"
        subprocess.run([PYTHON, manage, "shell", "--command", code], env=env, cwd=PROJECT,
                       check=True, stdout=subprocess.DEVNULL)
        log_path = root / "supervisor.log"
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with log_path.open("wb") as log:
            process = subprocess.Popen([PYTHON, str(PROJECT / "backend" / "supervisor.py"),
                "--host", "127.0.0.1", "--port", str(port), "--debug"], cwd=PROJECT, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **options)
            client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False)
            token = None
            session_revoked = False
            def login():
                nonlocal token
                token = client.get("/api/v1/session").json()["data"]["csrf_token"]
                response = client.post("/api/v1/session/login", json={"password": env["E2E_PASSWORD"]},
                    headers={"X-CSRFToken": token})
                response.raise_for_status()
                token = client.get("/api/v1/session").json()["data"]["csrf_token"]
            def post(path, body=None, files=None):
                headers = {"X-CSRFToken": token, "Idempotency-Key": str(uuid.uuid4())}
                response = client.post(path, json=body if files is None else None, files=files, headers=headers)
                if response.status_code >= 400:
                    raise AssertionError(f"{path}: {response.status_code} {response.text[:300]}")
                return response.json()["data"]
            def wait_operation(kind, identity, terminal):
                nonlocal session_revoked
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError("Supervisor exited unexpectedly")
                    try:
                        response = client.get(f"/api/v1/{kind}/{identity}")
                        if response.status_code == 401:
                            session_revoked = True
                            login()
                            continue
                        if response.status_code == 200:
                            value = response.json()["data"]
                            if value["state"] == "failed":
                                raise AssertionError(value)
                            if value["state"] in terminal:
                                return value
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                raise AssertionError("Maintenance operation did not finish")
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError("Supervisor failed to start")
                    try:
                        if client.get("/health/ready").json().get("ready") is True:
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.2)
                else:
                    raise AssertionError("Web did not become ready")
                login()
                post("/api/v1/bookmarks", {"url": "https://kept.example/", "title": "Kept", "snapshot_enabled": False})
                profile = post("/api/v1/ai-profiles", {"base_url": "https://provider.example/v1", "model": "test",
                    "api_key": "test-only-secret", "enabled": False})
                backup = post("/api/v1/backups", {})
                backed_up = wait_operation("backups", backup["operation_id"], {"succeeded"})
                package = client.get(f"/api/v1/backups/{backup['operation_id']}/download")
                package.raise_for_status()
                post("/api/v1/bookmarks", {"url": "https://removed.example/", "snapshot_enabled": False})
                self_count = client.get("/api/v1/bookmarks").json()["page"]["total"]
                assert self_count == 2, self_count
                restore = post("/api/v1/restores/validate", files={"file": ("backup.bookmarklib.zip", package.content, "application/zip")})
                validated = wait_operation("restores", restore["operation_id"], {"awaiting_confirmation"})
                post(f"/api/v1/restores/{restore['operation_id']}/execute", {"overwrite": True,
                    "validation_token": validated["validation_token"]})
                restored = wait_operation("restores", restore["operation_id"], {"succeeded"})
                bookmarks = client.get("/api/v1/bookmarks").json()
                assert bookmarks["page"]["total"] == 1, bookmarks
                assert bookmarks["data"][0]["url"] == "https://kept.example/", bookmarks
                profiles = client.get("/api/v1/ai-profiles").json()["data"]
                restored_profile = next(item for item in profiles if item["id"] == profile["id"])
                assert restored_profile["has_api_key"] is True and restored_profile["verified"] is False
                assert session_revoked, "The old session was not revoked"
                state = client.get("/api/v1/session").json()["data"]
                assert state["library_epoch"] >= 2 and state["active_generation"].startswith("restore-")
                print(json.dumps({"passed": True, "backup": backed_up["state"], "restore": restored["state"],
                    "bookmarks_before_restore": 2, "bookmarks_after_restore": 1,
                    "session_revoked": session_revoked, "current_password_and_api_key_preserved": True,
                    "ai_requires_reverification": True, "library_epoch": state["library_epoch"]}))
            except BaseException:
                log.flush()
                print(log_path.read_text(encoding="utf-8", errors="replace")[-8000:], file=sys.stderr)
                raise
            finally:
                client.close()
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        os.killpg(process.pid, __import__("signal").SIGTERM)
                    process.wait(timeout=20)


if __name__ == "__main__":
    main()
