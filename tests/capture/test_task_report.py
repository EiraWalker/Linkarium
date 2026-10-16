import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4
from scripts.diagnose_tasks import collect


class TaskReportTests(unittest.TestCase):
    def test_report_is_scoped_to_selected_group_and_does_not_expose_bookmark_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root/'state.sqlite3') as db:
                db.execute('CREATE TABLE runtime_runtimestate (id,active_generation,library_epoch,worker_heartbeat)')
                db.execute("INSERT INTO runtime_runtimestate VALUES (1,'test',1,'today')")
            db.close()
            library = root/'generations'/'test'/'library.sqlite3'
            library.parent.mkdir(parents=True)
            selected, other, job, bookmark, attempt = [uuid4().hex for _ in range(5)]
            with sqlite3.connect(library) as db:
                db.executescript('''
                    CREATE TABLE library_jobgroup (id,kind,created_at);
                    CREATE TABLE library_job (id,kind,target_id,variant,state,error_code,attempt_count,available_at);
                    CREATE TABLE library_jobgroupmember (group_id,job_id);
                    CREATE TABLE library_bookmark (id,canonical_url);
                    CREATE TABLE library_jobattempt (id,job_id,attempt_no,state,error_code,created_at,finished_at,heartbeat_at,lease_expires_at);
                ''')
                db.executemany('INSERT INTO library_jobgroup VALUES (?, ?, ?)', [(selected,'refresh','2'), (other,'refresh','1')])
                db.execute('INSERT INTO library_bookmark VALUES (?, ?)', (bookmark,'https://user:SECRET@example.com/SECRET?key=SECRET'))
                db.execute('INSERT INTO library_job VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (job,'capture',bookmark,'desktop','failed','CAPTURE_TIMEOUT',1,'now'))
                db.execute('INSERT INTO library_jobgroupmember VALUES (?,?)', (selected,job))
                db.execute('INSERT INTO library_jobattempt VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (attempt,job,1,'failed','CAPTURE_TIMEOUT','start','end','beat','lease'))
            db.close()
            before = library.read_bytes()
            report = collect(root)
            self.assertEqual(report['group']['id'], selected)
            self.assertEqual(report['errors'], {'CAPTURE_TIMEOUT': 1})
            self.assertEqual(report['failed_jobs'][0]['host'], 'example.com')
            self.assertFalse(report['failed_jobs'][0]['attempts'][0]['diagnostic_available'])
            self.assertNotIn('SECRET', json.dumps(report))
            self.assertEqual(collect(root, other)['total_device_jobs'], 0)
            self.assertEqual(library.read_bytes(), before)
