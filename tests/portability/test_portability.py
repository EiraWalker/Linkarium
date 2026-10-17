from runtime.testing import force_credential_login
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import httpx
from django.test import TransactionTestCase, override_settings

from library.ai_transport import AIError, ChatClient, endpoint_fingerprint, strict_json
from library.classification import accept_proposals, generate_proposals, test_profile, validate_result, verified_client
from library.importing import parse_bookmark_html, preview_import, execute_import
from runtime.maintenance import MaintenanceError, create_backup, validate_package


CHROME_HTML = b'''<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<DL><p><DT><H3>Design</H3><DL><p><DT><H3>Fonts</H3><DL><p>
<DT><A HREF="https://example.com/">Font &amp; icon</A>
<DT><A HREF="HTTPS://EXAMPLE.COM:443">Duplicate</A>
<DT><A HREF="javascript:alert(1)">Unsafe</A>
</DL><p></DL><p><DT><A HREF="https://outside.example/">Outside</A></DL>'''


class ParserAndTransportTests(unittest.TestCase):
    def test_default_port_selection_skips_a_live_unrelated_listener(self):
        import socket
        from supervisor import available_port
        with socket.socket() as unrelated:
            unrelated.bind(("127.0.0.1", 0))
            unrelated.listen(1)
            occupied = unrelated.getsockname()[1]
            if occupied == 65535:
                self.skipTest("Ephemeral port has no following candidate")
            selected = available_port("127.0.0.1", occupied)
            self.assertGreater(selected, occupied)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", selected))
            with socket.create_connection(("127.0.0.1", occupied), timeout=1):
                pass

    def test_no_available_candidate_fails_without_disrupting_listener(self):
        import socket
        from supervisor import available_port
        with socket.socket() as unrelated:
            unrelated.bind(("127.0.0.1", 0))
            unrelated.listen(1)
            with self.assertRaisesRegex(RuntimeError, "No available Web port"):
                available_port("127.0.0.1", unrelated.getsockname()[1], attempts=1)
            with socket.create_connection(unrelated.getsockname(), timeout=1):
                pass

    def test_chrome_nested_and_root_paths_and_invalid_protocol(self):
        entries = parse_bookmark_html(CHROME_HTML)
        self.assertEqual(entries[0].original_folder_path, ["Design", "Fonts"])
        self.assertEqual(entries[0].title, "Font & icon")
        self.assertEqual(entries[2].error_code, "INVALID_URL")
        self.assertEqual(entries[3].original_folder_path, [])

    def test_firefox_unicode_export(self):
        data = '<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><DT><H3>阅读</H3><DL><DT><A HREF="https://例子.测试/a?q=1#b">中文</A></DL></DL>'
        self.assertEqual(parse_bookmark_html(data.encode())[0].original_folder_path, ["阅读"])

    def test_endpoint_fingerprint_covers_path_protocol_and_port(self):
        self.assertEqual(endpoint_fingerprint("https://EXAMPLE.com:443/v1/"), endpoint_fingerprint("https://example.com/v1"))
        self.assertNotEqual(endpoint_fingerprint("https://example.com/v1"), endpoint_fingerprint("https://example.com/other"))
        with self.assertRaises(AIError):
            endpoint_fingerprint("https://key@example.com/v1")

    def test_redirect_never_receives_key(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://attacker.invalid"})
        with self.assertRaises(AIError) as caught:
            ChatClient("https://example.com/v1", "test-key", "test", transport=httpx.MockTransport(handler)).test()
        self.assertEqual(caught.exception.code, "AI_HTTP_REJECTED")
        self.assertEqual(calls, ["https://example.com/v1/chat/completions"])

    def test_slow_non_streaming_ai_can_use_its_response_budget(self):
        def handler(request):
            # Simulate a model that needs 45s before returning its first byte.
            # The transport obeys the timeout passed through the real client.
            if request.extensions["timeout"]["read"] < 45:
                raise httpx.ReadTimeout("Slow fixture response", request=request)
            self.assertLessEqual(request.extensions["timeout"]["connect"], 10)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
        self.assertEqual(ChatClient("https://example.com/v1", "test-key", "model", transport=httpx.MockTransport(handler)).test()["model"], "model")

    def test_tools_are_rejected_and_plain_json_is_required(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}',
                "tool_calls": [{"function": {"name": "move"}}]}}]})
        with self.assertRaises(AIError):
            ChatClient("https://example.com", "test-key", "model", transport=httpx.MockTransport(handler)).test()
        with self.assertRaises(AIError):
            strict_json('{"items":[],"items":[]}')

    def test_complete_accepts_one_json_fence_but_rejects_prose_and_duplicate_keys(self):
        for content, accepted in [('```json\n{"ok":true}\n```', True),
                                  ('```\n{"ok":true}\n```', True),
                                  ('Explanation\n```json\n{"ok":true}\n```', False),
                                  ('```json\n{"ok":true,"ok":false}\n```', False),
                                  ('```javascript\n{"ok":true}\n```', False)]:
            with self.subTest(content=content):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))
                client = ChatClient("https://example.com", "test-key", "model", transport=transport)
                if accepted:
                    self.assertEqual(client.test()["model"], "model")
                else:
                    with self.assertRaises(AIError):
                        client.test()

    def test_model_cannot_classify_unknown_or_duplicate_bookmarks(self):
        identity, folder = str(uuid.uuid4()), str(uuid.uuid4())
        result = {"items": [{"bookmark_id": str(uuid.uuid4()), "target": {"existing_folder_id": folder}, "reason": "x"}]}
        with self.assertRaises(AIError):
            validate_result(result, [identity], {folder: {"parent_id": None}})


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.generation = self.root / "source"
        self.generation.mkdir()
        payload = b"<!doctype html><html><body>Archived latest page</body></html>"
        artifact_hash = hashlib.sha256(payload).hexdigest()
        relative_path = f"artifacts/{artifact_hash[:2]}/{artifact_hash}.html"
        artifact = self.generation / relative_path
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(payload)
        self.artifact, self.hash = artifact, artifact_hash
        self.connection = sqlite3.connect(self.generation / "library.sqlite3")
        self.connection.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE library_bookmark(id TEXT PRIMARY KEY,canonical_url TEXT UNIQUE);
            CREATE TABLE library_folder(id TEXT PRIMARY KEY,parent_id TEXT);
            CREATE TABLE library_artifact(sha256 TEXT PRIMARY KEY,relative_path TEXT,byte_size INTEGER);
            CREATE TABLE library_snapshotversion(id TEXT PRIMARY KEY,bookmark_id TEXT,variant TEXT,artifact_id TEXT REFERENCES library_artifact(sha256));
            CREATE TABLE library_capturestate(id INTEGER PRIMARY KEY,bookmark_id TEXT,variant TEXT,current_version_id TEXT REFERENCES library_snapshotversion(id));
        ''')
        self.connection.execute("INSERT INTO library_bookmark VALUES('b','https://example.com/')")
        self.connection.execute("INSERT INTO library_artifact VALUES(?,?,?)", (artifact_hash, relative_path, len(payload)))
        self.connection.execute("INSERT INTO library_snapshotversion VALUES('s','b','desktop',?)", (artifact_hash,))
        self.connection.execute("INSERT INTO library_capturestate VALUES(1,'b','desktop','s')")
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def set_artifact_size(self, byte_size):
        """Write real, compressible HTML without allocating a huge test string."""
        prefix, suffix = b'<!doctype html><html><body><!--', b'--></body></html>'
        temporary = self.generation / 'large.html'
        digest = hashlib.sha256()
        with temporary.open('wb') as target:
            target.write(prefix)
            digest.update(prefix)
            remaining = byte_size - len(prefix) - len(suffix)
            chunk = b'x' * (1024 * 1024)
            while remaining:
                part = chunk[:min(remaining, len(chunk))]
                target.write(part)
                digest.update(part)
                remaining -= len(part)
            target.write(suffix)
            digest.update(suffix)
        artifact_hash = digest.hexdigest()
        relative_path = f'artifacts/{artifact_hash[:2]}/{artifact_hash}.html'
        artifact = self.generation / relative_path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(artifact)
        self.connection.execute('INSERT INTO library_artifact VALUES(?,?,?)',
                                (artifact_hash, relative_path, byte_size))
        self.connection.execute('UPDATE library_snapshotversion SET artifact_id=?', (artifact_hash,))
        self.connection.execute('DELETE FROM library_artifact WHERE sha256=?', (self.hash,))
        self.connection.commit()
        self.artifact, self.hash = artifact, artifact_hash
        return relative_path

    def test_96_mib_artifact_backup_and_restore_round_trip(self):
        size = 96 * 1024**2
        relative_path = self.set_artifact_size(size)
        package = self.root / 'large.bookmarklib.zip'
        create_backup(self.generation, package)
        candidate = self.root / 'candidate'
        manifest = validate_package(package, candidate, expected_schema=self.generation / 'library.sqlite3')
        self.assertEqual(manifest['files'][relative_path], {'size': size, 'sha256': self.hash})
        restored = candidate / relative_path
        self.assertEqual(restored.stat().st_size, size)
        actual_hash = hashlib.sha256()
        with restored.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                actual_hash.update(chunk)
        self.assertEqual(actual_hash.hexdigest(), self.hash)
        with closing(sqlite3.connect(candidate / 'library.sqlite3')) as database:
            self.assertEqual(database.execute('SELECT a.sha256,a.byte_size FROM library_capturestate c '
                'JOIN library_snapshotversion s ON c.current_version_id=s.id '
                'JOIN library_artifact a ON s.artifact_id=a.sha256').fetchone(), (self.hash, size))

    def test_artifact_above_96_mib_is_rejected_by_backup_and_restore(self):
        manifest = create_backup(self.generation, self.root / 'small.zip')['manifest']
        size = 96 * 1024**2 + 1
        relative_path = self.set_artifact_size(size)
        with self.assertRaises(MaintenanceError) as caught:
            create_backup(self.generation, self.root / 'oversized-backup.zip')
        self.assertEqual(caught.exception.code, 'BACKUP_ARTIFACT_INVALID')
        self.assertFalse((self.root / 'oversized-backup.zip').exists())

        # Construct a complete, correctly hashed package independently so the
        # restore entry-size gate is exercised even when backup rejects it.
        database = self.root / 'oversized.sqlite3'
        with closing(sqlite3.connect(database)) as target:
            self.connection.backup(target)
        manifest['files'] = {
            'library.sqlite3': {'size': database.stat().st_size,
                               'sha256': hashlib.sha256(database.read_bytes()).hexdigest()},
            relative_path: {'size': size, 'sha256': self.hash},
        }
        package = self.root / 'oversized-restore.zip'
        with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.write(database, 'library.sqlite3')
            archive.write(self.artifact, relative_path)
        candidate = self.root / 'oversized-candidate'
        with self.assertRaises(MaintenanceError) as caught:
            validate_package(package, candidate, expected_schema=self.generation / 'library.sqlite3')
        self.assertEqual(caught.exception.code, 'BACKUP_ENTRY_TOO_LARGE')
        self.assertFalse(any(candidate.iterdir()))

    def test_live_wal_backup_round_trip_and_credentials_absent(self):
        self.connection.execute("INSERT INTO library_bookmark VALUES('wal','https://wal.example/')")
        self.connection.commit()
        package = self.root / "backup.bookmarklib.zip"
        create_backup(self.generation, package)
        candidate = self.root / "candidate"
        manifest = validate_package(package, candidate, expected_schema=self.generation / "library.sqlite3")
        self.assertEqual(manifest["counts"]["bookmarks"], 2)
        with zipfile.ZipFile(package) as archive:
            self.assertFalse(any("state" in name or "secret" in name or "runtime" in name for name in archive.namelist()))
        with closing(sqlite3.connect(candidate / "library.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM library_bookmark").fetchone()[0], 2)

    def test_path_traversal_is_rejected_before_extraction(self):
        package = self.root / "bad.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../outside", b"bad")
        with self.assertRaises(MaintenanceError) as caught:
            validate_package(package, self.root / "candidate")
        self.assertEqual(caught.exception.code, "BACKUP_UNSAFE_PATH")
        self.assertFalse((self.root / "outside").exists())

    def test_duplicate_paths_and_symlinks_are_rejected(self):
        package = self.root / "bad.zip"
        info = zipfile.ZipInfo("library.sqlite3")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(info, b"/etc/passwd")
        with self.assertRaises(MaintenanceError) as caught:
            validate_package(package, self.root / "candidate")
        self.assertEqual(caught.exception.code, "BACKUP_UNSAFE_ENTRY")

    def test_corrupt_artifact_prevents_backup(self):
        self.artifact.write_bytes(b"changed")
        with self.assertRaises(MaintenanceError) as caught:
            create_backup(self.generation, self.root / "bad.zip")
        self.assertEqual(caught.exception.code, "BACKUP_ARTIFACT_CORRUPT")

    def test_unreferenced_artifact_path_cannot_escape_library_on_cleanup(self):
        self.connection.execute("INSERT INTO library_artifact VALUES(?,?,?)", ("f" * 64, "../../state.sqlite3", 1))
        self.connection.commit()
        with self.assertRaises(MaintenanceError) as caught:
            create_backup(self.generation, self.root / "bad.zip")
        self.assertEqual(caught.exception.code, "BACKUP_ARTIFACT_INVALID")

    def test_restore_rejects_orphan_attack_even_with_updated_valid_manifest_hash(self):
        good = self.root / "good.zip"
        create_backup(self.generation, good)
        staging = self.root / "attacker"
        staging.mkdir()
        with zipfile.ZipFile(good) as archive:
            content = {name: archive.read(name) for name in archive.namelist()}
        database = staging / "library.sqlite3"
        database.write_bytes(content["library.sqlite3"])
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("INSERT INTO library_artifact VALUES(?,?,?)", ("f" * 64, "../../state.sqlite3", 1))
            connection.commit()
        manifest = json.loads(content["manifest.json"])
        content["library.sqlite3"] = database.read_bytes()
        manifest["files"]["library.sqlite3"] = {"size": len(content["library.sqlite3"]),
            "sha256": hashlib.sha256(content["library.sqlite3"]).hexdigest()}
        content["manifest.json"] = json.dumps(manifest).encode()
        attack = self.root / "attack.zip"
        with zipfile.ZipFile(attack, "w") as archive:
            for name, value in content.items():
                archive.writestr(name, value)
        with self.assertRaises(MaintenanceError) as caught:
            validate_package(attack, self.root / "candidate", expected_schema=self.generation / "library.sqlite3")
        self.assertEqual(caught.exception.code, "BACKUP_ARTIFACT_INVALID")

    def test_trigger_and_wrong_current_device_are_rejected(self):
        self.connection.execute("UPDATE library_capturestate SET variant='mobile'")
        self.connection.commit()
        with self.assertRaises(MaintenanceError) as caught:
            create_backup(self.generation, self.root / "bad.zip")
        self.assertEqual(caught.exception.code, "BACKUP_CURRENT_POINTER_INVALID")
        self.connection.execute("UPDATE library_capturestate SET variant='desktop'")
        self.connection.execute("CREATE TRIGGER evil AFTER INSERT ON library_bookmark BEGIN DELETE FROM library_bookmark; END")
        self.connection.commit()
        with self.assertRaises(MaintenanceError) as caught:
            create_backup(self.generation, self.root / "bad2.zip")
        self.assertEqual(caught.exception.code, "BACKUP_SCHEMA_UNSAFE")


class CatalogPortabilityTests(TransactionTestCase):
    databases = {"default", "library"}

    def setUp(self):
        from runtime.models import RuntimeState
        from library.models import LibrarySettings
        RuntimeState.objects.using("default").get_or_create(pk=1)
        LibrarySettings.objects.using("library").get_or_create(pk=1)

    def test_import_skips_existing_without_overwrite_and_retains_original_path(self):
        from library.models import Bookmark, BookmarkSource, Folder
        from library.catalog import create_bookmark
        existing, _ = create_bookmark({"url": "https://example.com", "notes": "do not change", "snapshot_enabled": False})
        batch = preview_import(CHROME_HTML)
        self.assertEqual((batch.duplicate, batch.invalid, batch.valid), (2, 1, 1))
        execute_import(batch.id)
        existing.refresh_from_db()
        self.assertEqual(existing.notes, "do not change")
        self.assertFalse(BookmarkSource.objects.filter(bookmark=existing).exists())
        self.assertEqual(Folder.objects.count(), 0)
        imported = Bookmark.objects.get(canonical_url="https://outside.example/")
        self.assertIsNone(imported.folder_id)
        self.assertFalse(imported.is_starred)
        self.assertEqual(BookmarkSource.objects.get(bookmark=imported).source_folder_path, [])
        execute_import(batch.id)
        self.assertEqual(Bookmark.objects.count(), 2)

    def test_stale_proposal_prevents_whole_batch_folder_creation(self):
        from library.models import AIProfile, Bookmark, ClassificationBatch, ClassificationProposal, Folder, LibrarySettings
        profile = AIProfile.objects.create(base_url="https://api.example/v1", model="test")
        batch = ClassificationBatch.objects.create(profile=profile, tree_revision=1)
        bookmarks = [Bookmark.objects.create(original_url=f"https://example.com/{i}", canonical_url=f"https://example.com/{i}", domain="example.com") for i in range(2)]
        proposals = [ClassificationProposal.objects.create(batch=batch, bookmark=b, expected_classification_revision=1,
            expected_tree_revision=1, target_path={"parent_folder_id": None, "new_segments": ["Design"]}) for b in bookmarks]
        Bookmark.objects.filter(pk=bookmarks[1].id).update(classification_revision=2)
        with self.assertRaises(Exception) as caught:
            accept_proposals([{"id": str(p.id), "revision": 1} for p in proposals], 1)
        self.assertEqual(caught.exception.code, "PROPOSAL_STALE")
        self.assertEqual(Folder.objects.count(), 0)
        self.assertFalse(Bookmark.objects.exclude(folder_id=None).exists())

    def test_restored_endpoint_cannot_receive_retained_credentials(self):
        from library.models import AIProfile
        from runtime.models import ProviderSecret, RuntimeState
        profile = AIProfile.objects.create(base_url="https://new.example/v1", model="test")
        ProviderSecret.objects.using("default").create(profile_id=profile.id, api_key="test-only",
            bound_endpoint="https://old.example/v1", endpoint_fingerprint=endpoint_fingerprint("https://old.example/v1"),
            verified=True, verified_library_epoch=RuntimeState.objects.using("default").get(pk=1).library_epoch)
        with self.assertRaises(AIError) as caught:
            verified_client(profile.id)
        self.assertEqual(caught.exception.code, "AI_UNCONFIGURED")

    def configured_profile(self, verified=True):
        from library.models import AIProfile
        from runtime.models import ProviderSecret, RuntimeState
        profile = AIProfile.objects.create(base_url="https://api.example/v1", model="test")
        ProviderSecret.objects.using("default").create(profile_id=profile.id, api_key="test-only",
            bound_endpoint=profile.base_url, endpoint_fingerprint=endpoint_fingerprint(profile.base_url),
            verified=verified, verified_library_epoch=RuntimeState.objects.using("default").get(pk=1).library_epoch)
        return profile

    def test_cancelled_ai_cannot_commit_proposals_after_network_returns(self):
        from library.models import Bookmark, ClassificationProposal
        profile = self.configured_profile()
        bookmark = Bookmark.objects.create(original_url="https://example.com/", canonical_url="https://example.com/", domain="example.com")
        allowed = {"value": True}
        def returned(*args, **kwargs):
            allowed["value"] = False
            return {"items": [{"bookmark_id": str(bookmark.id), "target": {"parent_folder_id": None,
                    "new_segments": ["Design"]}, "reason": "test"}]}
        with patch("library.classification.ChatClient.complete", side_effect=returned):
            with self.assertRaises(Exception) as caught:
                generate_proposals([str(bookmark.id)], profile.id, execution_guard=lambda: allowed["value"])
        self.assertEqual(caught.exception.code, "ATTEMPT_EXPIRED")
        self.assertEqual(ClassificationProposal.objects.count(), 0)

    def test_changed_profile_cannot_commit_old_ai_response(self):
        from library.models import AIProfile, Bookmark, ClassificationProposal
        profile = self.configured_profile()
        bookmark = Bookmark.objects.create(original_url="https://example.com/", canonical_url="https://example.com/", domain="example.com")
        def returned(*args, **kwargs):
            AIProfile.objects.filter(pk=profile.id).update(revision=2)
            return {"items": [{"bookmark_id": str(bookmark.id), "target": {"parent_folder_id": None,
                    "new_segments": ["Design"]}, "reason": "test"}]}
        with patch("library.classification.ChatClient.complete", side_effect=returned):
            with self.assertRaises(AIError) as caught:
                generate_proposals([str(bookmark.id)], profile.id)
        self.assertEqual(caught.exception.code, "AI_CONFIG_STALE")
        self.assertEqual(ClassificationProposal.objects.count(), 0)

    def test_cancelled_api_test_cannot_mark_credentials_verified(self):
        from runtime.models import ProviderSecret
        profile = self.configured_profile(verified=False)
        allowed = {"value": True}
        def returned(*args, **kwargs):
            allowed["value"] = False
            return {"protocol": "chat-completions"}
        with patch("library.classification.ChatClient.test", side_effect=returned):
            with self.assertRaises(Exception) as caught:
                test_profile(profile.id, execution_guard=lambda: allowed["value"])
        self.assertEqual(caught.exception.code, "ATTEMPT_EXPIRED")
        self.assertFalse(ProviderSecret.objects.using("default").get(profile_id=profile.id).verified)

    def test_cancelled_import_does_not_commit_next_entry(self):
        from library.models import Bookmark
        batch = preview_import(CHROME_HTML)
        answers = iter([True, False])
        with self.assertRaises(Exception) as caught:
            execute_import(batch.id, execution_guard=lambda: next(answers))
        self.assertEqual(caught.exception.code, "ATTEMPT_EXPIRED")
        self.assertEqual(Bookmark.objects.count(), 0)

    def test_nginx_auth_subrequest_endpoint_accepts_only_owner_session_cookie(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get("/api/v1/runtime/status").status_code, 401)
        owner = get_user_model().objects.db_manager("default").create_user(username="owner", password="test-only-cookie-password")
        force_credential_login(client, owner)
        response = client.get("/api/v1/runtime/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("maintenance_mode", response.json()["data"])
        client.logout()
        other = get_user_model().objects.db_manager("default").create_user(username="other", password="test-only-cookie-password")
        force_credential_login(client, other)
        self.assertEqual(client.get("/api/v1/runtime/status").status_code, 401)


class MaintenanceFailureTests(TransactionTestCase):
    databases = {"default", "library"}

    def setUp(self):
        from django.db import connections
        from runtime.models import RuntimeState
        from library.models import LibrarySettings, Bookmark
        RuntimeState.objects.using("default").get_or_create(pk=1)
        LibrarySettings.objects.using("library").get_or_create(pk=1)
        Bookmark.objects.create(original_url="https://preserved.example/", canonical_url="https://preserved.example/",
            domain="preserved.example", snapshot_enabled=False)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch("config.paths.DATA_ROOT", self.root)
        self.paths.start()
        self.generation = self.root / "generations" / "initial"
        self.generation.mkdir(parents=True)
        connections["library"].ensure_connection()
        with closing(sqlite3.connect(self.generation / "library.sqlite3")) as database:
            connections["library"].connection.backup(database)
        self.package = self.root / "original.bookmarklib.zip"
        create_backup(self.generation, self.package)

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()
        super().tearDown()

    def prepared_restore(self):
        from runtime.maintenance import create_restore_validation, process_operation, serialize_operation, confirm_restore
        operation = create_restore_validation(self.package.read_bytes())
        self.assertTrue(process_operation(operation.id, stop_services=lambda: None,
            start_services=lambda: None, ready_services=lambda: True))
        operation.refresh_from_db()
        self.assertEqual(operation.state, "awaiting_confirmation")
        token = serialize_operation(operation, include_confirmation=True)["validation_token"]
        confirm_restore(operation.id, token, overwrite=True)
        return operation

    def test_pre_restore_backup_failure_keeps_original_pointer_and_library(self):
        from runtime.models import RuntimeState
        from runtime.maintenance import process_operation
        operation = self.prepared_restore()
        with patch("runtime.maintenance.create_backup", side_effect=MaintenanceError("INSUFFICIENT_DISK_SPACE")):
            result = process_operation(operation.id, stop_services=lambda: None,
                start_services=lambda: None, ready_services=lambda: True)
        self.assertFalse(result)
        operation.refresh_from_db()
        state = RuntimeState.objects.using("default").get(pk=1)
        self.assertEqual((state.active_generation, state.library_epoch, state.maintenance_mode), ("initial", 1, False))
        self.assertEqual(operation.error_code, "INSUFFICIENT_DISK_SPACE")
        self.assertTrue((self.generation / "library.sqlite3").exists())

    def test_new_generation_readiness_failure_rolls_back_and_increments_epoch(self):
        from runtime.models import RuntimeState
        from runtime.maintenance import process_operation
        operation = self.prepared_restore()
        answers = iter([False, True])
        result = process_operation(operation.id, stop_services=lambda: None,
            start_services=lambda: None, ready_services=lambda: next(answers))
        self.assertFalse(result)
        operation.refresh_from_db()
        state = RuntimeState.objects.using("default").get(pk=1)
        self.assertEqual((state.active_generation, state.library_epoch, state.maintenance_mode), ("initial", 3, False))
        self.assertEqual(operation.stage, "failed_rolled_back")
        self.assertTrue((self.root / "backups" / (str(operation.id) + ".pre-restore.bookmarklib.zip")).exists())

    def test_failed_rollback_preserves_maintenance_and_both_generations(self):
        from runtime.models import RuntimeState
        from runtime.maintenance import process_operation
        operation = self.prepared_restore()
        result = process_operation(operation.id, stop_services=lambda: None,
            start_services=lambda: None, ready_services=lambda: False)
        self.assertFalse(result)
        operation.refresh_from_db()
        state = RuntimeState.objects.using("default").get(pk=1)
        self.assertTrue(state.maintenance_mode)
        self.assertEqual(operation.stage, "rollback_failed")
        self.assertTrue((self.generation / "library.sqlite3").exists())
        self.assertTrue((self.root / "generations" / operation.target_generation / "library.sqlite3").exists())

    def test_restored_queued_jobs_and_old_idempotency_are_not_replayed(self):
        from runtime.maintenance import _cancel_candidate_work
        database = self.generation / "library.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("""INSERT INTO library_job
                (id,kind,target_id,variant,state,priority,available_at,attempt_count,active_attempt_id,max_attempts,active_key,generation,library_epoch,input_revision,payload,result,error_code,created_at,updated_at)
                VALUES(?, 'classification',NULL,'','queued',40,CURRENT_TIMESTAMP,0,NULL,3,'old-active','initial',1,1,'{}','{}','',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""", (uuid.uuid4().hex,))
            connection.commit()
        _cancel_candidate_work(database)
        with closing(sqlite3.connect(database)) as connection:
            state, active_key = connection.execute("SELECT state,active_key FROM library_job").fetchone()
        self.assertEqual((state, active_key), ("cancelled", None))


@override_settings(BOOKMARK_CAPTURE_MODE="development")
class SupervisorStartupFailureTests(TransactionTestCase):
    databases = {"default"}

    def setUp(self):
        from runtime.models import RuntimeState
        RuntimeState.objects.using("default").create(pk=1)
        self.temp = tempfile.TemporaryDirectory()
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        self.temp.cleanup()
        super().tearDown()

    def spawn_owned_children(self, supervisor):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        for name in ("web", "worker"):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            self.children.append(child)
            supervisor.children[name] = child

    def test_readiness_failure_terminates_actual_owned_web_and_worker_processes(self):
        from supervisor import Supervisor
        supervisor = Supervisor(port=18768)
        with patch("config.paths.generation_root", return_value=Path(self.temp.name)), \
                patch.object(supervisor, "start_services", side_effect=lambda: self.spawn_owned_children(supervisor)), \
                patch.object(supervisor, "ready_services", return_value=False), \
                patch.object(supervisor, "stop_services", wraps=supervisor.stop_services) as stopped:
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                supervisor.run()
        self.assertEqual(stopped.call_count, 1)
        self.assertEqual(supervisor.children, {})
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_partial_startup_exception_still_cleans_owned_children(self):
        from supervisor import Supervisor
        supervisor = Supervisor(port=18768)
        def partial_start():
            self.spawn_owned_children(supervisor)
            raise RuntimeError("startup failed after spawn")
        with patch("config.paths.generation_root", return_value=Path(self.temp.name)), \
                patch.object(supervisor, "start_services", side_effect=partial_start):
            with self.assertRaisesRegex(RuntimeError, "startup failed after spawn"):
                supervisor.run()
        self.assertEqual(supervisor.children, {})
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_database_discovery_failure_cannot_skip_owned_process_cleanup(self):
        from supervisor import Supervisor
        supervisor = Supervisor(port=18768)
        with patch.object(supervisor, "start_services", side_effect=lambda: self.spawn_owned_children(supervisor)), \
                patch.object(supervisor, "ready_services", return_value=False), \
                patch.object(supervisor, "stop_services", side_effect=RuntimeError("database unavailable")):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                supervisor.run()
        self.assertEqual(supervisor.children, {})
        self.assertTrue(all(child.poll() is not None for child in self.children))
