"""Isolated HTTP/worker fixture; fake only the external capture and AI services."""
import os
from pathlib import Path
import sys
import threading
import time
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "output" / "firefox-extension" / "acceptance-data"
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]
os.environ.update(BOOKMARK_DATA_ROOT=str(DATA), DJANGO_SETTINGS_MODULE="config.settings",
    BOOKMARK_AUTH_DEBUG="0", BOOKMARK_DEBUG="0", BOOKMARK_HTTPS="0", BOOKMARK_TRUST_PROXY="0",
    BOOKMARK_ALLOWED_HOSTS="localhost,127.0.0.1", BOOKMARK_PASSKEY_ORIGIN="http://127.0.0.1:18831")

import django
django.setup()
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.db import close_old_connections
from django.utils import timezone
from runtime.file_keys import generate_private_key
from library.classification import configure_profile, test_profile
from runtime.models import RuntimeState
from library.worker import run_one
from tests.run_ai_acceptance_provider import Handler
import capture

key_file = DATA / "runtime" / "fixture-key.pem"
key_file.parent.mkdir(parents=True, exist_ok=True)
if not key_file.exists():
    generate_private_key(key_file)
call_command("initialize", private_key=str(key_file), verbosity=0)

provider = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=provider.serve_forever, daemon=True).start()
profile = configure_profile({"base_url": f"http://127.0.0.1:{provider.server_port}/v1", "model": "fixture",
    "api_key": "ui-fixture-key"})
test_profile(profile.id)


def fixture_capture(self, request):
    values = {"title": "Controlled page", "description": "A public art tutorial", "text_excerpt": "Painting techniques",
        "final_url": request.url, "viewport_width": 1280, "viewport_height": 720}
    if request.mode != "metadata":
        artifact = Path(request.output_dir) / "capture.html"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("<!doctype html><title>Controlled page</title><h1>Painting techniques</h1>", encoding="utf-8")
        values["artifact_path"] = str(artifact)
    return values


capture.LauncherClient.capture = fixture_capture
capture.LauncherClient.release = lambda *args, **kwargs: None


class Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        content = b"<!doctype html><title>Controlled page</title><h1>Painting techniques</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


source = ThreadingHTTPServer(("127.0.0.1", 18832), Page)
threading.Thread(target=source.serve_forever, daemon=True).start()


def work():
    while True:
        RuntimeState.objects.filter(pk=1).update(worker_heartbeat=timezone.now())
        run_one()
        close_old_connections()
        threading.Event().wait(.1)


threading.Thread(target=work, daemon=True).start()
print("Extension fixture: http://127.0.0.1:18831; external AI/capture are controlled protocol fixtures", flush=True)
from waitress import serve
application = get_wsgi_application()


def delayed_submission(environ, start_response):
    phase = None
    if environ.get("PATH_INFO") == "/api/v1/extension/submissions" and environ.get("REQUEST_METHOD") == "POST":
        body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        environ["wsgi.input"] = io.BytesIO(body)
        url = json.loads(body).get("url", "")
        phase = "before" if "delay-before=1" in url else "after" if "delay-after=1" in url else None
    if phase == "before":
        (DATA.parent / "delay-before-entered").write_text("ready")
        time.sleep(2)
    result = application(environ, start_response)
    if phase == "after":
        (DATA.parent / "delay-after-entered").write_text("ready")
        time.sleep(2)
    return result


serve(delayed_submission, host="127.0.0.1", port=18831, threads=4)
