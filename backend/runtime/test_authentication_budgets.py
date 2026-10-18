"""Anonymous ceremonies have bounded shared state and never allocate login sessions."""
from datetime import timedelta
import hashlib
import secrets
import time
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ed25519
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from webauthn.helpers import bytes_to_base64url

from runtime.auth_challenges import NONCE_COOKIE, NONCE_SECONDS, cleanup_authentication_state
from runtime.auth_grants import issue_console_grant
from runtime.models import AuthRequestBucket, EnrollmentGrant, FileKeyCredential, PasskeyChallenge
from runtime.test_authentication import AuthenticationTests, ORIGIN, PUBLIC_BACKENDS


@override_settings(BOOKMARK_AUTH_DEBUG=False, BOOKMARK_PASSKEY_ORIGIN="", AUTHENTICATION_BACKENDS=PUBLIC_BACKENDS,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthenticationBudgetTests(TestCase):
    databases = {"default", "library"}
    setUp = AuthenticationTests.setUp
    post = AuthenticationTests.post
    options = AuthenticationTests.options
    credential = AuthenticationTests.credential
    register = AuthenticationTests.register

    def key(self):
        key = ed25519.Ed25519PrivateKey.generate()
        raw = key.public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, public_key=raw, fingerprint=hashlib.sha256(raw).hexdigest())
        return key

    def test_cookieless_options_flood_never_creates_durable_sessions(self):
        self.key()
        for _ in range(12):
            client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
            response = self.post("key/options", {}, client=client)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
            cookie = response.cookies[NONCE_COOKIE]
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Strict")
            self.assertEqual(int(cookie["max-age"]), NONCE_SECONDS)
        self.assertEqual(Session.objects.count(), 0)
        self.assertEqual(PasskeyChallenge.objects.count(), 12)

    def test_wrong_authorization_or_missing_verification_does_not_create_cookies_or_sessions(self):
        response = self.post("passkey/register/options", {"first_passkey_code": "00000000"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(NONCE_COOKIE, response.cookies)
        response = self.post("key/verify", {"challenge_id": str(secrets.token_hex(16)), "signature": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(NONCE_COOKIE, response.cookies)
        self.assertFalse(Session.objects.exists())

    def test_new_options_renew_nonce_lifetime_without_invalidating_other_pending_challenges(self):
        key = self.key()
        issued = time.time()
        first = self.post("key/options", {}).json()["data"]
        first_binding = PasskeyChallenge.objects.get(pk=first["challenge_id"]).session_hash
        with patch("django.core.signing.time.time", return_value=issued + NONCE_SECONDS - 60):
            second = self.post("key/options", {}).json()["data"]
        self.assertEqual(PasskeyChallenge.objects.get(pk=second["challenge_id"]).session_hash, first_binding)
        payload = {"challenge_id": first["challenge_id"], "signature": bytes_to_base64url(key.sign(first["message"].encode()))}
        with patch("django.core.signing.time.time", return_value=issued + NONCE_SECONDS + 60):
            self.assertEqual(self.post("key/verify", payload).status_code, 200)

    def test_known_initialization_link_keeps_its_own_five_guess_limit(self):
        grant = issue_console_grant("first_passkey", ORIGIN)
        payload = {"enrollment_token": grant["enrollment_url"].split("=", 1)[1], "authorization_code": "wrong"}
        for attempt in range(5):
            self.assertEqual(self.post("passkey/register/options", payload).status_code, 429 if attempt == 4 else 403)
        row = EnrollmentGrant.objects.get(active_slot="first_passkey")
        self.assertEqual(row.failed_attempts, 5)
        payload["authorization_code"] = grant["authorization_code"]
        self.assertEqual(self.post("passkey/register/options", payload).status_code, 429)
        self.assertFalse(Session.objects.exists())

    def test_file_key_requires_untampered_unexpired_same_browser_nonce_and_real_signature(self):
        key = self.key()
        response = self.post("key/options", {})
        options = response.json()["data"]
        original = self.client.cookies[NONCE_COOKIE].value
        payload = {"challenge_id": options["challenge_id"], "signature": bytes_to_base64url(key.sign(options["message"].encode()))}
        other = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        self.assertEqual(self.post("key/verify", payload, client=other).status_code, 400)
        self.client.cookies[NONCE_COOKIE] = original + "modified"
        self.assertEqual(self.post("key/verify", payload).status_code, 400)
        self.client.cookies[NONCE_COOKIE] = original
        with patch("django.core.signing.time.time", return_value=time.time() + NONCE_SECONDS + 1):
            self.assertEqual(self.post("key/verify", payload).status_code, 400)
        self.assertFalse(Session.objects.exists())
        self.assertTrue(PasskeyChallenge.objects.filter(pk=options["challenge_id"]).exists())
        self.assertEqual(self.post("key/verify", payload).status_code, 200)
        self.assertEqual(Session.objects.count(), 1)

    def test_source_budget_covers_discarded_cookies_and_preserves_valid_grant(self):
        grant = issue_console_grant("first_passkey", ORIGIN)
        with patch("runtime.auth_challenges.SOURCE_OPTIONS_PER_MINUTE", 3):
            for _ in range(3):
                client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
                self.assertEqual(self.post("passkey/register/options", {"enrollment_token": secrets.token_urlsafe(32),
                    "authorization_code": "wrong"}, client=client).status_code, 403)
            response = self.post("passkey/register/options", {"enrollment_token": grant["enrollment_url"].split("=", 1)[1],
                "authorization_code": grant["authorization_code"]})
            self.assertEqual(response.status_code, 429)
        self.assertEqual(EnrollmentGrant.objects.get().failed_attempts, 0)
        self.assertFalse(Session.objects.exists())

    def test_global_budget_is_shared_between_sources(self):
        self.key()
        with patch("runtime.auth_challenges.GLOBAL_OPTIONS_PER_MINUTE", 2):
            for index in range(3):
                client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost", REMOTE_ADDR=f"192.0.2.{index}")
                self.assertEqual(self.post("key/options", {}, client=client).status_code, 200 if index < 2 else 429)
        self.assertEqual(PasskeyChallenge.objects.count(), 2)

    def test_counter_table_is_bounded_and_expired_sources_are_reclaimed(self):
        self.key()
        now = timezone.now()
        with patch("django.utils.timezone.now", return_value=now), patch("runtime.auth_challenges.SOURCE_BUCKET_LIMIT", 2):
            for index in range(3):
                client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost", REMOTE_ADDR=f"192.0.2.{index}")
                self.assertEqual(self.post("key/options", {}, client=client).status_code, 200 if index < 2 else 429)
            self.assertEqual(AuthRequestBucket.objects.count(), 3)  # Two sources and one global counter.
        with patch("django.utils.timezone.now", return_value=now + timedelta(minutes=2)):
            self.assertEqual(self.post("key/options", {}).status_code, 200)
        self.assertEqual(AuthRequestBucket.objects.count(), 2)

    def test_challenge_budgets_preserve_existing_work_and_recover_after_expiry(self):
        self.key()
        with patch("runtime.auth_challenges.BROWSER_PENDING_LIMIT", 2):
            for index in range(3):
                self.assertEqual(self.post("key/options", {}).status_code, 200 if index < 2 else 429)
        other = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        with patch("runtime.auth_challenges.CHALLENGE_LIMIT", 2):
            self.assertEqual(self.post("key/options", {}, client=other).status_code, 429)
            self.assertEqual(PasskeyChallenge.objects.count(), 2)
            PasskeyChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(self.post("key/options", {}, client=other).status_code, 200)
        self.assertEqual(PasskeyChallenge.objects.count(), 1)

    def test_idle_housekeeping_cleans_expired_rows_without_deleting_active_sessions(self):
        self.key()
        self.post("key/options", {})
        PasskeyChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        expired, current = SessionStore(), SessionStore()
        expired["_auth_user_id"], current["_auth_user_id"] = "1", "1"
        expired.save()
        current.save()
        Session.objects.filter(session_key=expired.session_key).update(expire_date=timezone.now() - timedelta(seconds=1))
        AuthRequestBucket.objects.update(window_start=timezone.now() - timedelta(minutes=2))
        cleanup_authentication_state()
        self.assertEqual(list(Session.objects.values_list("session_key", flat=True)), [current.session_key])
        self.assertFalse(PasskeyChallenge.objects.exists())
        self.assertFalse(AuthRequestBucket.objects.exists())

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_https_nonce_cookie_requires_secure_transport(self):
        self.key()
        self.assertTrue(self.post("key/options", {}).cookies[NONCE_COOKIE]["secure"])
