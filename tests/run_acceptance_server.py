"""Create an isolated local acceptance library; never change the user's owner."""
import os
import secrets
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
os.environ["BOOKMARK_DATA_ROOT"] = str(root / "output" / "acceptance-data")
os.environ["BOOKMARK_CAPTURE_MODE"] = "development"
os.environ["BOOKMARK_DEBUG"] = "1"
os.environ["BOOKMARK_AUTH_DEBUG"] = "1"
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from config.paths import DATA_ROOT

call_command("initialize", no_password=True, verbosity=0)
password_file = DATA_ROOT / "runtime" / "acceptance-password"
password = password_file.read_text(encoding="utf-8") if password_file.exists() else secrets.token_urlsafe(24)
if not password_file.exists():
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
user, created = get_user_model().objects.get_or_create(username="owner")
if created or not user.check_password(password):
    user.set_password(password)
    user.save()
port = int(os.environ.get("BOOKMARK_ACCEPTANCE_PORT", "18765"))
print(f"Acceptance server: http://127.0.0.1:{port}; test password stored in output/acceptance-data/runtime/acceptance-password", flush=True)
from supervisor import Supervisor, InstanceLock
with InstanceLock(DATA_ROOT / "runtime" / "supervisor.lock"):
    Supervisor("127.0.0.1", port).run()
