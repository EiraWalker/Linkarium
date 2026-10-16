"""Monitoring observes real business boundaries without retaining user input."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import uuid

from django.db import connection
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from config.middleware import PerformanceMiddleware, ResponsePolicyMiddleware
from runtime import performance


class PerformanceTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bookmark-performance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = patch.dict(os.environ, {"BOOKMARK_PERF_DIR": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        switch = patch.object(performance, "_ENABLED", True)
        switch.start()
        self.addCleanup(switch.stop)

    def records(self):
        return [json.loads(line) for path in self.root.glob("*.jsonl") for line in path.read_text().splitlines()]

    def test_nested_spans_inherit_correlation_without_leaking_dynamic_fields(self):
        job_id = str(uuid.uuid4())
        with performance.trace("worker.job", job_id=job_id, kind="capture", url="https://secret.invalid/?token=SECRET") as parent:
            with performance.span("capture.render", response_bytes=42, error="SECRET") as child:
                performance.emit("capture.ready", count=3, headers={"Authorization": "SECRET"})
        event, inner, outer = self.records()
        self.assertEqual(inner["parent_span_id"], outer["span_id"])
        self.assertEqual(event["parent_span_id"], child.span_id)
        self.assertTrue(all(row["trace_id"] == parent.trace_id for row in self.records()))
        self.assertTrue(all(row["job_id"] == job_id for row in self.records()))
        self.assertGreaterEqual(inner["duration_ms"], 0)
        self.assertNotIn("SECRET", json.dumps(self.records()))
        self.assertIsNone(performance._CURRENT.get())

    def test_exception_identity_and_business_propagation_without_message(self):
        with self.assertRaisesRegex(ValueError, "SECRET"):
            with performance.span("test.failure"):
                raise ValueError("SECRET authentication token")
        record = self.records()[0]
        self.assertEqual(record["exception_type"], "ValueError")
        self.assertEqual(record["outcome"], "error")
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertIsNone(performance._CURRENT.get())

    def test_log_io_failure_never_changes_success_or_business_exception(self):
        with patch.object(performance, "_write", side_effect=OSError("disk full")):
            with performance.span("test.success"):
                performance.emit("test.event")
            with self.assertRaisesRegex(RuntimeError, "business"):
                with performance.trace("test.failure"):
                    raise RuntimeError("business")
        self.assertIsNone(performance._CURRENT.get())

    def test_disabled_mode_does_not_read_clocks_context_or_storage(self):
        with patch.object(performance, "_ENABLED", False), \
                patch.object(performance, "_write", side_effect=AssertionError("write")), \
                patch.object(performance.time, "perf_counter", side_effect=AssertionError("clock")), \
                patch.object(performance, "_CURRENT") as current:
            with performance.trace("ignored"):
                with performance.span("ignored"):
                    performance.emit("ignored", count=1)
            current.get.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_rotation_stays_bounded_and_all_lines_are_json(self):
        with patch.dict(os.environ, {"BOOKMARK_PERF_MAX_BYTES": "65536"}):
            for index in range(1500):
                performance.emit("rotation.sample", count=index, category="x" * 96)
        files = list(self.root.iterdir())
        self.assertLessEqual(len(files), 4)
        self.assertTrue(all(path.stat().st_size <= 65536 for path in files))
        for path in files:
            for line in path.read_text().splitlines():
                self.assertEqual(json.loads(line)["operation"], "rotation.sample")

    def test_fields_drop_urls_containers_nonfinite_numbers_and_unknown_ids(self):
        performance.emit("privacy.sample", job_id="SECRET", category="https://secret.invalid/?token=SECRET",
            duration_ms=float("nan"), count=float("inf"), status="ok", body="SECRET", params=["SECRET"])
        record = self.records()[0]
        self.assertEqual(record["status"], "ok")
        self.assertNotIn("job_id", record)
        self.assertNotIn("duration_ms", record)
        self.assertNotIn("SECRET", json.dumps(record))

    def test_capture_child_can_import_without_django_settings(self):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        result = subprocess.run([sys.executable, "-c", "import sys; from runtime import performance; assert 'django' not in sys.modules; print('stdlib-only')"],
            env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdlib-only", result.stdout)


class PerformanceMiddlewareTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bookmark-performance-http-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"BOOKMARK_PERF_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        switch = patch.object(performance, "_ENABLED", True)
        switch.start()
        self.addCleanup(switch.stop)
        self.factory = RequestFactory()

    def records(self):
        return [json.loads(line) for path in self.root.glob("*.jsonl") for line in path.read_text().splitlines()]

    def test_request_sql_headers_and_template_preserve_no_sql_or_query(self):
        def view(request):
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s", ["SECRET SQL PARAM"])
                self.assertEqual(cursor.fetchone()[0], "SECRET SQL PARAM")
            return HttpResponse("abc")
        middleware = PerformanceMiddleware(ResponsePolicyMiddleware(view))
        request = self.factory.get(f"/api/v1/snapshots/{uuid.uuid4()}/thumbnail?password=SECRET")
        request.META["HTTP_AUTHORIZATION"] = "Bearer SECRET"
        response = middleware(request)
        self.assertEqual(response.content, b"abc")
        record = self.records()[0]
        self.assertEqual(record["route"], "/api/v1/snapshots/:id/thumbnail")
        self.assertEqual(record["sql_count"], 1)
        self.assertGreaterEqual(record["sql_ms"], 0)
        self.assertEqual(response["X-Request-ID"], record["request_id"])
        self.assertEqual(request.request_id, record["request_id"])
        self.assertIn("app;dur=", response["Server-Timing"])
        self.assertIn("sql;dur=", response["Server-Timing"])
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertNotIn("SELECT", json.dumps(record))

    def test_unknown_path_is_never_retained(self):
        for path in ("/SECRET-TOKEN", "/api/v1/SECRET-TOKEN", "/assets/SECRET-TOKEN.js"):
            PerformanceMiddleware(lambda request: HttpResponse(""))(self.factory.get(path))
        self.assertNotIn("SECRET", json.dumps(self.records()))
        self.assertEqual([r["route"] for r in self.records()], [":unmatched", "/api/v1/:unmatched", "/assets/:asset"])

    def test_stream_is_lazy_with_separate_duration_sql_and_request_trace(self):
        consumed = []
        def body():
            consumed.append(True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            yield b"abc"
            yield b"de"
        response = PerformanceMiddleware(lambda request: StreamingHttpResponse(body()))(self.factory.get("/library"))
        self.assertEqual(consumed, [])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["sql_count"], 0)
        self.assertEqual(b"".join(response.streaming_content), b"abcde")
        request, stream = self.records()
        self.assertEqual(stream["operation"], "http.stream")
        self.assertEqual(stream["trace_id"], request["trace_id"])
        self.assertEqual(stream["parent_span_id"], request["span_id"])
        self.assertEqual(stream["response_bytes"], 5)
        self.assertEqual(stream["sql_count"], 1)
        self.assertIsNone(performance._CURRENT.get())

    def test_stream_failure_propagates_and_records_only_class(self):
        def body():
            yield b"abc"
            raise ValueError("SECRET STREAM ERROR")
        response = PerformanceMiddleware(lambda request: StreamingHttpResponse(body()))(self.factory.get("/library"))
        with self.assertRaises(ValueError):
            b"".join(response.streaming_content)
        record = self.records()[1]
        self.assertEqual(record["exception_type"], "ValueError")
        self.assertEqual(record["response_bytes"], 3)
        self.assertNotIn("SECRET", json.dumps(record))

    def test_async_stream_remains_async_and_lazy(self):
        consumed = []
        async def body():
            consumed.append(True)
            yield b"abc"
        response = PerformanceMiddleware(lambda request: StreamingHttpResponse(body()))(self.factory.get("/library"))
        self.assertTrue(response.is_async)
        self.assertEqual(consumed, [])
        async def consume():
            return b"".join([chunk async for chunk in response.streaming_content])
        self.assertEqual(asyncio.run(consume()), b"abc")
        self.assertEqual(self.records()[1]["response_bytes"], 3)

    def test_broken_log_destination_does_not_change_http_body(self):
        with patch.object(performance, "_write", side_effect=PermissionError("denied")):
            response = PerformanceMiddleware(lambda request: HttpResponse("business result"))(self.factory.get("/library"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"business result")

    def test_disabled_middleware_installs_no_sql_wrappers_or_headers(self):
        with patch.object(performance, "_ENABLED", False), \
                patch("config.middleware._performance_sql", side_effect=AssertionError("SQL wrapper")):
            response = PerformanceMiddleware(lambda request: HttpResponse("abc"))(self.factory.get("/library"))
        self.assertNotIn("Server-Timing", response)
        self.assertEqual(self.records(), [])

    def test_clock_collection_failure_never_prevents_query_execution(self):
        from config.middleware import _performance_sql
        totals = {"sql_count": 0, "sql_ms": 0.0}
        with _performance_sql(totals):
            with patch("time.perf_counter", side_effect=RuntimeError("clock unavailable")):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 37")
                    self.assertEqual(cursor.fetchone()[0], 37)
