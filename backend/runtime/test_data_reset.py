from runtime.testing import force_credential_login
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from library.models import AIProfile, Artifact, Bookmark, Folder, Job, LibrarySettings, Tag
from runtime.data_reset import create_data_operation, process_data_operation
from runtime.maintenance import process_operation
from runtime.models import MaintenanceOperation, ProviderSecret, RuntimeState


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DataOperationApiTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        self.owner = get_user_model().objects.create_user("owner", password="test-password")
        force_credential_login(self.client, self.owner)

    def post(self, kind, **fields):
        return self.client.post(f"/api/v1/data/{kind}", json.dumps({"library_epoch": 1, **fields}),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY="test-" + kind)

    def test_confirmation_and_authentication_are_required(self):
        self.assertEqual(self.post("clear", confirmation="重置数据").status_code, 400)
        self.client.logout()
        self.assertEqual(self.post("clear", confirmation="清空仓库").status_code, 401)
        self.assertFalse(MaintenanceOperation.objects.exists())

    def test_csrf_is_required_even_with_a_valid_owner_session(self):
        csrf = Client(enforce_csrf_checks=True)
        force_credential_login(csrf, self.owner)
        response = csrf.post("/api/v1/data/reset", json.dumps({"confirmation": "重置数据", "library_epoch": 1}),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY="csrf-reset")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MaintenanceOperation.objects.exists())

    def test_registration_is_idempotent_and_immediately_fences_writes(self):
        first = self.post("clear", confirmation="清空仓库")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.post("clear", confirmation="清空仓库").json(), first.json())
        self.assertEqual(MaintenanceOperation.objects.count(), 1)
        self.assertTrue(RuntimeState.objects.get(pk=1).maintenance_mode)
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 503)
        self.assertEqual(self.post("reset", confirmation="重置数据").status_code, 409)
        response = self.client.post("/api/v1/backups", "{}", content_type="application/json", HTTP_IDEMPOTENCY_KEY="backup")
        self.assertEqual(response.status_code, 409)

    def test_stale_library_and_existing_maintenance_are_rejected(self):
        self.assertEqual(self.post("clear", confirmation="清空仓库", library_epoch=0).status_code, 409)
        MaintenanceOperation.objects.create(kind="restore", state="queued")
        self.assertEqual(self.post("clear", confirmation="清空仓库").status_code, 409)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DataErasureTests(TransactionTestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        profile = AIProfile.objects.create(name="preserved-profile", base_url="https://ai.example/v1", model="test-model")
        LibrarySettings.objects.create(pk=1, default_refresh_seconds=86400, active_ai_profile_id=profile.id,
            ui_defaults={"sort": "title"})
        folder = Folder.objects.create(name="private-folder", name_key="private-folder")
        self.bookmark = Bookmark.objects.create(original_url="https://private.example/", canonical_url="https://private.example/",
            domain="private.example", folder=folder, notes="private-note")
        Tag.objects.create(name="private-tag", name_key="private-tag")
        Job.objects.create(kind="capture", target_id=self.bookmark.id, available_at=timezone.now(), generation="initial", library_epoch=1)
        Artifact.objects.create(sha256="a" * 64, relative_path="artifacts/aa/" + "a" * 64 + ".html", byte_size=7)
        self.profile = profile
        self.owner = get_user_model().objects.create_user("owner", password="test-password")
        force_credential_login(self.client, self.owner)
        ProviderSecret.objects.create(profile_id=profile.id, api_key="private-test-api-key", bound_endpoint=profile.base_url,
            endpoint_fingerprint="test-fingerprint", verified=True, verified_library_epoch=1)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = patch("config.paths.DATA_ROOT", self.root)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.pointer = patch("config.paths.current_generation", side_effect=lambda: RuntimeState.objects.get(pk=1).active_generation)
        self.pointer.start()
        self.addCleanup(self.pointer.stop)
        self.generation = self.root / "generations" / "initial"
        self.generation.mkdir(parents=True)
        with closing(sqlite3.connect(self.generation / "library.sqlite3")) as database:
            connections["library"].connection.backup(database)
        for path in [self.generation / "artifacts" / "private.html", self.root / "backups" / "private.zip",
                self.root / "work" / "capture" / "private.html", self.root / "generations" / "old-restore" / "private.html"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private", encoding="utf-8")
        (self.root / "runtime").mkdir()
        (self.root / "runtime" / "secret-key").write_text("old-signing-key", encoding="utf-8")
        self.key = override_settings(SECRET_KEY="old-signing-key")
        self.key.enable()
        self.addCleanup(self.key.disable)

    def run_operation(self, kind, ready=lambda: True):
        operation = create_data_operation(kind, {"confirmation": "清空仓库" if kind == "clear" else "重置数据", "library_epoch": 1})
        result = process_operation(operation.id, stop_services=lambda: None, start_services=lambda: None, ready_services=ready)
        operation.refresh_from_db()
        return result, operation

    def candidate(self, operation):
        return sqlite3.connect(self.root / "generations" / operation.target_generation / "library.sqlite3")

    def assert_website_data_erased(self, operation):
        with closing(self.candidate(operation)) as database:
            for table in ["library_bookmark", "library_folder", "library_tag", "library_artifact", "library_job"]:
                self.assertEqual(database.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertIsNone(database.execute("PRAGMA foreign_key_check").fetchone())
        self.assertFalse(self.generation.exists())
        self.assertEqual([p.name for p in (self.root / "generations").iterdir()], [operation.target_generation])
        self.assertFalse(list((self.root / "backups").iterdir()))
        self.assertFalse((self.root / "work" / "capture" / "private.html").exists())

    def test_clear_removes_website_data_and_files_but_preserves_credentials_and_settings(self):
        result, operation = self.run_operation("clear")
        self.assertTrue(result)
        self.assert_website_data_erased(operation)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("test-password"))
        self.assertTrue(Session.objects.exists())
        secret = ProviderSecret.objects.get(pk=self.profile.id)
        self.assertEqual(secret.api_key, "private-test-api-key")
        self.assertEqual(secret.verified_library_epoch, 2)
        self.assertTrue(secret.verified)
        with closing(self.candidate(operation)) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM library_aiprofile").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT default_refresh_seconds,revision,tree_revision FROM library_librarysettings").fetchone(), (86400, 2, 2))
        self.assertFalse(RuntimeState.objects.get(pk=1).maintenance_mode)

    def test_reset_also_erases_all_users_sessions_secrets_and_preferences(self):
        from runtime.auth_grants import get_auth_state, issue_console_grant
        from runtime.models import AuthState, EnrollmentGrant, FileKeyCredential, PasskeyChallenge
        auth_before = get_auth_state()
        issue_console_grant("reset", "http://localhost:8000")
        result, operation = self.run_operation("reset")
        self.assertTrue(result)
        self.assert_website_data_erased(operation)
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(Session.objects.exists())
        self.assertFalse(ProviderSecret.objects.exists())
        self.assertFalse(FileKeyCredential.objects.exists())
        self.assertFalse(EnrollmentGrant.objects.exists())
        self.assertFalse(PasskeyChallenge.objects.exists())
        auth_after = AuthState.objects.get(pk=1)
        self.assertGreater(auth_after.credential_epoch, auth_before.credential_epoch)
        self.assertNotEqual(bytes(auth_after.webauthn_user_handle), bytes(auth_before.webauthn_user_handle))
        with closing(self.candidate(operation)) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM library_aiprofile").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT default_refresh_seconds,ui_defaults,active_ai_profile_id FROM library_librarysettings").fetchone(), (604800, "{}", None))
        self.assertNotEqual((self.root / "runtime" / "secret-key").read_text(), "old-signing-key")
        session = self.client.get("/api/v1/session").json()["data"]
        self.assertFalse(session["authenticated"])
        self.assertTrue(session["initialization_required"])
        self.assertEqual(self.client.get("/api/v1/runtime/status").status_code, 401)

    def test_failed_candidate_start_preserves_original_user_data_and_files(self):
        answers = iter([False, True])
        result, operation = self.run_operation("reset", ready=lambda: next(answers))
        self.assertFalse(result)
        self.assertEqual(operation.state, "failed")
        self.assertEqual(RuntimeState.objects.get(pk=1).active_generation, "initial")
        self.assertFalse(RuntimeState.objects.get(pk=1).maintenance_mode)
        self.assertTrue(self.generation.exists())
        self.assertTrue((self.root / "backups" / "private.zip").exists())
        self.assertTrue(get_user_model().objects.exists())
        self.assertTrue(ProviderSecret.objects.exists())

    def test_interrupted_erasure_resumes_without_restoring_deleted_data(self):
        with patch("runtime.data_reset._erase_runtime_data", side_effect=OSError("test interruption")):
            result, operation = self.run_operation("reset")
        self.assertFalse(result)
        self.assertEqual(operation.state, "running")
        self.assertTrue(RuntimeState.objects.get(pk=1).maintenance_mode)
        self.assertFalse(self.generation.exists())
        self.assertTrue(process_data_operation(operation, stop_services=lambda: None,
            start_services=lambda: None, ready_services=lambda: True))
        operation.refresh_from_db()
        self.assertEqual(operation.state, "succeeded")
        self.assertEqual(operation.error_code, "")
        self.assertFalse(get_user_model().objects.exists())
        self.assert_website_data_erased(operation)

    def test_supervisor_resumes_confirmation_registered_before_shutdown(self):
        from supervisor import Supervisor
        operation = create_data_operation("clear", {"confirmation": "清空仓库", "library_epoch": 1})
        supervisor = Supervisor()
        with patch.object(supervisor, "stop_services"), patch.object(supervisor, "start_services"), patch.object(supervisor, "ready_services", return_value=True):
            supervisor.recover_interrupted()
        operation.refresh_from_db()
        self.assertEqual(operation.state, "succeeded")
        self.assertFalse(RuntimeState.objects.get(pk=1).maintenance_mode)
        self.assert_website_data_erased(operation)

    def test_cleanup_rejects_external_paths(self):
        from runtime.data_reset import _remove_managed_path
        outside = Path(tempfile.mkdtemp())
        try:
            private = outside / "preserved.txt"
            private.write_text("preserved")
            from runtime.maintenance import MaintenanceError
            with self.assertRaises(MaintenanceError):
                _remove_managed_path(outside)
            self.assertTrue(private.exists())
        finally:
            import shutil
            shutil.rmtree(outside)
