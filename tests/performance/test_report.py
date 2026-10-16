import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import performance as workflow
import performance_report as reports
from tests.performance import test_inventory as inventory_tests


class PerformanceReportTests(unittest.TestCase):
    def test_percentiles_keep_missing_and_small_samples_explicit(self):
        self.assertIsNone(reports.statistics([])["p95_ms"])
        self.assertEqual(reports.statistics([100, 200, 300])["p50_ms"], 200)
        values = reports.summarize_spans([{"record_type": "span", "operation": "http.request", "duration_ms": 20}])
        self.assertEqual(values[0]["p95_sample_note"], "low_sample_count")
        self.assertEqual(values[0]["count"], 1)

    def test_nested_spans_are_not_added_into_fabricated_end_to_end_time(self):
        rows = [{"record_type": "span", "operation": "job.execute", "duration_ms": 100, "trace_id": "trace"},
            {"record_type": "span", "operation": "ai.request", "duration_ms": 80, "trace_id": "trace"}]
        result = reports.summarize_spans(rows)
        self.assertEqual({row["operation"]: row["max_ms"] for row in result}, {"job.execute": 100, "ai.request": 80})
        self.assertFalse(any("total_ms" in row for row in result))

    def test_queue_and_maintenance_events_keep_their_distinct_time_meanings(self):
        rows = [{"record_type": "event", "operation": "worker.queue_age", "queue_ms": 900, "wait_ms": 100},
            {"record_type": "event", "operation": "maintenance.stage", "stage": "compress", "duration_ms": 25},
            {"record_type": "event", "operation": "maintenance.stage", "stage": "copy", "duration_ms": 15}]
        result = reports.summarize_spans(rows)
        self.assertEqual(len(result), 3)
        queue = next(row for row in result if row["operation"] == "worker.queue_age")
        self.assertEqual(queue["p50_ms"], 900)
        self.assertEqual(queue["ready_wait"]["p50_ms"], 100)
        self.assertEqual(queue["timing_field"], "queue_ms")
        self.assertIsNone(queue["sql_count"])
        self.assertEqual({row["stage"] for row in result if row["operation"] == "maintenance.stage"}, {"compress", "copy"})

    def test_handled_job_failure_is_counted_even_when_exception_does_not_escape(self):
        result = reports.summarize_spans([{"record_type": "span", "operation": "worker.job", "duration_ms": 2, "outcome": "ok", "result_state": "handled_error"}])
        self.assertEqual(result[0]["error_count"], 1)

    def test_log_export_ignores_partial_tail_unknown_fields_and_outside_window(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perf-123.jsonl"
            rows = [{"timestamp": now.isoformat(), "operation": "http.request", "duration_ms": 7, "authorization": "SECRET", "error_code": "https://example.invalid/?token=SECRET", "sql_count": "not-a-number"},
                {"timestamp": (now - timedelta(days=2)).isoformat(), "operation": "http.request", "duration_ms": 999}]
            path.write_text("\n".join(json.dumps(row) for row in rows) + '\n{"partial":', encoding="utf-8")
            events, coverage = workflow.read_events(directory, now - timedelta(hours=1))
            self.assertEqual(len(events), 1)
            self.assertNotIn("SECRET", json.dumps(events))
            self.assertNotIn("sql_count", events[0])
            self.assertEqual(coverage["malformed_rows"], 1)
            self.assertEqual(coverage["outside_window"], 1)

    def test_rotation_race_leaves_a_report_instead_of_failing_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perf-123.jsonl"
            path.write_text("{}\n")
            original = Path.open
            def open_file(current, *args, **kwargs):
                if current == path:
                    raise FileNotFoundError("rotated")
                return original(current, *args, **kwargs)
            with patch.object(Path, "open", open_file):
                events, coverage = workflow.read_events(directory, datetime.now(timezone.utc))
            self.assertEqual(events, [])
            self.assertEqual(coverage["unreadable_files"], 1)

    def test_report_escapes_untrusted_content_and_keeps_pending_display_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run.json").write_text(json.dumps({"started_at_local": "<script>bad()</script>"}))
            (root / "browser.json").write_text(json.dumps({"status": "complete", "runs": [{"device": "mobile", "cache": "cold", "scenario": "<script>bad()</script>", "status": "complete", "preview": {
                "first_ready_ms": 50, "all_ready_ms": None, "success": 2, "available": 3, "pending": 1}}]}))
            report = reports.write_report(root)
            document = (root / "report.html").read_text(encoding="utf-8")
            self.assertIn(r"\u003cscript\u003ebad()\u003c/script\u003e", document)
            self.assertNotIn("<script>bad()", document)
            self.assertIsNone(report["browser"]["runs"][0]["preview"]["all_ready_ms"])
            tasks = json.loads((root / "tasks.json").read_text(encoding="utf-8"))["tasks"]
            pending = next(stage for stage in tasks[0]["stages"] if stage["name"] == "全部预览就绪")
            self.assertIsNone(pending["offset_ms"])
            self.assertIsNone(pending["duration_ms"])
            self.assertIn("— 表示没有记录", document)

    def test_unavailable_private_commit_is_null_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "resources.json").write_text(json.dumps({"samples": [{"sampling_ms": 1, "system": {}, "application": {
                "process_count": 1, "rss_sum_bytes": 10, "private_commit_sum_bytes": None}}]}))
            report = reports.build_report(root)
            self.assertIsNone(report["resource_summary"]["peak_application_private_commit_bytes"])

    def test_fixture_collection_does_not_touch_database_or_submit_operations(self):
        fixture = inventory_tests.InventoryTests(methodName="test_connection_rejects_writes_and_missing_database_is_not_created")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before = fixture.hashes()
        output = fixture.root.parent / "monitor-report"
        sample = {"at": datetime.now(timezone.utc).isoformat(), "elapsed_ms": 0, "sampling_ms": 1,
            "system": {"physical_available_bytes": 100, "commit_available_bytes": 200, "cpu_percent": None},
            "application": {"process_count": 1, "rss_sum_bytes": 10, "private_commit_sum_bytes": 5, "cpu_percent_of_machine": None, "processes": []}}
        with patch.object(workflow, "ResourceSampler") as sampler, patch.object(workflow.subprocess, "run", side_effect=AssertionError("No subprocess needed without browser")):
            sampler.return_value.sample.return_value = sample
            with contextlib.redirect_stdout(io.StringIO()):
                code = workflow.main(["collect", "--data-root", str(fixture.root), "--output", str(output), "--duration", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(before, fixture.hashes())
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["coverage"]["capture_stages"])
        self.assertFalse(report["coverage"]["request_and_sql_instrumentation"])
        self.assertTrue((output / "timings.csv").is_file())
