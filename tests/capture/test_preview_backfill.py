"""Offline first-screen backfill reuses archives without visiting their origins."""
import os
from pathlib import Path
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from capture.preview import validate_preview
from library.preview_render import render


@unittest.skipUnless(os.environ.get("RUN_CAPTURE_INTEGRATION") == "1", "Real Chromium integration")
class PreviewBackfillTests(unittest.TestCase):
    def test_saved_archive_renders_both_viewports_without_running_scripts_or_loading_external_urls(self):
        hits = []
        class Server(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"external resource")
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "archive.html"
                source.write_text(f'''<!doctype html><body style="background:#69b">
                    <h1>Saved archive</h1><script>fetch('http://127.0.0.1:{server.server_port}/script')</script>
                    <img src="http://127.0.0.1:{server.server_port}/image">
                    <div style="height:3000px">Content below the first screen stays in HTML.</div></body>''', encoding="utf-8")
                original = source.read_bytes()
                for width, height in ((1366, 768), (390, 844)):
                    output = Path(directory) / f"{width}.jpg"
                    render(source, output, width, height)
                    details = validate_preview(output, width=width, height=height)
                    self.assertGreater(details["byte_size"], 1000)
                    self.assertEqual(source.read_bytes(), original)
                self.assertEqual(hits, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
