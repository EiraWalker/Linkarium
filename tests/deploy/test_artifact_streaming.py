"""Real loopback HTTP integrity regression, not an authentication acceptance test.

Run with an explicit fixture BOOKMARK_DATA_ROOT and DJANGO_SECRET_KEY, or via
Django's test runner. Identity, ORM and runtime lookups are fixtures; API dispatch,
artifact reads, StreamingHttpResponse, Waitress and http.client are real.
"""
from contextlib import ExitStack
import gzip
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def probe_http(cases):
    import django
    from django.apps import apps
    if not apps.ready:
        if not os.environ.get("BOOKMARK_DATA_ROOT") or not os.environ.get("DJANGO_SECRET_KEY"):
            raise RuntimeError("HTTP probe requires explicit isolated fixture settings")
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
    from django.core.handlers.wsgi import WSGIRequest
    from waitress import create_server, wasyncore
    from config import api
    from library import artifact_storage

    rows, readers, versions = [], [], {}
    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(self.format(record))

    def application(environ, start_response):
        request = WSGIRequest(environ)
        request.user = SimpleNamespace(is_authenticated=True, username="owner")
        route = environ["PATH_INFO"].removeprefix("/api/v1/")
        response = api.dispatch(request, route)
        start_response(f"{response.status_code} {response.reason_phrase}", list(response.items()))
        return response

    original_open = artifact_storage.open_artifact

    def recorded_open(*args, **kwargs):
        reader = original_open(*args, **kwargs)
        readers.append(reader)
        return reader

    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        state = SimpleNamespace(active_generation="initial", maintenance_mode=False)
        stack.enter_context(patch("library.artifacts.generation_root", side_effect=lambda *args: root))
        stack.enter_context(patch("library.local_ai.authenticate_device", return_value=None))
        stack.enter_context(patch("library.queue.runtime_state", return_value=state))
        stack.enter_context(patch("library.catalog.get_entity", side_effect=lambda model, key: versions[key]))
        stack.enter_context(patch("config.api.open_artifact", side_effect=recorded_open))
        logger = logging.getLogger("waitress")
        stack.enter_context(patch.object(logger, "handlers", [Capture()]))
        stack.enter_context(patch.object(logger, "propagate", False))
        socket_map = {}
        server = create_server(application, host="127.0.0.1", port=0, threads=1,
                               map=socket_map, asyncore_loop_timeout=0.05)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for index, (encoding, corruption, length) in enumerate(cases):
                data = b"a" * length
                digest = hashlib.sha256(data).hexdigest()
                canonical = root / "artifacts" / digest[:2] / (digest + ".html")
                canonical.parent.mkdir(parents=True, exist_ok=True)
                physical = canonical if encoding == "raw" else Path(str(canonical) + ".gz")
                logical = (b"b" * length if corruption == "wrong-sha" else
                           data + b"!" if corruption == "overlimit" else
                           data[:-1] if corruption == "short" else data)
                payload = logical if encoding == "raw" else gzip.compress(logical, mtime=0)
                if corruption == "truncated-footer":
                    payload = payload[:-5]
                elif corruption == "crc":
                    payload = bytearray(payload)
                    payload[-8] ^= 1
                physical.write_bytes(payload)
                key = str(index)
                versions[key] = SimpleNamespace(artifact_id=digest, artifact=SimpleNamespace(
                    byte_size=length, relative_path=f"artifacts/{digest[:2]}/{digest}.html"))
                row = {"encoding": encoding, "corruption": corruption, "logical_size": length}
                connection = http.client.HTTPConnection("127.0.0.1", server.effective_port, timeout=5)
                try:
                    connection.request("GET", f"/api/v1/snapshots/{key}/preview", headers={"Connection": "close"})
                    response = connection.getresponse()
                    row.update(status=response.status, content_length=response.getheader("Content-Length"),
                               content_type=response.getheader("Content-Type"),
                               content_encoding=response.getheader("Content-Encoding"))
                    try:
                        body = response.read()
                        row.update(client_success=True, body_size=len(body), body_matches_original=body == data)
                    except http.client.IncompleteRead as error:
                        row.update(client_success=False, client_error=type(error).__name__,
                                   partial_size=len(error.partial))
                finally:
                    connection.close()
                rows.append(row)
                # Subsequent encodings of the same identity must not prefer an old raw fixture.
                # Closing the client does not synchronously close the server's file on Windows;
                # keep distinct identity directories by serving the next case from a fresh root.
                root = Path(directory) / str(index + 1)
        finally:
            server.task_dispatcher.shutdown()
            server.close()
            wasyncore.close_all(socket_map)
            thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError("Loopback Waitress thread did not stop")
        if len(readers) != len(rows):
            raise AssertionError("A request did not reach the actual artifact reader")
        for index, (row, reader) in enumerate(zip(rows, readers)):
            row["server_errors"] = [message for message in messages
                                    if f"/api/v1/snapshots/{index}/preview" in message]
            row.update(reader_verified=reader._verified,
                       reader_closed=reader.closed and reader._file.closed and reader._stream.closed)
    return rows


def http_cases():
    for length in (0, 1, 65535, 65536, 65537, 131072):
        for encoding in ("raw", "gzip"):
            yield encoding, "none", length
            # Empty-content identity cannot have a different same-length payload.
            corruptions = ("overlimit",) if length == 0 else ("wrong-sha", "overlimit", "short")
            if encoding == "gzip":
                corruptions += ("truncated-footer", "crc")
            for corruption in corruptions:
                yield encoding, corruption, length


class ArtifactStreamingHTTPTests(unittest.TestCase):
    def test_valid_lengths_and_corruption_over_real_http(self):
        rows = probe_http(http_cases())
        report = os.environ.get("ARTIFACT_HTTP_REPORT")
        if report:
            Path(report).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        for row in rows:
            with self.subTest(encoding=row["encoding"], corruption=row["corruption"], length=row["logical_size"]):
                self.assertTrue(row["reader_closed"], row)
                if row["corruption"] == "none":
                    self.assertEqual(row["status"], 200, row)
                    self.assertEqual(int(row["content_length"]), row["logical_size"], row)
                    self.assertEqual(row["content_type"], "text/html; charset=utf-8", row)
                    self.assertIsNone(row["content_encoding"], row)
                    self.assertTrue(row["client_success"] and row["body_matches_original"], row)
                    self.assertTrue(row["reader_verified"], row)
                    self.assertFalse(row["server_errors"], row)
                else:
                    self.assertFalse(row["status"] == 200 and row["client_success"], row)
                    self.assertFalse(row["reader_verified"], row)
                    self.assertTrue(row["server_errors"], row)
                    if row["status"] == 200:
                        self.assertLess(row["partial_size"], row["logical_size"], row)


if __name__ == "__main__":
    unittest.main()
