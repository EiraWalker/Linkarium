"""Independent-process tests use disk SQLite and separate thread-local DB connections."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class AuthenticationDatabaseTests(unittest.TestCase):
    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory(prefix="linkarium-auth-") as directory:
            env = {**os.environ, "BOOKMARK_DATA_ROOT": directory, "BOOKMARK_AUTH_DEBUG": "0",
                "BOOKMARK_PASSKEY_ORIGIN": "", "BOOKMARK_HTTPS": "0"}
            result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), scenario],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disk_sqlite_concurrent_initialization_grant_consumption_delete_and_revocation(self):
        self.run_scenario("concurrency")

    def test_upgrade_keeps_legacy_handle_and_credentials_and_invalidates_old_authority(self):
        self.run_scenario("migration")


def _run_child(scenario):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from datetime import timedelta
    import copy
    from concurrent.futures import ThreadPoolExecutor
    import hashlib
    import secrets
    import threading
    from unittest.mock import patch
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import close_old_connections, connection
    from django.test import Client
    from django.utils import timezone
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from webauthn import verify_registration_response
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
    from runtime.models import (AuthState, EnrollmentGrant, FileKeyCredential, PasskeyChallenge,
        PasskeyCredential, PasskeySetupToken, RuntimeState)
    from runtime.auth_grants import issue_console_grant
    from runtime.test_authentication import AuthenticationTests, ORIGIN
    from library.models import LibrarySettings

    if scenario == "migration":
        call_command("migrate", "auth", database="default", verbosity=0)
        call_command("migrate", "runtime", "0002_public_key_auth", database="default", verbosity=0)
        owner = get_user_model().objects.create_user("owner", password=None)
        private = ed25519.Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes_raw()
        file_key = FileKeyCredential.objects.create(user=owner, fingerprint=hashlib.sha256(public).hexdigest(), public_key=public)
        signer = AuthenticationTests()
        signer.user, signer.private = owner, ec.generate_private_key(ec.SECP256R1())
        signer.credential_id = secrets.token_bytes(32)
        signer.client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        registration_challenge = secrets.token_bytes(32)
        registration_options = {"options": {"challenge": bytes_to_base64url(registration_challenge)}}
        verified = verify_registration_response(credential=signer.credential(registration_options),
            expected_challenge=registration_challenge, expected_rp_id="localhost", expected_origin=ORIGIN,
            require_user_verification=True)
        key = PasskeyCredential.objects.create(user=owner, credential_id=bytes_to_base64url(verified.credential_id),
            public_key=verified.credential_public_key, sign_count=42, rp_id="localhost", name="旧电脑")
        legacy = PasskeySetupToken.objects.create(user=owner, token_hash="a" * 64,
            origin=ORIGIN, expires_at=timezone.now() + timedelta(minutes=10))
        # The current model has new columns, so insert the old challenge using its migration state.
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO runtime_passkeychallenge (id,session_hash,challenge,purpose,origin,rp_id,setup_id,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                [secrets.token_hex(16), "b" * 64, "legacy", "register", ORIGIN, "localhost", legacy.pk.hex, timezone.now().isoformat()])
        original_key = (key.pk, key.credential_id, bytes(key.public_key), key.sign_count)
        original_file = (file_key.pk, bytes(file_key.public_key))
        call_command("migrate", database="default", verbosity=0)
        state = AuthState.objects.get(pk=1)
        assert bytes(state.webauthn_user_handle) == f"website-owner-{owner.pk}".encode()
        key.refresh_from_db()
        file_key.refresh_from_db()
        assert (key.pk, key.credential_id, bytes(key.public_key), key.sign_count) == original_key
        assert (file_key.pk, bytes(file_key.public_key)) == original_file
        assert not PasskeySetupToken.objects.exists() and not PasskeyChallenge.objects.exists()
        call_command("migrate", database="library", verbosity=0)
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        options = signer.post("passkey/login/options", {}).json()["data"]
        response = signer.post("passkey/login/verify", {"challenge_id": options["challenge_id"],
            "credential": signer.credential(options, "login", count=43)})
        assert response.status_code == 200, response.content
        assert signer.client.get("/api/v1/bookmarks").status_code == 200
        assert signer.client.session["credential_id"] == str(key.pk)
        assert signer.post("logout", {}).status_code == 200
        options = signer.post("key/options", {}).json()["data"]
        response = signer.post("key/verify", {"challenge_id": options["challenge_id"],
            "signature": bytes_to_base64url(private.sign(options["message"].encode()))})
        assert response.status_code == 200, response.content
        assert signer.client.get("/api/v1/bookmarks").status_code == 200
        assert signer.client.session["credential_id"] == str(file_key.pk)
        print("migration: legacy handle/public keys/counters preserved; real WebAuthn and Ed25519 login passed")
        return

    call_command("migrate", database="default", verbosity=0)
    call_command("migrate", database="library", verbosity=0)
    assert not str(connection.settings_dict["NAME"]).startswith("file:memory")
    fixture = AuthenticationTests()
    fixture.setUp()
    fixture.user.delete()

    def make_browser():
        signer = copy.copy(fixture)
        signer.private = ec.generate_private_key(ec.SECP256R1())
        signer.credential_id = secrets.token_bytes(32)
        signer.client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        return signer

    def grant_payload(grant):
        if "initialization_code" in grant:
            return {"bootstrap_code": grant["initialization_code"], "name": "首把 Passkey"}
        return {"enrollment_token": grant["enrollment_url"].split("=", 1)[1],
            "authorization_code": grant["authorization_code"], "name": "设备 Passkey"}

    def prepared(signer, grant):
        response = signer.post("passkey/register/options", grant_payload(grant))
        assert response.status_code == 200, response.content
        options = response.json()["data"]
        return {"challenge_id": options["challenge_id"], "credential": signer.credential(options)}

    def parallel(functions):
        barrier = threading.Barrier(len(functions))
        def run(function):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    bootstrap = issue_console_grant("bootstrap", ORIGIN)
    browsers = [make_browser(), make_browser()]
    payloads = [prepared(browser, bootstrap) for browser in browsers]
    responses = parallel([lambda i=i: browsers[i].post("passkey/register/verify", payloads[i]) for i in range(2)])
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1, statuses
    assert get_user_model().objects.filter(username="owner").count() == 1
    assert PasskeyCredential.objects.count() == 1
    owner_browser = browsers[statuses.index(200)]

    grant_response = owner_browser.post("passkey/grants", {})
    assert grant_response.status_code == 201, grant_response.content
    grant = grant_response.json()["data"]
    browsers = [make_browser(), make_browser()]
    payloads = [prepared(browser, grant) for browser in browsers]
    responses = parallel([lambda i=i: browsers[i].post("passkey/register/verify", payloads[i]) for i in range(2)])
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1, statuses
    assert PasskeyCredential.objects.count() == 2
    new_browser = browsers[statuses.index(200)]

    def delete_credential(signer, credential_id):
        token = signer.client.get("/api/v1/session").json()["data"]["csrf_token"]
        return signer.client.delete(f"/api/v1/session/passkeys/{credential_id}", data="{}",
            content_type="application/json", HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN=ORIGIN)

    browsers = [owner_browser, new_browser]
    ids = [browser.client.session["credential_id"] for browser in browsers]
    responses = parallel([lambda i=i: delete_credential(browsers[i], ids[i]) for i in range(2)])
    statuses = [response.status_code for response in responses]
    assert sorted(statuses) == [200, 409], statuses
    assert PasskeyCredential.objects.count() == 1
    survivor = browsers[statuses.index(409)]
    survivor_id = survivor.client.session["credential_id"]

    # Add a second independent credential, then authenticate it with its real signature.
    replacement = make_browser()
    options_response = survivor.post("passkey/register/options", {"name": "备用"})
    options = options_response.json()["data"]
    payload = {"challenge_id": options["challenge_id"], "credential": replacement.credential(options)}
    assert survivor.post("passkey/register/verify", payload).status_code == 200
    login_options = replacement.post("passkey/login/options", {}).json()["data"]
    login_response = replacement.post("passkey/login/verify", {"challenge_id": login_options["challenge_id"],
        "credential": replacement.credential(login_options, "login", count=2)})
    assert login_response.status_code == 200, login_response.content
    grant = survivor.post("passkey/grants", {}).json()["data"]
    target = make_browser()
    payload = prepared(target, grant)
    verified, revoked = threading.Event(), threading.Event()

    def verify_then_wait(*args, **kwargs):
        result = verify_registration_response(*args, **kwargs)
        verified.set()
        assert revoked.wait(timeout=10), "revocation did not finish"
        return result

    def revoke_during_verification():
        assert verified.wait(timeout=10), "verification did not reach commit boundary"
        try:
            return delete_credential(replacement, survivor_id)
        finally:
            revoked.set()

    with patch("runtime.authentication.verify_registration_response", side_effect=verify_then_wait):
        responses = parallel([lambda: target.post("passkey/register/verify", payload), revoke_during_verification])
    assert [r.status_code for r in responses] == [403, 200], [r.content for r in responses]
    assert PasskeyCredential.objects.count() == 1
    assert target.client.get("/api/v1/bookmarks").status_code == 401
    print("disk SQLite: concurrent bootstrap, shared grant, last credential deletion, revocation during real verification passed")


if __name__ == "__main__":
    _run_child(sys.argv[1])
