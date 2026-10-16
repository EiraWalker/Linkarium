"""Parent process failures retain their capture code and bounded diagnostic evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from capture.development import _run_capture_process, run_development_capture
from capture.models import CaptureError, CaptureRequest


class CaptureLifetimeErrorTests(unittest.TestCase):
    def request(self, root):
        attempt = str(uuid.uuid4())
        return CaptureRequest(attempt_uuid=attempt, generation="test", library_epoch=1,
            url="https://example.com/private?token=NEVER_STORE_THIS", development=True,
            output_dir=str(Path(root) / "work" / "capture" / attempt))

    def test_watchdog_timeout_survives_termination_failure(self):
        child = Mock()
        child.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            with patch("capture.development.subprocess.Popen", return_value=child), \
                    patch("capture.development._terminate", side_effect=subprocess.TimeoutExpired("taskkill", 10)):
                with self.assertRaises(CaptureError) as caught:
                    _run_capture_process(["fixture"], Path(directory), 0, lambda: True)
        self.assertEqual(caught.exception.code, "timeout")

    def test_child_timeout_survives_result_cleanup_failure(self):
        original_unlink = Path.unlink
        def refuse_result_cleanup(path, *args, **kwargs):
            if path.name == "runner-result.json":
                raise PermissionError("NEVER_STORE_THIS")
            return original_unlink(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(Path, "unlink", refuse_result_cleanup):
                with self.assertRaises(CaptureError) as caught:
                    _run_capture_process([sys.executable, "-c",
                        'print(\'{"ok":false,"error":{"code":"timeout"}}\')'],
                        Path(directory), 10, lambda: True)
        self.assertEqual(caught.exception.code, "timeout")

    def test_wrapper_cleanup_does_not_replace_timeout_and_preserves_child_trace(self):
        original_unlink = Path.unlink
        def refuse_request_cleanup(path, *args, **kwargs):
            if path.name == "runner-request.json":
                raise PermissionError("https://user:password@example.com/?token=NEVER_STORE_THIS")
            return original_unlink(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory)
            trace = Path(directory) / "diagnostics" / "capture" / (request.attempt_uuid + ".json")
            trace.parent.mkdir(parents=True)
            trace.write_text(json.dumps({"attempt_id": request.attempt_uuid, "state": "running",
                "stage": "freeze", "events": [{"stage": "freeze"}]}), encoding="utf-8")
            with patch("capture.development._run_capture_process",
                    side_effect=CaptureError("timeout", "Capture process exceeded its total time budget")), \
                    patch.object(Path, "unlink", refuse_request_cleanup):
                with self.assertRaises(CaptureError) as caught:
                    run_development_capture(request, lambda: True)
            self.assertEqual(caught.exception.code, "timeout")
            body = json.loads(trace.read_text(encoding="utf-8"))
            self.assertEqual(body["state"], "terminated_by_parent")
            self.assertEqual(body["stage"], "freeze")
            self.assertEqual(body["events"], [{"stage": "freeze"}])
            self.assertEqual(body["parent_failure"][0]["code"], "timeout")
            self.assertEqual(body["cleanup_errors"][0][0]["type"], "PermissionError")
            self.assertNotIn("NEVER_STORE_THIS", trace.read_text(encoding="utf-8"))
            self.assertNotIn("password", trace.read_text(encoding="utf-8"))

    def test_parent_failure_before_child_start_creates_redacted_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory)
            with patch("capture.development._run_capture_process",
                    side_effect=CaptureError("timeout", "Capture process exceeded its total time budget")):
                with self.assertRaises(CaptureError):
                    run_development_capture(request, lambda: True)
            trace = Path(directory) / "diagnostics" / "capture" / (request.attempt_uuid + ".json")
            body = json.loads(trace.read_text(encoding="utf-8"))
            self.assertEqual(body["parent_failure"][0]["code"], "timeout")
            self.assertEqual(body["host"], "example.com")
            self.assertNotIn("NEVER_STORE_THIS", trace.read_text(encoding="utf-8"))

    def test_malformed_child_error_is_a_capture_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CaptureError) as caught:
                _run_capture_process([sys.executable, "-c", 'print(\'{"ok":false,"error":null}\')'],
                    Path(directory), 10, lambda: True)
        self.assertEqual(caught.exception.code, "invalid_result")

    def test_failed_diagnostic_write_never_replaces_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory)
            with patch("capture.development._run_capture_process",
                    side_effect=CaptureError("timeout", "Capture process exceeded its total time budget")), \
                    patch("capture.development.development_trace_path", side_effect=PermissionError("denied")):
                with self.assertRaises(CaptureError) as caught:
                    run_development_capture(request, lambda: True)
            self.assertEqual(caught.exception.code, "timeout")

    def test_completed_child_with_invalid_result_records_parent_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory)
            trace = Path(directory) / "diagnostics" / "capture" / (request.attempt_uuid + ".json")
            trace.parent.mkdir(parents=True)
            trace.write_text(json.dumps({"attempt_id": request.attempt_uuid, "state": "succeeded",
                "stage": "replay_check", "events": []}), encoding="utf-8")
            with patch("capture.development._run_capture_process", return_value={"bad_key": True}):
                with self.assertRaises(CaptureError) as caught:
                    run_development_capture(request, lambda: True)
            self.assertEqual(caught.exception.code, "browser_failed")
            body = json.loads(trace.read_text(encoding="utf-8"))
            self.assertEqual(body["state"], "failed")
            self.assertEqual(body["child_state"], "succeeded")
            self.assertEqual(body["stage"], "replay_check")

    def test_existing_request_and_result_are_not_removed_when_not_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory)
            output = Path(request.output_dir)
            output.mkdir(parents=True)
            request_path = output / "runner-request.json"
            result_path = output / "runner-result.json"
            request_path.write_text("existing request", encoding="utf-8")
            result_path.write_text("existing result", encoding="utf-8")
            with self.assertRaises(CaptureError):
                run_development_capture(request, lambda: True)
            self.assertEqual(request_path.read_text(encoding="utf-8"), "existing request")
            self.assertEqual(result_path.read_text(encoding="utf-8"), "existing result")


if __name__ == "__main__":
    unittest.main()
