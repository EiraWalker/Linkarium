"""Regression coverage for audited owner upload and import resource boundaries."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from runtime.models import RuntimeState
from runtime.testing import force_credential_login
from .ai_transport import AIError, normalize_endpoint
from .importing import import_plan, preview_import, start_import, execute_import, MAX_EXECUTION_ENTRIES
from .models import Bookmark, ImportBatch, Job, LibrarySettings


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SecurityLimitsTests(TestCase):
    databases = {'default', 'library'}

    def setUp(self):
        RuntimeState.objects.create(pk=1)
        LibrarySettings.objects.create(pk=1)
        user = get_user_model().objects.create_user('owner')
        force_credential_login(self.client, user)

    def preview(self, count=1):
        content = ('<DL>' + ''.join(f'<A HREF="https://example.org/{n}">Site {n}</A>' for n in range(count)) + '</DL>').encode()
        return preview_import(content)

    def post(self, path, body):
        return self.client.post('/api/v1/' + path, json.dumps(body), content_type='application/json',
            HTTP_IDEMPOTENCY_KEY='request-' + path)

    def test_remote_plaintext_ai_is_rejected_and_literal_loopback_retained(self):
        for endpoint in ('http://example.org/v1', 'http://192.168.1.3:8080/v1', 'http://127.0.0.1.example.org/v1'):
            with self.subTest(endpoint=endpoint), self.assertRaises(AIError) as error:
                normalize_endpoint(endpoint)
            self.assertEqual(error.exception.code, 'AI_HTTPS_REQUIRED')
        for endpoint in ('http://127.0.0.1:11434/v1', 'http://[::1]:11434/v1', 'http://localhost:8080/v1', 'https://example.org/v1'):
            self.assertEqual(normalize_endpoint(endpoint), endpoint)

    def test_both_upload_routes_only_preview_without_creating_jobs(self):
        for route in ('imports', 'imports/preview'):
            response = self.client.post('/api/v1/' + route,
                {'file': SimpleUploadedFile('bookmarks.html', b'<DL><A HREF="https://example.org">Site</A></DL>')},
                HTTP_IDEMPOTENCY_KEY=route)
            self.assertEqual(response.status_code, 201, response.content)
            plan = response.json()['data']['execution_plan']
            self.assertEqual((plan['captures'], plan['ai_requests']), (2, 2))
        self.assertFalse(Job.objects.exists())
        self.assertFalse(Bookmark.objects.exists())

    def test_execute_requires_confirmation_of_exact_immutable_preview(self):
        batch = self.preview()
        response = self.post(f'imports/{batch.id}/execute', {})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(Job.objects.exists())
        token = import_plan(batch)['confirmation_token']
        batch.entries.update(title='Changed after preview')
        response = self.post(f'imports/{batch.id}/execute', {'confirmation_token': token})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(Job.objects.exists())

    def test_confirmed_import_has_bounded_dual_capture_and_ai_budgets(self):
        batch = self.preview(11)
        plan = import_plan(batch)
        start_import(batch, plan['confirmation_token'])
        batch.refresh_from_db()
        self.assertEqual((batch.capture_budget, batch.ai_budget), (22, 3))
        execute_import(batch.id)
        self.assertEqual(Bookmark.objects.count(), 11)
        self.assertEqual(Job.objects.filter(kind='capture').count(), 22)

    def test_parser_limit_is_separate_from_execution_quota(self):
        batch = self.preview(MAX_EXECUTION_ENTRIES + 1)
        self.assertEqual(batch.valid, MAX_EXECUTION_ENTRIES + 1)
        self.assertFalse(import_plan(batch)['can_execute'])
        with self.assertRaises(Exception) as error:
            start_import(batch, import_plan(batch)['confirmation_token'])
        self.assertEqual(error.exception.code, 'IMPORT_WORKLOAD_LIMIT')
        self.assertFalse(Job.objects.exists())

    def test_only_one_import_can_execute_at_once(self):
        first, second = self.preview(), self.preview()
        start_import(first, import_plan(first)['confirmation_token'])
        with self.assertRaises(Exception) as error:
            start_import(second, import_plan(second)['confirmation_token'])
        self.assertEqual(error.exception.code, 'IMPORT_BUSY')
        self.assertEqual(Job.objects.filter(kind='import').count(), 1)

    def test_upload_admission_requires_authenticated_owner(self):
        self.client.logout()
        response = self.post('runtime/uploads', {'path': '/api/v1/restores/validate', 'size': 1024**3})
        self.assertEqual(response.status_code, 401)

    def test_ai_request_budget_blocks_retries_before_network_and_survives_contexts(self):
        import httpx
        from .ai_transport import ChatClient, request_budget
        from .importing import reserve_ai_request
        batch = self.preview()
        start_import(batch, import_plan(batch)['confirmation_token'])
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
        client = ChatClient('https://example.org/v1', 'test-key', 'model', transport=httpx.MockTransport(respond))
        for _ in range(2):
            with request_budget(lambda: reserve_ai_request(batch.id)):
                client.complete([])
        with request_budget(lambda: reserve_ai_request(batch.id)), self.assertRaises(AIError) as error:
            client.complete([])
        self.assertEqual(error.exception.code, 'AI_REQUEST_QUOTA')
        self.assertEqual(len(calls), 2)
        batch.refresh_from_db()
        self.assertEqual(batch.ai_requests_used, 2)

    def test_unsigned_restored_ai_budget_cannot_authorize_provider_calls(self):
        from .importing import reserve_ai_request
        batch = self.preview()
        ImportBatch.objects.filter(pk=batch.id).update(ai_budget=100000)
        with self.assertRaises(AIError):
            reserve_ai_request(batch.id)
