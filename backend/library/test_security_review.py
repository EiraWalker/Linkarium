"""Review regressions for older AI settings and untrusted import summary counts."""
import hashlib
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from runtime.models import ProviderSecret, RuntimeState
from runtime.testing import force_credential_login
from .ai_transport import AIError
from .classification import configure_profile, test_profile as check_profile, verified_client
from .errors import ApiError
from .importing import MAX_EXECUTION_ENTRIES, _preview_digest, execute_import, import_plan, preview_import, start_import
from .models import AIProfile, Bookmark, ImportBatch, Job, LibrarySettings


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SecurityReviewTests(TestCase):
    databases = {"default", "library"}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        user = get_user_model().objects.create_user("owner")
        force_credential_login(self.client, user)

    def legacy_profile(self):
        profile = AIProfile.objects.create(name="Legacy provider", base_url="http://remote.example/v1", model="legacy-model")
        fingerprint = hashlib.sha256((profile.base_url + "\n" + profile.protocol).encode()).hexdigest()
        ProviderSecret.objects.create(profile_id=profile.id, api_key="retained-test-key", bound_endpoint=profile.base_url,
            endpoint_fingerprint=fingerprint, verified=True, verified_library_epoch=1)
        LibrarySettings.objects.filter(pk=1).update(active_ai_profile_id=profile.id)
        return profile

    def test_insecure_existing_ai_profiles_remain_readable_without_becoming_usable(self):
        profile = self.legacy_profile()
        for route in ("/api/v1/ai-profiles", f"/api/v1/ai-profiles/{profile.id}"):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200, response.content)
            data = response.json()["data"]
            data = data[0] if isinstance(data, list) else data
            self.assertEqual(data["base_url"], profile.base_url)
            self.assertEqual(data["endpoint_error_code"], "AI_HTTPS_REQUIRED")
            self.assertTrue(data["key_configured"])
            self.assertFalse(data["key_verified"])
            self.assertFalse(data["binding_matches"])
            self.assertNotIn("retained-test-key", response.content.decode())
        with patch("library.ai_transport.httpx.Client") as network:
            for action in (lambda: verified_client(profile.id), lambda: check_profile(profile.id)):
                with self.assertRaises(AIError) as error:
                    action()
                self.assertEqual(error.exception.code, "AI_HTTPS_REQUIRED")
            network.assert_not_called()

    def test_old_remote_http_profile_can_be_reconfigured_without_losing_the_profile(self):
        profile = self.legacy_profile()
        updated = configure_profile({"name": profile.name, "base_url": "https://remote.example/v1", "model": profile.model,
            "revision": profile.revision, "api_key": "replacement-test-key"}, profile.id)
        self.assertEqual(updated.pk, profile.pk)
        response = self.client.get(f"/api/v1/ai-profiles/{profile.id}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["endpoint_error_code"], "")
        self.assertFalse(response.json()["data"]["key_verified"])
        self.assertTrue(response.json()["data"]["binding_matches"])

    def forged_import(self):
        content = ("<DL>" + "".join(f'<A HREF="https://example.org/review/{n}">Site {n}</A>'
            for n in range(MAX_EXECUTION_ENTRIES + 1)) + "</DL>").encode()
        batch = preview_import(content)
        # A restored SQLite library can have valid schema and forged denormalized counts.
        ImportBatch.objects.filter(pk=batch.pk).update(valid=1)
        batch.refresh_from_db()
        return batch

    def test_import_plan_counts_real_entries_instead_of_trusting_restored_summary(self):
        batch = self.forged_import()
        plan = import_plan(batch)
        self.assertEqual(plan["entries"], MAX_EXECUTION_ENTRIES + 1)
        self.assertFalse(plan["can_execute"])
        with self.assertRaises(ApiError):
            start_import(batch, plan["confirmation_token"])
        self.assertFalse(Bookmark.objects.exists())
        self.assertFalse(Job.objects.exists())

    def test_worker_rejects_underreported_import_before_creating_any_bookmarks(self):
        batch = self.forged_import()
        batch.confirmation_digest = _preview_digest(batch)
        batch.capture_budget, batch.ai_budget = 2, 2
        batch.state = "queued"
        batch.save()
        with self.assertRaises(ApiError):
            execute_import(batch.id)
        self.assertFalse(Bookmark.objects.exists())
        self.assertFalse(Job.objects.filter(kind="capture").exists())
