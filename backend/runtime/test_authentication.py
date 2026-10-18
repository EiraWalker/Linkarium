import hashlib
import json
import secrets
import tempfile
from pathlib import Path
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from django.contrib.auth import get_user_model
from django.contrib.auth import password_validation
from django.contrib.sessions.models import Session
from django.core.management import call_command, CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from webauthn.helpers import bytes_to_base64url
from library.models import LibrarySettings
from runtime.auth_grants import get_auth_state, issue_console_grant
from runtime.models import RuntimeState, PasskeyCredential, PasskeySetupToken, PasskeyChallenge, FileKeyCredential

PUBLIC_BACKENDS = ["axes.backends.AxesStandaloneBackend", "runtime.authentication.PublicKeyBackend"]
DEBUG_BACKENDS = ["axes.backends.AxesStandaloneBackend", "django.contrib.auth.backends.ModelBackend", "runtime.authentication.PublicKeyBackend"]
ORIGIN = "http://localhost"


@override_settings(BOOKMARK_AUTH_DEBUG=False, BOOKMARK_PASSKEY_ORIGIN="", AUTHENTICATION_BACKENDS=PUBLIC_BACKENDS,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthenticationTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        self.user = get_user_model().objects.create_user("owner", password="464194")
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)

    def post(self, path, data, client=None, origin=ORIGIN):
        client = client or self.client
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        return client.post("/api/v1/session/" + path, data=json.dumps(data), content_type="application/json",
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN=origin)

    def options(self, purpose="register", token=None):
        if purpose == "register" and token is None:
            token = issue_console_grant("add_device", ORIGIN)
        data = {"enrollment_token": token["enrollment_url"].split("=", 1)[1],
            "authorization_code": token["authorization_code"], "name": "Passkey"} if purpose == "register" else {}
        response = self.post(f"passkey/{purpose}/options", data)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def credential(self, options, purpose="register", origin=ORIGIN, verified=True, count=1):
        client_data = json.dumps({"type": "webauthn.create" if purpose == "register" else "webauthn.get",
            "challenge": options["options"]["challenge"], "origin": origin, "crossOrigin": False}).encode()
        flags = 1 | (4 if verified else 0) | (64 if purpose == "register" else 0)
        auth_data = hashlib.sha256(b"localhost").digest() + bytes([flags]) + count.to_bytes(4, "big")
        result = {"id": bytes_to_base64url(self.credential_id), "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key", "clientExtensionResults": {}}
        response = {"clientDataJSON": bytes_to_base64url(client_data)}
        if purpose == "register":
            public = self.private.public_key().public_numbers()
            cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: public.x.to_bytes(32, "big"), -3: public.y.to_bytes(32, "big")})
            auth_data += bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose
            response["attestationObject"] = bytes_to_base64url(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data}))
            response["transports"] = ["internal"]
        else:
            response["authenticatorData"] = bytes_to_base64url(auth_data)
            response["signature"] = bytes_to_base64url(self.private.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())))
            response["userHandle"] = bytes_to_base64url(bytes(get_auth_state().webauthn_user_handle))
        result["response"] = response
        return result

    def register(self):
        options = self.options()
        response = self.post("passkey/register/verify", {"challenge_id": options["challenge_id"], "credential": self.credential(options)})
        self.assertEqual(response.status_code, 200, response.content)
        return options

    def test_normal_mode_rejects_password_and_has_no_password_backend(self):
        response = self.post("login", {"password": "464194"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PASSWORD_LOGIN_DISABLED")
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)
        self.assertFalse(self.client.get("/api/v1/session").json()["data"]["password_login_enabled"])

    def test_real_passkey_registration_and_login_counter_replay_protection(self):
        self.register()
        self.assertEqual(PasskeyCredential.objects.count(), 1)
        self.assertFalse(PasskeySetupToken.objects.exists())
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)
        self.post("logout", {})
        options = self.options("login")
        data = {"challenge_id": options["challenge_id"], "credential": self.credential(options, "login", count=2)}
        response = self.post("passkey/login/verify", data)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(PasskeyCredential.objects.get().sign_count, 2)
        self.assertEqual(self.post("passkey/login/verify", data).status_code, 400)

    def test_saved_transports_are_retained_in_registration_and_login_options(self):
        self.register()
        stored = PasskeyCredential.objects.get()
        for transports in (["internal"], ["usb", "nfc"], ["internal", "hybrid"], []):
            stored.transports = transports
            stored.save(update_fields=["transports"])
            for purpose, field in (("register", "excludeCredentials"), ("login", "allowCredentials")):
                with self.subTest(transports=transports, purpose=purpose):
                    descriptor = self.options(purpose)["options"][field][0]
                    self.assertEqual(descriptor["id"], stored.credential_id)
                    if transports:
                        self.assertEqual(descriptor.get("transports"), transports)
                    else:
                        self.assertNotIn("transports", descriptor)

    def test_registration_requires_local_admin_grant(self):
        self.assertEqual(self.post("passkey/register/options", {}).status_code, 403)
        self.assertFalse(PasskeyChallenge.objects.filter(state="pending").exists())

    def test_origin_and_user_verification_are_enforced(self):
        for origin, verified in (("https://evil.example", True), (ORIGIN, False)):
            options = self.options()
            response = self.post("passkey/register/verify", {"challenge_id": options["challenge_id"],
                "credential": self.credential(options, origin=origin, verified=verified)})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.exists())
        self.assertEqual(self.post("passkey/register/options", {}, origin="https://evil.example").status_code, 403)

    def test_challenge_is_bound_to_session_and_expires(self):
        options = self.options()
        data = {"challenge_id": options["challenge_id"], "credential": self.credential(options)}
        other = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        self.assertEqual(self.post("passkey/register/verify", data, client=other).status_code, 400)
        PasskeyChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post("passkey/register/verify", data).status_code, 400)

    def test_bad_signature_is_rejected_and_consumed(self):
        self.register()
        options = self.options("login")
        credential = self.credential(options, "login", count=2)
        credential["response"]["signature"] = bytes_to_base64url(b"invalid")
        data = {"challenge_id": options["challenge_id"], "credential": credential}
        self.assertEqual(self.post("passkey/login/verify", data).status_code, 400)
        self.assertFalse(PasskeyChallenge.objects.filter(purpose="login").exists())

    def test_password_session_does_not_survive_switch_to_normal_mode(self):
        with override_settings(BOOKMARK_AUTH_DEBUG=True, AUTHENTICATION_BACKENDS=DEBUG_BACKENDS):
            self.assertEqual(self.post("login", {"password": "464194"}).status_code, 200)
            self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 401)

    def test_debug_allows_password_and_passkey(self):
        with override_settings(BOOKMARK_AUTH_DEBUG=True, AUTHENTICATION_BACKENDS=DEBUG_BACKENDS):
            self.assertTrue(self.client.get("/api/v1/session").json()["data"]["password_login_enabled"])
            self.assertEqual(self.post("login", {"password": "464194"}).status_code, 200)
            self.post("logout", {})
            self.register()

    def test_csrf_required_for_key_options(self):
        self.assertEqual(self.client.post("/api/v1/session/key/options", data="{}", content_type="application/json", HTTP_ORIGIN=ORIGIN).status_code, 403)

    def test_real_file_key_signature_login_and_replay_rejected(self):
        private = ed25519.Ed25519PrivateKey.generate()
        raw = private.public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, fingerprint=hashlib.sha256(raw).hexdigest(), public_key=raw)
        for debug in (False, True):
            with override_settings(BOOKMARK_AUTH_DEBUG=debug):
                options = self.post("key/options", {}).json()["data"]
                data = {"challenge_id": options["challenge_id"], "signature": bytes_to_base64url(private.sign(options["message"].encode()))}
                response = self.post("key/verify", data)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(self.client.get("/api/v1/bookmarks").status_code, 200)
                self.assertEqual(self.post("key/verify", data).status_code, 400)

    def test_unregistered_private_key_and_forged_public_key_rejected(self):
        trusted = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, fingerprint=hashlib.sha256(trusted).hexdigest(), public_key=trusted)
        attacker = ed25519.Ed25519PrivateKey.generate()
        options = self.post("key/options", {}).json()["data"]
        response = self.post("key/verify", {"challenge_id": options["challenge_id"],
            "signature": bytes_to_base64url(attacker.sign(options["message"].encode())),
            "public_key": bytes_to_base64url(attacker.public_key().public_bytes_raw())})
        self.assertEqual(response.status_code, 401)

    def test_normal_reset_password_command_does_not_change_hash(self):
        original = self.user.password
        with self.assertRaises(CommandError):
            call_command("initialize", reset_password=True, stdout=StringIO())
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, original)

    def test_key_file_generation_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "login.pem"
            path.write_bytes(b"preserve existing private key")
            with self.assertRaises(CommandError):
                call_command("file_key_setup", generate=str(path), stdout=StringIO())
            self.assertEqual(path.read_bytes(), b"preserve existing private key")
            self.assertFalse(FileKeyCredential.objects.exists())

    def test_normal_initialize_rejects_invalid_explicit_private_key_before_preparing(self):
        with patch("runtime.management.commands.initialize.prepare_databases") as prepare:
            with self.assertRaises(CommandError):
                call_command("initialize", private_key="missing-key.pem", stdout=StringIO())
            prepare.assert_not_called()
        self.assertFalse(FileKeyCredential.objects.exists())

    def test_initialize_registers_only_public_half_and_is_idempotent(self):
        from runtime.file_keys import generate_private_key
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "initial.pem"
            private = generate_private_key(path)
            original = path.read_bytes()
            old_password = self.user.password
            with patch("runtime.management.commands.initialize.prepare_databases"):
                for _ in range(2):
                    call_command("initialize", private_key=str(path), stdout=StringIO())
            self.assertEqual(FileKeyCredential.objects.count(), 1)
            stored = FileKeyCredential.objects.get()
            self.assertEqual(bytes(stored.public_key), private.public_key().public_bytes_raw())
            self.assertEqual(path.read_bytes(), original)
            self.user.refresh_from_db()
            self.assertEqual(self.user.password, old_password)

    def test_missing_owner_can_generate_key_before_initialization(self):
        self.user.delete()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bootstrap.pem"
            call_command("file_key_setup", generate=str(path), stdout=StringIO())
            self.assertTrue(path.is_file())
            self.assertFalse(get_user_model().objects.exists())
            with patch("runtime.management.commands.initialize.prepare_databases"):
                call_command("initialize", private_key=str(path), stdout=StringIO())
            owner = get_user_model().objects.get(username="owner")
            self.assertFalse(owner.has_usable_password())
            self.assertEqual(FileKeyCredential.objects.get().user_id, owner.id)

    def test_key_authenticated_owner_can_add_passkey_from_settings_without_setup_token(self):
        private = ed25519.Ed25519PrivateKey.generate()
        raw = private.public_key().public_bytes_raw()
        FileKeyCredential.objects.create(user=self.user, fingerprint=hashlib.sha256(raw).hexdigest(), public_key=raw)
        challenge = self.post("key/options", {}).json()["data"]
        self.assertEqual(self.post("key/verify", {"challenge_id": challenge["challenge_id"],
            "signature": bytes_to_base64url(private.sign(challenge["message"].encode()))}).status_code, 200)
        inventory = self.client.get("/api/v1/session/passkeys").json()["data"]
        self.assertTrue(inventory["can_add"])
        self.assertEqual(inventory["items"], [])
        original_session = dict(self.client.session)
        original_key = self.client.session.session_key
        options = self.post("passkey/register/options", {"name": "我的电脑"}).json()["data"]
        response = self.post("passkey/register/verify", {"challenge_id": options["challenge_id"],
            "credential": self.credential(options), "name": "我的电脑"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(PasskeyCredential.objects.get().name, "我的电脑")
        self.assertEqual(dict(self.client.session), original_session)
        self.assertEqual(self.client.session.session_key, original_key)
        self.assertFalse(PasskeySetupToken.objects.exists())
        self.assertEqual(self.client.get("/api/v1/session/passkeys").json()["data"]["items"][0]["name"], "我的电脑")

    def test_password_only_session_cannot_bind_public_key_without_console_authorization(self):
        self.assertEqual(self.client.get("/api/v1/session/passkeys").status_code, 401)
        with override_settings(BOOKMARK_AUTH_DEBUG=True, AUTHENTICATION_BACKENDS=DEBUG_BACKENDS):
            self.assertEqual(self.post("login", {"password": "464194"}).status_code, 200)
            self.assertFalse(self.client.get("/api/v1/session/passkeys").json()["data"]["can_add"])
            self.assertEqual(self.post("passkey/register/options", {}).status_code, 403)
