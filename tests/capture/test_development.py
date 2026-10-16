import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from capture.development import _run_capture_process, run_development_capture
from capture.models import CaptureError, CaptureRequest


class DevelopmentLifetimeTests(unittest.TestCase):
    def test_cancelled_attempt_terminates_its_running_child(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            with self.assertRaises(CaptureError) as caught:
                _run_capture_process([sys.executable, "-c", "import time;time.sleep(30)"], Path(directory),
                    10, lambda: time.monotonic() - started < 0.3)
            self.assertEqual(caught.exception.code, "cancelled")
            self.assertLess(time.monotonic() - started, 5)

    def test_total_timeout_terminates_spawned_child_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "grandchild.pid"
            child_code = "import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(30)"
            with self.assertRaises(CaptureError) as caught:
                _run_capture_process([sys.executable, "-c", child_code, str(marker)], Path(directory), 1.5, lambda: True)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertTrue(marker.exists())
            grandchild_id = int(marker.read_text())
            if os.name == "nt":
                import ctypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.restype = ctypes.c_void_p
                handle = kernel.OpenProcess(0x100000, False, grandchild_id)
                if handle:
                    try:
                        self.assertEqual(kernel.WaitForSingleObject(ctypes.c_void_p(handle), 2000), 0)
                    finally:
                        kernel.CloseHandle(ctypes.c_void_p(handle))
            else:
                status = Path(f"/proc/{grandchild_id}/stat")
                self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")

    @unittest.skipUnless(os.environ.get("RUN_CAPTURE_INTEGRATION") == "1", "Requires installed Chromium and Monolith")
    def test_real_cli_process_returns_rendered_monolith_html(self):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = b'<!doctype html><html><head><title>CLI lifetime fixture</title></head><body><script>document.body.innerHTML="Rendered by JavaScript"</script></body></html>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(attempt_uuid=str(uuid.uuid4()), generation="test", library_epoch=1,
                    url=f"http://127.0.0.1:{server.server_port}/", output_dir=directory,
                    development=True, allow_test_hosts=("127.0.0.1",))
                result = run_development_capture(request, lambda: True)
                self.assertIn("Rendered by JavaScript", Path(result.artifact_path).read_text(encoding="utf-8"))
                self.assertFalse((Path(directory) / "runner-request.json").exists())
                self.assertFalse((Path(directory) / "runner-result.json").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
