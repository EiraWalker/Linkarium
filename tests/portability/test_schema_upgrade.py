"""Restore actual released SQLite layouts using only the shipped migration chain."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime.maintenance import (MaintenanceError, create_backup, digest, schema_signature,
                                 validate_candidate, validate_package)
from runtime.schema_upgrade import TRUSTED_LIBRARY_MIGRATIONS


SEED_LEGACY_LIBRARY = '''
from datetime import timedelta
import hashlib,json,os
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from config.paths import generation_root
target=os.environ['SCHEMA_FIXTURE_MIGRATION']
apps=MigrationExecutor(connections['library']).loader.project_state([('library',target)]).apps
def create(model_name,**fields):
    return apps.get_model('library',model_name).objects.using('library').create(**fields)
now=timezone.now()
profile=create('AIProfile',name='Retained provider',base_url='https://provider.example/v1',model='legacy-model')
create('LibrarySettings',pk=1,active_ai_profile_id=profile.id)
folder=create('Folder',name='Saved folder',name_key='saved folder')
bookmark=create('Bookmark',original_url='https://example.org/saved',canonical_url='https://example.org/saved',
    domain='example.org',notes='retain previous backup 中文',title_override='Saved website',folder_id=folder.id,is_starred=True)
tag=create('Tag',name='Saved tag',name_key='saved tag')
create('BookmarkTag',bookmark_id=bookmark.id,tag_id=tag.id)
capture=create('JobGroup',kind='refresh',source='import',requested_count=2)
classification=create('JobGroup',kind='classification',source='import',requested_count=2)
fields=dict(state='running',total=3,valid=2,invalid=1)
if not target.startswith('0001'):
    fields.update(stage='captures',capture_group_id=capture.id,classification_group_id=classification.id,ai_status='running')
batch=create('ImportBatch',**fields)
create('ImportEntry',batch_id=batch.id,url=bookmark.original_url,title='Original import title',
    original_folder_path=['Source','Retained'],status='imported',bookmark_id=bookmark.id)
create('ImportEntry',batch_id=batch.id,url='https://example.org/pending',title='Pending website',status='pending')
create('ImportEntry',batch_id=batch.id,url='javascript:void(0)',title='Invalid',status='invalid',error_code='INVALID_URL')
create('BookmarkSource',bookmark_id=bookmark.id,import_batch_id=batch.id,source_folder_path=['Source','Retained'],source_title='Original import title')
def job(kind,state,target_id=None,group=None,payload=None,variant=''):
    value=create('Job',kind=kind,state=state,target_id=target_id,variant=variant,available_at=now,
        generation='initial',library_epoch=1,payload=payload or {},active_key=('fixture:'+kind) if state!='succeeded' else None)
    attempt=None
    if state in ('running','succeeded'):
        attempt=create('JobAttempt',job_id=value.id,attempt_no=1,state=state,heartbeat_at=now,
            lease_expires_at=now+timedelta(minutes=3),generation='initial',library_epoch=1,
            finished_at=now if state=='succeeded' else None)
        if state=='running':
            apps.get_model('library','Job').objects.using('library').filter(pk=value.id).update(active_attempt_id=attempt.id)
    if group:
        create('JobGroupMember',group_id=group.id,job_id=value.id,bookmark_id=target_id)
    return value,attempt
saved_job,saved_attempt=job('capture','succeeded',bookmark.id,capture,variant='desktop')
archive=b'<!doctype html><title>retained snapshot</title><p>Saved bytes</p>'
sha=hashlib.sha256(archive).hexdigest()
relative=f'artifacts/{sha[:2]}/{sha}.html'
path=generation_root()/relative
path.parent.mkdir(parents=True,exist_ok=True)
path.write_bytes(archive)
artifact=create('Artifact',sha256=sha,relative_path=relative,byte_size=len(archive))
snapshot=create('SnapshotVersion',bookmark_id=bookmark.id,artifact_id=sha,capture_attempt_id=saved_attempt.id,
    variant='desktop',captured_at=now,source_url=bookmark.original_url,final_url=bookmark.original_url,
    viewport_width=1366,viewport_height=768,capture_epoch=1)
create('CaptureState',bookmark_id=bookmark.id,variant='desktop',current_version_id=snapshot.id)
review=create('ClassificationBatch',profile_id=profile.id,job_group_id=classification.id,tree_revision=1)
proposal=create('ClassificationProposal',batch_id=review.id,bookmark_id=bookmark.id,
    expected_classification_revision=1,expected_tree_revision=1,target_path={'new_segments':['Saved review']},reason='Retain AI proposal')
owned=[]
attempts=[]
for kind,state,target_id,group,variant in (
        ('import','running',batch.id,None,''),('capture','retry_wait',bookmark.id,capture,'mobile'),
        ('classification','running',None,classification,'')):
    value,attempt=job(kind,state,target_id,group,{'import_id':str(batch.id)},variant)
    owned.append(value.id.hex)
    if attempt: attempts.append(attempt.id.hex)
unrelated,_=job('metadata','queued',bookmark.id)
if target.startswith('0003'):
    create('ClassificationTaxonomy',pk=1,categories=[{'path':['Saved AI'],'description':'Retained taxonomy'}])
fixture=dict(batch=batch.id.hex,bookmark=bookmark.id.hex,folder=folder.id.hex,profile=profile.id.hex,
    proposal=proposal.id.hex,snapshot=snapshot.id.hex,saved_attempt=saved_attempt.id.hex,
    owned_jobs=owned,owned_attempts=attempts,unrelated_job=unrelated.id.hex,artifact=relative,sha256=sha)
(generation_root()/'fixture.json').write_text(json.dumps(fixture),encoding='utf-8')
'''


class PreviousSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = Path(__file__).resolve().parents[2]
        cls.directory = tempfile.TemporaryDirectory(prefix='previous-bookmark-schema-')
        cls.root = Path(cls.directory.name)
        cls.current = cls.root / 'current'
        cls.run_command(cls.current, 'migrate', 'library', TRUSTED_LIBRARY_MIGRATIONS[-1],
                        '--database', 'library', '--noinput', '--verbosity', '0')
        cls.expected = cls.current / 'generations' / 'initial' / 'library.sqlite3'
        cls.enterClassContext(patch('config.paths.DATA_ROOT', cls.root / 'restore-runtime'))
        (cls.root / 'restore-runtime' / 'work').mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @classmethod
    def run_command(cls, data, *arguments, extra_environment=None):
        environment = dict(os.environ, BOOKMARK_DATA_ROOT=str(data))
        environment.update(extra_environment or {})
        result = subprocess.run([sys.executable, '-X', 'utf8', str(cls.project / 'backend' / 'manage.py'), *arguments],
                                env=environment, cwd=cls.project, capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr.decode('utf-8', errors='replace'))

    def old_library(self, root, migration):
        data = root / 'data'
        self.run_command(data, 'migrate', 'library', migration, '--database', 'library', '--noinput', '--verbosity', '0')
        self.run_command(data, 'shell', '--command', SEED_LEGACY_LIBRARY,
                         extra_environment={'SCHEMA_FIXTURE_MIGRATION': migration})
        generation = data / 'generations' / 'initial'
        return generation, json.loads((generation / 'fixture.json').read_text(encoding='utf-8'))

    def assert_upgraded_rows(self, generation, fixture):
        with closing(sqlite3.connect(generation / 'library.sqlite3')) as connection:
            connection.row_factory = sqlite3.Row
            bookmark = connection.execute('SELECT * FROM library_bookmark').fetchone()
            self.assertEqual((bookmark['id'], bookmark['notes'], bookmark['folder_id'], bookmark['is_starred']),
                             (fixture['bookmark'], 'retain previous backup 中文', fixture['folder'], 1))
            profile = connection.execute('SELECT * FROM library_aiprofile').fetchone()
            self.assertEqual((profile['id'], profile['base_url'], profile['model'], profile['enabled']),
                             (fixture['profile'], 'https://provider.example/v1', 'legacy-model', 1))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM library_bookmarktag').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT source_title FROM library_bookmarksource').fetchone()[0], 'Original import title')
            self.assertEqual(connection.execute('SELECT reason FROM library_classificationproposal WHERE id=?', (fixture['proposal'],)).fetchone()[0], 'Retain AI proposal')
            batch = connection.execute('SELECT * FROM library_importbatch WHERE id=?', (fixture['batch'],)).fetchone()
            self.assertEqual((batch['state'], batch['stage'], batch['capture_budget'], batch['ai_budget'],
                              batch['ai_requests_used'], batch['execution_authorization']), ('previewed', 'records', 0, 0, 0, ''))
            self.assertEqual(batch['ai_error_code'], 'IMPORT_CONFIRMATION_REQUIRED')
            self.assertIsNone(batch['capture_group_id'])
            self.assertIsNone(batch['classification_group_id'])
            for identity in fixture['owned_jobs']:
                row = connection.execute('SELECT state,error_code,active_key,active_attempt_id FROM library_job WHERE id=?', (identity,)).fetchone()
                self.assertEqual(tuple(row), ('cancelled', 'IMPORT_CONFIRMATION_REQUIRED', None, None))
            for identity in fixture['owned_attempts']:
                row = connection.execute('SELECT state,error_code,finished_at FROM library_jobattempt WHERE id=?', (identity,)).fetchone()
                self.assertEqual(tuple(row)[:2], ('cancelled', 'IMPORT_CONFIRMATION_REQUIRED'))
                self.assertIsNotNone(row['finished_at'])
            self.assertEqual(connection.execute('SELECT state FROM library_job WHERE id=?', (fixture['unrelated_job'],)).fetchone()[0], 'queued')
            self.assertEqual(connection.execute('SELECT state FROM library_jobattempt WHERE id=?', (fixture['saved_attempt'],)).fetchone()[0], 'succeeded')
            self.assertEqual(connection.execute('SELECT current_version_id FROM library_capturestate').fetchone()[0], fixture['snapshot'])
            self.assertEqual(connection.execute('SELECT count(*) FROM library_importentry').fetchone()[0], 3)
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual([row[0] for row in connection.execute("SELECT name FROM django_migrations WHERE app='library' ORDER BY name")], list(TRUSTED_LIBRARY_MIGRATIONS))
        self.assertEqual(digest(generation / fixture['artifact']), fixture['sha256'])
        self.assertEqual(schema_signature(generation / 'library.sqlite3'), schema_signature(self.expected))

    def test_each_released_schema_round_trips_to_current_with_saved_data_and_work_fenced(self):
        for index, migration in enumerate(TRUSTED_LIBRARY_MIGRATIONS[:-1]):
            with self.subTest(migration=migration), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                generation, fixture = self.old_library(root, migration)
                package = root / 'previous.bookmarklib.zip'
                create_backup(generation, package)
                source_hash = digest(generation / 'library.sqlite3')
                candidate = root / 'candidate'
                expected = self.expected if index != 1 else schema_signature(self.expected)
                manifest = validate_package(package, candidate, expected_schema=expected)
                self.assertEqual(manifest['files']['library.sqlite3']['sha256'], digest(candidate / 'library.sqlite3'))
                self.assertNotEqual(source_hash, digest(candidate / 'library.sqlite3'))
                self.assertEqual(digest(generation / 'library.sqlite3'), source_hash)
                self.assert_upgraded_rows(candidate, fixture)
                roundtrip = root / 'current.bookmarklib.zip'
                create_backup(candidate, roundtrip, expected_schema=self.expected)
                restored = root / 'roundtrip'
                validate_package(roundtrip, restored, expected_schema=self.expected)
                self.assert_upgraded_rows(restored, fixture)
                validate_candidate(restored, expected_schema=self.expected, prepare=True)
                with closing(sqlite3.connect(restored / 'library.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT enabled FROM library_aiprofile').fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM library_job WHERE state IN ('queued','running','retry_wait')").fetchone()[0], 0)

    def test_forged_migration_rows_cannot_skip_real_schema_upgrade(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            generation, fixture = self.old_library(root, TRUSTED_LIBRARY_MIGRATIONS[2])
            with closing(sqlite3.connect(generation / 'library.sqlite3')) as connection:
                connection.execute('DELETE FROM django_migrations')
                connection.execute("INSERT INTO django_migrations(app,name,applied) VALUES('library',?,CURRENT_TIMESTAMP)", (TRUSTED_LIBRARY_MIGRATIONS[-1],))
                connection.execute("INSERT INTO django_migrations(app,name,applied) VALUES('untrusted','9999_skip_checks',CURRENT_TIMESTAMP)")
                connection.commit()
            package = root / 'forged-history.bookmarklib.zip'
            create_backup(generation, package)
            candidate = root / 'candidate'
            validate_package(package, candidate, expected_schema=schema_signature(self.expected))
            self.assert_upgraded_rows(candidate, fixture)
            with closing(sqlite3.connect(candidate / 'library.sqlite3')) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM django_migrations WHERE app<>'library'").fetchone()[0], 0)

    def test_unknown_or_partially_current_layouts_are_rejected_without_changing_source(self):
        changes = ('CREATE INDEX library_unrecognized ON library_bookmark(notes)',
                   "ALTER TABLE library_importbatch ADD COLUMN execution_authorization TEXT NOT NULL DEFAULT ''")
        for sql in changes:
            with self.subTest(sql=sql), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                generation, _ = self.old_library(root, TRUSTED_LIBRARY_MIGRATIONS[2])
                with closing(sqlite3.connect(generation / 'library.sqlite3')) as connection:
                    connection.execute(sql)
                    connection.commit()
                package = root / 'unknown-layout.bookmarklib.zip'
                create_backup(generation, package)
                source_hash = digest(generation / 'library.sqlite3')
                candidate = root / 'candidate'
                with self.assertRaises(MaintenanceError) as caught:
                    validate_package(package, candidate, expected_schema=self.expected)
                self.assertEqual(caught.exception.code, 'BACKUP_SCHEMA_INCOMPATIBLE')
                self.assertEqual(digest(generation / 'library.sqlite3'), source_hash)
                self.assertEqual(list(candidate.iterdir()), [])
