"""Startup acceptance uses child processes and temporary on-disk stores only."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREPARE = """
from config.paths import DATA_ROOT
from runtime.bootstrap import prepare_databases
from supervisor import InstanceLock
with InstanceLock(DATA_ROOT / 'runtime' / 'supervisor.lock'):
    prepare_databases(automatic=True)
"""


class BootstrapProcessTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="linkarium-bootstrap-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.env = {**os.environ, "BOOKMARK_DATA_ROOT": str(self.root),
                    "DJANGO_SETTINGS_MODULE": "config.settings", "BOOKMARK_AUTH_DEBUG": "0",
                    "BOOKMARK_PASSKEY_ORIGIN": "http://localhost:18790", "BOOKMARK_CAPTURE_MODE": "isolated",
                    "BOOKMARK_HTTPS": "0", "BOOKMARK_ALLOWED_HOSTS": "localhost,127.0.0.1",
                    "BOOKMARK_TRUST_PROXY": "0", "BOOKMARK_PERF": "0"}

    def run_command(self, *args, success=True):
        result = subprocess.run([sys.executable, "-X", "utf8", str(PROJECT_ROOT / "backend" / "manage.py"), *args],
                                env=self.env, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_code(self, code, success=True):
        preamble = "import sys; sys.path.insert(0, 'backend'); import django; django.setup()\n"
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", preamble + code],
                                env=self.env, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def scalar(self, sql, database="state.sqlite3"):
        with closing(sqlite3.connect(self.root / database)) as connection:
            return connection.execute(sql).fetchone()[0]

    def test_initialize_without_private_key_prepares_both_stores_without_owner(self):
        self.run_command("initialize")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM auth_user"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runtime_authstate"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runtime_runtimestate"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM library_librarysettings", "generations/initial/library.sqlite3"), 1)
        self.assertFalse((self.root / "runtime/preparing-databases.json").exists())
        self.run_code(PREPARE)

    def test_automatic_preparation_recovers_interrupted_second_database_migration(self):
        failed = self.run_code("""
from unittest.mock import patch
from runtime import bootstrap
original = bootstrap.call_command
def interrupted(*args, **kwargs):
    if kwargs.get('database') == 'library':
        raise RuntimeError('simulated interruption between databases')
    return original(*args, **kwargs)
with patch.object(bootstrap, 'call_command', side_effect=interrupted):
""" + "\n".join("    " + line for line in PREPARE.strip().splitlines()), success=False)
        self.assertIn("simulated interruption", failed.stderr)
        marker = self.root / "runtime/preparing-databases.json"
        self.assertTrue(marker.is_file())
        self.run_code(PREPARE)
        self.assertFalse(marker.exists())
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM auth_user"), 0)

    def test_automatic_start_rejects_unknown_partial_directory_and_preserves_content(self):
        directory = self.root / "generations/initial/artifacts"
        directory.mkdir(parents=True)
        artifact = directory / "existing-snapshot.html"
        artifact.write_bytes(b"existing business data")
        result = self.run_code(PREPARE, success=False)
        self.assertIn("missing its library database", result.stderr)
        self.assertEqual(artifact.read_bytes(), b"existing business data")
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_initialize_refuses_lost_state_database_for_existing_collection(self):
        directory = self.root / "generations/retained-library"
        directory.mkdir(parents=True)
        library = directory / "library.sqlite3"
        library.write_bytes(b"existing library must be preserved")
        result = self.run_command("initialize", success=False)
        self.assertIn("no state database", result.stderr)
        self.assertEqual(library.read_bytes(), b"existing library must be preserved")
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_automatic_start_requires_offline_upgrade_for_existing_schema(self):
        self.run_code("""
from django.core.management import call_command
call_command('migrate', 'runtime', '0002', database='default', verbosity=0)
call_command('migrate', database='library', verbosity=0)
from runtime.models import RuntimeState
from library.models import LibrarySettings
RuntimeState.objects.create(pk=1)
LibrarySettings.objects.create(pk=1)
""")
        result = self.run_code(PREPARE, success=False)
        self.assertIn("Database upgrade required", result.stderr)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM django_migrations WHERE app='runtime' AND name='0003_passkey_enrollment'"), 0)
        self.run_command("initialize")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM django_migrations WHERE app='runtime' AND name='0003_passkey_enrollment'"), 1)

    def test_offline_initialize_cannot_run_while_instance_lock_is_owned(self):
        from supervisor import InstanceLock
        with InstanceLock(self.root / "runtime/supervisor.lock"):
            result = self.run_command("initialize", success=False)
        self.assertIn("already owns this data directory", result.stderr)
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_direct_migration_cannot_run_while_instance_lock_is_owned(self):
        from runtime.instance_lock import InstanceLock
        with InstanceLock(self.root / "runtime/supervisor.lock"):
            result = self.run_command("migrate", "--database", "default", success=False)
        self.assertIn("already owns this data directory", result.stderr)
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_nested_lock_keeps_outer_ownership_and_releases_after_error(self):
        from runtime.instance_lock import InstanceLock
        lock_path = self.root / "runtime/supervisor.lock"
        with self.assertRaisesRegex(RuntimeError, "test interruption"):
            with InstanceLock(lock_path):
                with InstanceLock(lock_path.parent / "." / lock_path.name):
                    pass
                result = self.run_command("migrate", "--database", "default", success=False)
                self.assertIn("already owns this data directory", result.stderr)
                raise RuntimeError("test interruption")
        self.run_command("initialize")

    def test_initialization_code_reissue_replaces_only_bootstrap_and_does_not_create_owner(self):
        self.run_command("initialize")
        for _ in range(2):
            result = self.run_command("passkey_initialize")
            self.assertRegex(result.stdout, r"Authorization code: [0-9]{8}")
            self.assertIn("http://localhost:18790/login", result.stdout)
            self.assertIn("Expires at: never (single use)", result.stdout)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runtime_enrollmentgrant WHERE active_slot='bootstrap'"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runtime_enrollmentgrant WHERE invalidated_at IS NOT NULL"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM auth_user"), 0)

    def test_console_add_and_reset_keep_existing_credentials_and_separate_code_from_url(self):
        key = Path(self.temp.name) / "login.pem"
        self.run_command("file_key_setup", "--generate", str(key))
        self.run_command("initialize", "--private-key", str(key))
        for command, fragment in (("passkey_setup", "#enroll="), ("passkey_reset", "#reset=")):
            output = self.run_command(command).stdout
            url = re.search(r"^Open: (.+)$", output, re.MULTILINE).group(1)
            code = re.search(r"^Authorization code: ([0-9]{8})$", output, re.MULTILINE).group(1)
            self.assertIn(fragment, url)
            self.assertNotIn(code, url)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runtime_filekeycredential"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM auth_user"), 1)
        result = self.run_command("passkey_initialize")
        self.assertIn("First Passkey for the existing owner", result.stdout)
        self.assertEqual(re.search(r"^Open: (.+)$", result.stdout, re.MULTILINE).group(1), "http://localhost:18790/login")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runtime_filekeycredential"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM auth_user"), 1)
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as connection:
            lifetimes = dict(connection.execute("SELECT purpose, ROUND((julianday(expires_at)-julianday(created_at))*86400) FROM runtime_enrollmentgrant"))
        self.assertEqual(lifetimes, {"add_device": 300, "reset": 600, "first_passkey": None})

    def test_upgrade_keeps_active_initial_codes_and_other_authority_unchanged(self):
        self.run_command("initialize")
        self.run_code("""
from datetime import timedelta
from django.core.management import call_command
from django.utils import timezone
from runtime.models import EnrollmentGrant
call_command('migrate', 'runtime', '0003', database='default', verbosity=0)
now = timezone.now()
for purpose, status in [('bootstrap', 'active'), ('first_passkey', 'active'),
                        ('bootstrap', 'invalidated'), ('first_passkey', 'consumed'),
                        ('add_device', 'active'), ('reset', 'active')]:
    EnrollmentGrant.objects.create(purpose=purpose, origin='http://localhost:18790', rp_id='localhost',
        credential_epoch=1, issuer_kind='console', token_hash=purpose + status, code_hash='unchanged',
        active_slot=purpose if status == 'active' and purpose in ('bootstrap', 'first_passkey') else None,
        expires_at=now - timedelta(hours=1), failed_attempts=2,
        consumed_at=now if status == 'consumed' else None,
        invalidated_at=now if status == 'invalidated' else None)
call_command('migrate', 'runtime', '0004', database='default', verbosity=0)
assert EnrollmentGrant.objects.filter(expires_at__isnull=True).count() == 2
assert EnrollmentGrant.objects.filter(active_slot__isnull=False, expires_at__isnull=True).count() == 2
assert EnrollmentGrant.objects.exclude(code_hash='unchanged', failed_attempts=2).count() == 0
assert EnrollmentGrant.objects.filter(purpose__in=('add_device', 'reset'), expires_at__isnull=True).count() == 0
call_command('migrate', 'runtime', '0003', database='default', verbosity=0)
assert not EnrollmentGrant.objects.filter(expires_at__isnull=True).exists()
""")

    def test_console_origin_must_match_canonical_source(self):
        self.run_command("initialize")
        for origin in ("http://localhost:18791", "http://127.0.0.1:18790"):
            result = self.run_command("passkey_initialize", "--origin", origin, success=False)
            self.assertIn("configured HTTPS origin", result.stderr)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM runtime_enrollmentgrant"), 0)

    def supervisor_session(self):
        if not hasattr(self, "supervisor_port"):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                self.supervisor_port = listener.getsockname()[1]
        port = self.supervisor_port
        self.env["BOOKMARK_PASSKEY_ORIGIN"] = f"http://localhost:{port}"
        code = "import sys; sys.path.insert(0, 'backend'); import django; django.setup()\n" + PREPARE + f"""
import threading
from supervisor import Supervisor
owner = Supervisor(port={port})
def stop_on_input():
    sys.stdin.readline()
    owner.running = False
threading.Thread(target=stop_on_input, daemon=True).start()
with InstanceLock(DATA_ROOT / 'runtime' / 'supervisor.lock'):
    owner.run()
"""
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-u", "-c", code], env=self.env,
                                   cwd=PROJECT_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8")
        response = None
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 25
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with opener.open(f"http://localhost:{port}/api/v1/session", timeout=1) as result:
                        response = json.load(result)
                        break
                except OSError:
                    time.sleep(0.2)
        finally:
            stdout, stderr = process.communicate(input="\n", timeout=30)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIsNotNone(response, stdout + stderr)
        return response, stdout

    def test_fresh_supervisor_serves_uninitialized_session_and_retains_code_on_restart(self):
        response, stdout = self.supervisor_session()
        self.assertTrue(response["data"]["initialization_required"])
        self.assertFalse(response["data"]["authenticated"])
        self.assertRegex(stdout, r"Authorization code: [0-9]{8}")
        grant_id = self.scalar("SELECT id FROM runtime_enrollmentgrant WHERE active_slot='bootstrap'")
        # Keep the canonical origin fixed when restarting the real child processes.
        response, stdout = self.supervisor_session()
        self.assertTrue(response["data"]["initialization_required"])
        self.assertIn("重启后仍有效", stdout)
        self.assertNotIn("Authorization code:", stdout)
        self.assertEqual(self.scalar("SELECT id FROM runtime_enrollmentgrant WHERE active_slot='bootstrap'"), grant_id)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM auth_user"), 0)
