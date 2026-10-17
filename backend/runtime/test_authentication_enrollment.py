"""Enrollment regressions use actual WebAuthn/Ed25519 signatures, never verification stubs."""
from datetime import timedelta, datetime
import copy
import hashlib
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from cryptography.hazmat.primitives.asymmetric import ed25519

from runtime.auth_grants import get_auth_state, issue_console_grant
from runtime.models import AuthState, EnrollmentGrant, PasskeyCredential, PasskeyChallenge, FileKeyCredential
from runtime.test_authentication import AuthenticationTests, ORIGIN, PUBLIC_BACKENDS
from library.models import LibrarySettings


@override_settings(BOOKMARK_AUTH_DEBUG=False, BOOKMARK_PASSKEY_ORIGIN="", AUTHENTICATION_BACKENDS=PUBLIC_BACKENDS,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class EnrollmentTests(TestCase):
    databases = {"default", "library"}
    setUp = AuthenticationTests.setUp
    post = AuthenticationTests.post
    options = AuthenticationTests.options
    credential = AuthenticationTests.credential
    register = AuthenticationTests.register

    def grant_data(self, grant, name="新设备"):
        if "initialization_code" in grant:
            return {"bootstrap_code": grant["initialization_code"], "name": name}
        return {"enrollment_token": grant["enrollment_url"].split("=", 1)[1],
            "authorization_code": grant["authorization_code"], "name": name}

    def test_first_passkey_code_retains_existing_owner_file_key_and_library(self):
        from runtime.file_keys import register_public_key
        key = register_public_key(self.user, ed25519.Ed25519PrivateKey.generate().public_key(), "Old file key")
        _, _, fingerprint = self.preserved_library()
        before = fingerprint()
        owner_id = self.user.pk
        grant = issue_console_grant("first_passkey", ORIGIN)
        self.assertEqual(grant["enrollment_url"], ORIGIN + "/login")
        self.assertFalse(self.client.get("/api/v1/session").json()["data"]["passkey_configured"])
        options = self.post("passkey/register/options", {"name": "First passkey", "first_passkey_code": grant["authorization_code"]})
        self.assertEqual(options.status_code, 200, options.content)
        response, _ = self.finish(options.json()["data"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["registration_mode"], "first_passkey")
        self.assertEqual(get_user_model().objects.get(username="owner").pk, owner_id)
        self.assertTrue(FileKeyCredential.objects.filter(pk=key.pk).exists())
        self.assertEqual(fingerprint(), before)
        self.assertTrue(self.client.get("/api/v1/session").json()["data"]["passkey_configured"])
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)

    def test_first_passkey_code_requires_its_own_authority_and_cannot_bypass_existing_key(self):
        from library.errors import ApiError
        grant = issue_console_grant("first_passkey", ORIGIN)
        for attempt in range(5):
            response = self.post("passkey/register/options", {"first_passkey_code": "bad-code"})
            self.assertEqual(response.status_code, 429 if attempt == 4 else 403)
        self.assertEqual(self.post("passkey/register/options", {"first_passkey_code": grant["authorization_code"]}).status_code, 429)
        fresh = issue_console_grant("first_passkey", ORIGIN)
        linked = issue_console_grant("add_device", ORIGIN)
        self.assertEqual(self.post("passkey/register/options", {"first_passkey_code": linked["authorization_code"]}).status_code, 403)
        self.assertEqual(self.post("passkey/register/options", {"first_passkey_code": fresh["authorization_code"], "enrollment_token": "x"}).status_code, 400)
        self.assertEqual(self.post("passkey/register/options", {"bootstrap_code": fresh["authorization_code"]}).status_code, 409)
        options = self.post("passkey/register/options", {"first_passkey_code": fresh["authorization_code"]}).json()["data"]
        other = self.post("passkey/register/options", {"first_passkey_code": fresh["authorization_code"]}).json()["data"]
        self.assertEqual(self.finish(options)[0].status_code, 200)
        self.credential_id = secrets.token_bytes(32)
        self.assertGreaterEqual(self.finish(other)[0].status_code, 400)
        self.assertEqual(PasskeyCredential.objects.count(), 1)
        with self.assertRaises(ApiError):
            issue_console_grant("first_passkey", ORIGIN)

    def test_first_passkey_code_does_not_expire_but_challenges_and_reissued_codes_do(self):
        issued = timezone.now()
        with patch("django.utils.timezone.now", return_value=issued):
            previous = issue_console_grant("first_passkey", ORIGIN)
            fresh = issue_console_grant("first_passkey", ORIGIN)
        self.assertEqual(self.post("passkey/register/options", {"first_passkey_code": previous["authorization_code"]}).status_code, 403)
        self.assertIsNone(fresh["expires_at"])
        later = issued + timedelta(days=365)
        data = {"first_passkey_code": fresh["authorization_code"]}
        with patch("django.utils.timezone.now", return_value=later):
            options = self.post("passkey/register/options", data).json()["data"]
            self.assertEqual(datetime.fromisoformat(options["expires_at"]), later + timedelta(minutes=3))
        with patch("django.utils.timezone.now", return_value=later + timedelta(minutes=3)):
            self.assertEqual(self.finish(options)[0].status_code, 400)
            response = self.post("passkey/register/options", data)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(self.finish(response.json()["data"])[0].status_code, 200)
            self.assertGreaterEqual(self.post("passkey/register/options", data).status_code, 400)
        self.assertEqual(PasskeyCredential.objects.count(), 1)

    def test_bootstrap_code_remains_usable_until_first_registration(self):
        self.user.delete()
        issued = timezone.now()
        grant = issue_console_grant("bootstrap", ORIGIN)
        self.assertIsNone(grant["expires_at"])
        with patch("django.utils.timezone.now", return_value=issued + timedelta(days=365)):
            self.assertEqual(self.finish(self.begin(grant))[0].status_code, 200)
            self.assertEqual(self.post("passkey/register/options", self.grant_data(grant)).status_code, 409)
        self.assertEqual(PasskeyCredential.objects.count(), 1)

    def test_missing_deadline_cannot_make_device_or_reset_authority_permanent(self):
        for purpose in ("add_device", "reset"):
            with self.subTest(purpose=purpose):
                grant = issue_console_grant(purpose, ORIGIN)
                EnrollmentGrant.objects.filter(purpose=purpose).update(expires_at=None)
                self.assertEqual(self.post("passkey/register/options", self.grant_data(grant)).status_code, 403)

    def test_registration_rejects_locked_revoked_or_wrong_origin_initialization(self):
        for reason in ("locked", "revoked", "wrong_origin"):
            with self.subTest(reason=reason):
                origin = "http://localhost:18791" if reason == "wrong_origin" else ORIGIN
                grant = issue_console_grant("first_passkey", origin)
                if reason == "locked":
                    EnrollmentGrant.objects.filter(active_slot="first_passkey").update(failed_attempts=5)
                elif reason == "revoked":
                    EnrollmentGrant.objects.filter(active_slot="first_passkey").update(invalidated_at=timezone.now())
                response = self.post("passkey/register/options", {"first_passkey_code": grant["authorization_code"]})
                self.assertEqual(response.status_code, 429 if reason == "locked" else 403)

    def begin(self, grant, client=None):
        response = self.post("passkey/register/options", self.grant_data(grant), client=client)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def finish(self, options, client=None, **kwargs):
        payload = {"challenge_id": options["challenge_id"], "credential": self.credential(options, **kwargs)}
        return self.post("passkey/register/verify", payload, client=client), payload

    def change(self, method, pk, data=None, client=None):
        import json
        client = client or self.client
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        return getattr(client, method)(f"/api/v1/session/passkeys/{pk}", data=json.dumps(data or {}),
            content_type="application/json", HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN=ORIGIN)

    def preserved_library(self):
        from library.models import Artifact, Bookmark, BookmarkTag, CaptureState, Folder, Job, JobAttempt, SnapshotVersion, Tag
        from runtime.models import RuntimeState
        directory = tempfile.TemporaryDirectory(prefix="linkarium-auth-content-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        artifact_location = patch("library.artifacts.generation_root", return_value=root)
        artifact_location.start()
        self.addCleanup(artifact_location.stop)
        folder = Folder.objects.create(name="保存目录", name_key="保存目录")
        tag = Tag.objects.create(name="保留标签", name_key="保留标签")
        bookmark = Bookmark.objects.create(original_url="https://example.com/preserved", canonical_url="https://example.com/preserved",
            domain="example.com", title_override="保留收藏", notes="认证重置后保留的备注", folder=folder, is_starred=True)
        BookmarkTag.objects.create(bookmark=bookmark, tag=tag)
        content = "<!doctype html><html><body>需要保留的页面快照</body></html>".encode()
        digest = hashlib.sha256(content).hexdigest()
        relative_path = f"artifacts/{digest[:2]}/{digest}.html"
        artifact_file = root / relative_path
        artifact_file.parent.mkdir(parents=True)
        artifact_file.write_bytes(content)
        artifact = Artifact.objects.create(sha256=digest, relative_path=relative_path, byte_size=len(content))
        now = timezone.now()
        job = Job.objects.create(kind="capture", target_id=bookmark.id, variant="desktop", state="succeeded",
            available_at=now, generation="initial", library_epoch=1)
        attempt = JobAttempt.objects.create(job=job, attempt_no=1, state="succeeded", lease_expires_at=now,
            heartbeat_at=now, generation="initial", library_epoch=1)
        version = SnapshotVersion.objects.create(bookmark=bookmark, artifact=artifact, variant="desktop", capture_attempt=attempt,
            captured_at=now, source_url=bookmark.original_url, final_url=bookmark.canonical_url,
            viewport_width=1366, viewport_height=768, capture_epoch=1)
        CaptureState.objects.create(bookmark=bookmark, variant="desktop", current_version=version)
        models = (LibrarySettings, Folder, Tag, Bookmark, BookmarkTag, Artifact, SnapshotVersion, CaptureState, Job, JobAttempt, RuntimeState)
        def fingerprint():
            return ([list(model.objects.order_by("pk").values()) for model in models], hashlib.sha256(artifact_file.read_bytes()).hexdigest())
        return version, content, fingerprint

    def test_bootstrap_does_not_create_owner_until_real_registration_succeeds(self):
        self.user.delete()
        grant = issue_console_grant("bootstrap", ORIGIN)
        options = self.begin(grant)
        self.assertFalse(get_user_model().objects.exists())
        failed, _ = self.finish(options, verified=False)
        self.assertEqual(failed.status_code, 400)
        self.assertFalse(get_user_model().objects.exists())
        options = self.begin(grant)
        success, _ = self.finish(options)
        self.assertEqual(success.status_code, 200, success.content)
        owner = get_user_model().objects.get(username="owner")
        self.assertFalse(owner.has_usable_password())
        self.assertEqual(PasskeyCredential.objects.count(), 1)
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)
        self.assertEqual(self.post("passkey/register/options", self.grant_data(grant)).status_code, 409)

    def test_reissuing_bootstrap_code_replaces_only_bootstrap_authority(self):
        self.user.delete()
        old = issue_console_grant("bootstrap", ORIGIN)
        fresh = issue_console_grant("bootstrap", ORIGIN)
        self.assertEqual(EnrollmentGrant.objects.filter(active_slot="bootstrap").count(), 1)
        self.assertEqual(self.post("passkey/register/options", self.grant_data(old)).status_code, 403)
        self.assertEqual(self.post("passkey/register/options", self.grant_data(fresh)).status_code, 200)

    def test_first_passkey_reissue_excludes_previous_code_when_random_values_repeat(self):
        with patch("runtime.auth_grants.secrets.randbelow", return_value=12345678):
            previous = issue_console_grant("first_passkey", ORIGIN)
        with patch("runtime.auth_grants.secrets.randbelow", side_effect=(12345678, 87654321)):
            fresh = issue_console_grant("first_passkey", ORIGIN)
        self.assertEqual(fresh["authorization_code"], "87654321")
        self.assertEqual(self.post("passkey/register/options", {"first_passkey_code": previous["authorization_code"]}).status_code, 403)
        self.assertEqual(self.post("passkey/register/options", {"first_passkey_code": fresh["authorization_code"]}).status_code, 200)

    def test_existing_owner_without_credentials_never_reopens_bootstrap(self):
        from library.errors import ApiError
        with self.assertRaises(ApiError) as raised:
            issue_console_grant("bootstrap", ORIGIN)
        self.assertEqual(raised.exception.code, "AUTH_ALREADY_INITIALIZED")
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.post("passkey/register/options", {"bootstrap_code": "00000000"}).status_code, 409)

    def test_wrong_codes_commit_five_attempt_lock_without_consuming_grant(self):
        grant = issue_console_grant("add_device", ORIGIN)
        data = self.grant_data(grant)
        data["authorization_code"] = "not-the-code"
        for attempt in range(1, 6):
            response = self.post("passkey/register/options", data)
            self.assertEqual(response.status_code, 429 if attempt == 5 else 403)
            self.assertEqual(EnrollmentGrant.objects.get().failed_attempts, attempt)
        self.assertEqual(self.post("passkey/register/options", self.grant_data(grant)).status_code, 429)
        self.assertFalse(PasskeyChallenge.objects.exists())
        self.assertIsNone(EnrollmentGrant.objects.get().consumed_at)

    def test_links_and_codes_must_match_and_never_appear_in_persisted_rows(self):
        first = issue_console_grant("add_device", ORIGIN)
        second = issue_console_grant("add_device", ORIGIN)
        data = self.grant_data(first)
        data["authorization_code"] = second["authorization_code"]
        self.assertEqual(self.post("passkey/register/options", data).status_code, 403)
        rows = repr(list(EnrollmentGrant.objects.values()))
        for grant in (first, second):
            self.assertNotIn(grant["authorization_code"], grant["enrollment_url"])
            self.assertNotIn(grant["authorization_code"], rows)
            self.assertNotIn(grant["enrollment_url"].split("=", 1)[1], rows)
        self.assertEqual(EnrollmentGrant.objects.filter(invalidated_at=None).count(), 2)

    def test_five_minute_deadline_is_absolute_including_final_verify(self):
        issued = timezone.now()
        with patch("django.utils.timezone.now", return_value=issued):
            grant = issue_console_grant("add_device", ORIGIN)
        self.assertEqual(datetime.fromisoformat(grant["expires_at"]), issued + timedelta(minutes=5))
        with patch("django.utils.timezone.now", return_value=issued + timedelta(minutes=4, seconds=59)):
            options = self.begin(grant)
        self.assertEqual(options["expires_at"], grant["expires_at"])
        with patch("django.utils.timezone.now", return_value=issued + timedelta(minutes=5)):
            self.assertEqual(self.finish(options)[0].status_code, 400)
            self.assertEqual(self.post("passkey/register/options", self.grant_data(grant)).status_code, 410)
        self.assertFalse(PasskeyCredential.objects.exists())

    def test_validated_code_does_not_create_collection_session(self):
        self.begin(issue_console_grant("add_device", ORIGIN))
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)

    def test_device_grant_requires_credential_session_and_explicit_input_cannot_fallback(self):
        self.assertEqual(self.post("passkey/grants", {}).status_code, 401)
        self.register()
        response = self.post("passkey/grants", {})
        self.assertEqual(response.status_code, 201, response.content)
        grant = response.json()["data"]
        row = EnrollmentGrant.objects.get(consumed_at=None)
        self.assertEqual(str(row.issuer_credential_id), self.client.session["credential_id"])
        self.assertEqual(self.post("passkey/register/options", {"enrollment_token": "invalid"}).status_code, 403)
        self.assertEqual(self.post("passkey/register/options", {"setup_token": "a" * 40}).status_code, 403)
        self.assertEqual(self.post("passkey/register/options", {**self.grant_data(grant), "bootstrap_code": "0"}).status_code, 400)

    def test_new_device_registers_and_grant_cannot_register_twice(self):
        self.register()
        grant = self.post("passkey/grants", {"purpose": "reset"}).json()["data"]
        new_client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        options = self.begin(grant, new_client)
        self.credential_id = secrets.token_bytes(32)
        response, payload = self.finish(options, new_client)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["registration_mode"], "add_device")
        self.assertEqual(new_client.get("/api/v1/bookmarks").status_code, 200)
        self.assertEqual(self.post("passkey/register/options", self.grant_data(grant)).status_code, 409)
        replay = self.post("passkey/register/verify", payload, new_client)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertTrue(replay.json()["data"]["already_completed"])
        self.assertEqual(replay.json()["data"]["registration_mode"], "add_device")
        self.assertEqual(PasskeyCredential.objects.count(), 2)

    def test_lost_cookie_response_returns_receipt_without_a_new_login(self):
        grant = issue_console_grant("add_device", ORIGIN)
        options = self.begin(grant)
        old_cookies = copy.deepcopy(self.client.cookies)
        response, payload = self.finish(options)
        self.assertEqual(response.status_code, 200)
        lost = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        lost.cookies = old_cookies
        replay = self.post("passkey/register/verify", payload, lost)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["data"], {"registered": True, "authenticated": False,
            "credential_id": str(PasskeyCredential.objects.get().pk), "already_completed": True,
            "registration_mode": "add_device"})
        self.assertEqual(lost.get("/api/v1/bookmarks").status_code, 401)
        self.assertEqual(PasskeyCredential.objects.count(), 1)

    def test_completion_receipt_rejects_different_payload_and_never_authenticates_another_browser(self):
        options = self.options()
        _, payload = self.finish(options)
        unrelated = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        receipt = self.post("passkey/register/verify", payload, unrelated)
        self.assertEqual(receipt.status_code, 200)
        self.assertFalse(receipt.json()["data"]["authenticated"])
        self.assertEqual(unrelated.get("/api/v1/bookmarks").status_code, 401)
        payload["credential"]["clientExtensionResults"] = {"different": True}
        self.assertEqual(self.post("passkey/register/verify", payload).status_code, 400)

    def test_processing_interruption_can_restart_without_consuming_grant(self):
        grant = issue_console_grant("add_device", ORIGIN)
        options = self.begin(grant)
        PasskeyChallenge.objects.filter(pk=options["challenge_id"]).update(state="processing")
        self.assertEqual(self.finish(options)[0].status_code, 409)
        fresh = self.begin(grant)
        self.assertEqual(self.finish(fresh)[0].status_code, 200)

    def test_rename_delete_current_and_other_sessions_observe_credential_revocation(self):
        self.register()
        first = PasskeyCredential.objects.get()
        same = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        same.cookies = copy.deepcopy(self.client.cookies)
        self.credential_id = secrets.token_bytes(32)
        added = self.post("passkey/register/options", {"name": "第二把"}).json()["data"]
        self.assertEqual(self.finish(added)[0].status_code, 200)
        self.assertEqual(self.change("patch", first.pk, {"name": "办公电脑"}).status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.name, "办公电脑")
        response = self.change("delete", first.pk)
        self.assertEqual(response.json()["data"], {"deleted": True, "logged_out": True})
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)
        self.assertEqual(same.get("/api/v1/bookmarks").status_code, 401)
        self.assertEqual(same.get("/api/v1/snapshots/11111111-1111-1111-1111-111111111111/preview").status_code, 401)

    def test_cannot_delete_last_usable_credential_even_with_wrong_domain_credential(self):
        self.register()
        first = PasskeyCredential.objects.get()
        PasskeyCredential.objects.create(user=self.user, credential_id="unusable", public_key=b"public",
            rp_id="other.example", name="其他站点")
        self.assertEqual(self.change("delete", first.pk).status_code, 409)
        raw = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, fingerprint=hashlib.sha256(raw).hexdigest(), public_key=raw)
        self.assertEqual(self.change("delete", first.pk).status_code, 200)

    def test_issuer_deletion_revokes_grants_and_already_issued_registration_challenges(self):
        self.register()
        first = PasskeyCredential.objects.get()
        grant = self.post("passkey/grants", {}).json()["data"]
        new_client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        options = self.begin(grant, new_client)
        raw = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, fingerprint=hashlib.sha256(raw).hexdigest(), public_key=raw)
        self.assertEqual(self.change("delete", first.pk).status_code, 200)
        self.credential_id = secrets.token_bytes(32)
        self.assertEqual(self.finish(options, new_client)[0].status_code, 403)
        self.assertEqual(self.post("passkey/register/options", self.grant_data(grant), new_client).status_code, 403)

    def test_session_deadline_stays_fixed_after_other_writes_and_expires_at_fourteen_days(self):
        now = timezone.now()
        with patch("django.utils.timezone.now", return_value=now):
            self.register()
        deadline = datetime.fromisoformat(self.client.session["auth_expires_at"])
        self.assertEqual(deadline, now + timedelta(days=14))
        with patch("django.utils.timezone.now", return_value=deadline - timedelta(seconds=1)):
            self.assertEqual(self.post("passkey/grants", {}).status_code, 201)
            self.assertEqual(datetime.fromisoformat(self.client.session["auth_expires_at"]), deadline)
            self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)
        with patch("django.utils.timezone.now", return_value=deadline):
            self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)

    def test_old_session_without_version_must_log_in_again(self):
        self.register()
        session = self.client.session
        del session["auth_version"]
        session.save()
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)

    def test_reset_commits_all_revocations_only_after_valid_passkey_and_preserves_library(self):
        old_options = self.register()
        old_payload = {"challenge_id": old_options["challenge_id"], "credential": self.credential(old_options)}
        old_credential = PasskeyCredential.objects.get().pk
        old_session_key = self.client.session.session_key
        stale_session = dict(self.client.session)
        old_client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        old_client.cookies = copy.deepcopy(self.client.cookies)
        snapshot, snapshot_bytes, library_fingerprint = self.preserved_library()
        before_library = library_fingerprint()
        before_preview = old_client.get(f"/api/v1/snapshots/{snapshot.pk}/preview")
        self.assertEqual(before_preview.status_code, 200)
        self.assertEqual(b"".join(before_preview.streaming_content), snapshot_bytes)
        before_preview.close()
        raw = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, fingerprint=hashlib.sha256(raw).hexdigest(), public_key=raw)
        device_grant = self.post("passkey/grants", {}).json()["data"]
        reset_grant = issue_console_grant("reset", ORIGIN)
        reset_client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        options = self.begin(reset_grant, reset_client)
        self.credential_id = secrets.token_bytes(32)
        self.assertEqual(self.finish(options, reset_client, verified=False)[0].status_code, 400)
        self.assertTrue(PasskeyCredential.objects.filter(pk=old_credential).exists())
        self.assertEqual(old_client.get("/api/v1/bookmarks").status_code, 200)
        options = self.begin(reset_grant, reset_client)
        old_cookies = copy.deepcopy(reset_client.cookies)
        response, payload = self.finish(options, reset_client)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["registration_mode"], "reset")
        self.assertEqual(PasskeyCredential.objects.count(), 1)
        self.assertFalse(PasskeyCredential.objects.filter(pk=old_credential).exists())
        self.assertFalse(FileKeyCredential.objects.exists())
        self.assertFalse(Session.objects.filter(pk=old_session_key).exists())
        self.assertEqual(old_client.get("/api/v1/bookmarks").status_code, 401)
        self.assertEqual(reset_client.get("/api/v1/bookmarks").status_code, 200)
        self.assertEqual(library_fingerprint(), before_library)
        self.assertEqual(old_client.get(f"/api/v1/snapshots/{snapshot.pk}/preview").status_code, 401)
        after_preview = reset_client.get(f"/api/v1/snapshots/{snapshot.pk}/preview")
        self.assertEqual(after_preview.status_code, 200)
        self.assertEqual(b"".join(after_preview.streaming_content), snapshot_bytes)
        after_preview.close()
        self.assertEqual(self.post("passkey/register/verify", old_payload).status_code, 400)
        self.assertEqual(self.post("passkey/register/options", self.grant_data(device_grant)).status_code, 403)
        # Simulate an old request writing its session back after the reset transaction.
        restored = SessionStore()
        restored.update(stale_session)
        restored.save()
        old_client.cookies[settings.SESSION_COOKIE_NAME] = restored.session_key
        self.assertEqual(old_client.get("/api/v1/bookmarks").status_code, 401)
        reset_client.cookies = old_cookies
        replay = self.post("passkey/register/verify", payload, reset_client)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertFalse(replay.json()["data"]["authenticated"])

    def test_ip_passkey_origin_is_unavailable_and_localhost_is_suggested(self):
        client = Client(HTTP_HOST="127.0.0.1:8000")
        state = client.get("/api/v1/session").json()["data"]
        self.assertEqual(state["passkey_origin"], "http://localhost:8000")
        self.assertFalse(state["passkey_available"])

    @override_settings(BOOKMARK_PASSKEY_ORIGIN="http://localhost:8000")
    def test_file_key_retains_same_port_loopback_alias_with_explicit_origin(self):
        from webauthn.helpers import bytes_to_base64url
        private = ed25519.Ed25519PrivateKey.generate()
        raw = private.public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, fingerprint=hashlib.sha256(raw).hexdigest(), public_key=raw)
        client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1:8000")
        options_response = self.post("key/options", {}, client, origin="http://127.0.0.1:8000")
        self.assertEqual(options_response.status_code, 200, options_response.content)
        options = options_response.json()["data"]
        result = self.post("key/verify", {"challenge_id": options["challenge_id"],
            "signature": bytes_to_base64url(private.sign(options["message"].encode()))}, client, origin="http://127.0.0.1:8000")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.post("passkey/register/options", {}, client, origin="http://127.0.0.1:8000").status_code, 400)
        self.assertEqual(self.post("key/options", {}, client, origin="http://localhost:8000").status_code, 403)
        with override_settings(BOOKMARK_PASSKEY_ORIGIN="https://bookmarks.example"):
            self.assertEqual(self.post("key/options", {}, client, origin="http://127.0.0.1:8000").status_code, 400)
