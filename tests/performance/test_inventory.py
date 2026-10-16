"""Inventory proves provenance and read-only behavior using local SQLite fixtures."""
import hashlib
import importlib.util
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("performance_inventory", ROOT / "scripts" / "performance_inventory.py")
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)

JOB = "10000000000000000000000000000001"
ATTEMPT = "20000000000000000000000000000001"
GROUP = "30000000000000000000000000000001"
VERSION = "40000000000000000000000000000001"
DIGEST = "a" * 64
START = "2026-10-15T00:00:00+00:00"
CLAIM = "2026-10-15T00:00:02+00:00"
FINISH = "2026-10-15T00:00:07+00:00"
SECRET = "SECRET_TOKEN_DO_NOT_EXPORT"


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.generation = self.root / "generations" / "fixture"
        self.generation.mkdir(parents=True)
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as db, db:
            db.executescript("""
                CREATE TABLE runtime_runtimestate(id INTEGER, active_generation TEXT, library_epoch INTEGER, maintenance_mode INTEGER, worker_heartbeat TEXT);
                CREATE TABLE runtime_maintenanceoperation(id TEXT,kind TEXT,state TEXT,stage TEXT,created_at TEXT,updated_at TEXT,source_package TEXT);
                INSERT INTO runtime_runtimestate VALUES(1,'fixture',1,0,NULL);
            """)
            db.execute("INSERT INTO runtime_maintenanceoperation VALUES(?,?,?,?,?,?,?)", ("maintenance", "backup", "succeeded", "completed", START, START, SECRET))
        with closing(sqlite3.connect(self.generation / "library.sqlite3")) as db, db:
            db.executescript("""
                CREATE TABLE library_job(id TEXT,kind TEXT,variant TEXT,state TEXT,available_at TEXT,created_at TEXT,attempt_count INTEGER,active_attempt_id TEXT,payload TEXT);
                CREATE TABLE library_jobattempt(id TEXT,job_id TEXT,attempt_no INTEGER,state TEXT,created_at TEXT,finished_at TEXT,heartbeat_at TEXT,lease_expires_at TEXT);
                CREATE TABLE library_jobgroup(id TEXT,kind TEXT,requested_count INTEGER,created_at TEXT);
                CREATE TABLE library_jobgroupmember(group_id TEXT,job_id TEXT);
                CREATE TABLE library_importbatch(id TEXT,state TEXT,stage TEXT,created_at TEXT,total INTEGER,valid INTEGER,capture_group_id TEXT,classification_group_id TEXT,ai_status TEXT);
                CREATE TABLE library_snapshotversion(id TEXT,variant TEXT,artifact_id TEXT,viewport_width INTEGER,viewport_height INTEGER,captured_at TEXT);
                CREATE TABLE library_artifact(sha256 TEXT,byte_size INTEGER);
                CREATE TABLE library_capturestate(variant TEXT,current_version_id TEXT,failure_count INTEGER,last_error_code TEXT);
                CREATE TABLE library_bookmark(id TEXT,canonical_url TEXT);
                CREATE TABLE library_folder(id TEXT);
            """)
            db.execute("INSERT INTO library_job VALUES(?,?,?,?,?,?,?,?,?)", (JOB, "capture", "desktop", "succeeded", START, START, 1, ATTEMPT, SECRET))
            db.execute("INSERT INTO library_jobattempt VALUES(?,?,?,?,?,?,?,?)", (ATTEMPT, JOB, 1, "succeeded", CLAIM, FINISH, CLAIM, FINISH))
            db.execute("INSERT INTO library_jobgroup VALUES(?,?,?,?)", (GROUP, "refresh", 1, START))
            db.execute("INSERT INTO library_jobgroupmember VALUES(?,?)", (GROUP, JOB))
            db.execute("INSERT INTO library_importbatch VALUES(?,?,?,?,?,?,?,?,?)", ("import", "completed", "done", START, 1, 1, GROUP, None, "unavailable"))
            db.execute("INSERT INTO library_snapshotversion VALUES(?,?,?,?,?,?)", (VERSION, "desktop", DIGEST, 1366, 768, FINISH))
            db.execute("INSERT INTO library_artifact VALUES(?,?)", (DIGEST, 4))
            db.execute("INSERT INTO library_capturestate VALUES(?,?,?,?)", ("desktop", VERSION, 0, ""))
            db.execute("INSERT INTO library_bookmark VALUES(?,?)", ("bookmark", "https://example.test/?token=" + SECRET))
        previews = self.generation / "previews" / "v1"
        previews.mkdir(parents=True)
        (previews / f"{DIGEST}-desktop-1366x768.jpg").write_bytes(b"fixture")
        diagnostics = self.root / "diagnostics" / "capture"
        diagnostics.mkdir(parents=True)
        (diagnostics / "20000000-0000-0000-0000-000000000001.json").write_text(json.dumps({
            "state": "succeeded", "variant": "desktop", "host": SECRET, "started_at": CLAIM,
            "elapsed_seconds": 5, "timeout_seconds": 120, "max_bytes": 96 * 1024**2,
            "events": [{"stage": "navigation", "elapsed_seconds": 0}, {"stage": "archive", "elapsed_seconds": 3}],
            "errors": [{"message": SECRET}],
        }), encoding="utf-8")

    def hashes(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_inventory_has_real_duration_provenance_without_mutating_files_or_exporting_secrets(self):
        before = self.hashes()
        result = inventory.collect_inventory(self.root)
        self.assertEqual(before, self.hashes())
        self.assertEqual(result["attempts"]["execution"]["median_ms"], 5000)
        self.assertEqual(result["attempts"]["initial_queue"]["median_ms"], 2000)
        self.assertEqual(result["groups"]["details"][0]["wall_ms"], 7000)
        self.assertTrue(result["imports"]["details"][0]["duration_missing"])
        self.assertIsNone(result["maintenance"]["details"][0]["duration_ms"])
        self.assertEqual(result["snapshots"]["current_with_preview"], 1)
        self.assertEqual(result["capture_diagnostics"]["linked_attempts"], 1)
        self.assertEqual(result["capture_diagnostics"]["marker_interval_by_stage"]["navigation"]["median_ms"], 3000)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_connection_rejects_writes_and_missing_database_is_not_created(self):
        with inventory.read_database(self.root / "state.sqlite3") as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE runtime_runtimestate SET library_epoch=99")
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            with inventory.read_database(missing):
                pass
        self.assertFalse(missing.exists())

    def test_cancelled_job_does_not_reuse_previous_failed_attempt_as_cancellation_time(self):
        with closing(sqlite3.connect(self.generation / "library.sqlite3")) as db, db:
            db.execute("UPDATE library_job SET state='cancelled'")
            db.execute("UPDATE library_jobattempt SET state='failed'")
        result = inventory.collect_inventory(self.root)
        self.assertIsNone(result["groups"]["details"][0]["wall_ms"])
        self.assertTrue(result["groups"]["details"][0]["duration_missing"])

    def test_running_trace_last_interval_is_unknown_instead_of_zero(self):
        path = next((self.root / "diagnostics" / "capture").glob("*.json"))
        trace = json.loads(path.read_text("utf-8"))
        trace["state"] = "running"
        path.write_text(json.dumps(trace), encoding="utf-8")
        result = inventory.collect_inventory(self.root)
        detail = result["capture_diagnostics"]["details"][0]
        self.assertTrue(detail["incomplete"])
        self.assertIsNone(detail["segments"][-1]["duration_ms"])


if __name__ == "__main__":
    unittest.main()
