"""Previous releases remain restorable after the import workflow migration."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing

from runtime.maintenance import create_backup, validate_package


class PreviousSchemaTests(unittest.TestCase):
    def test_previous_backup_is_upgraded_and_rebound_to_candidate_bytes(self):
        from runtime.maintenance import digest
        project = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="previous-bookmark-schema-") as directory:
            root = Path(directory)
            data = root / "data"
            environment = dict(os.environ, BOOKMARK_DATA_ROOT=str(data))
            command = [sys.executable, "-X", "utf8", str(project / "backend/manage.py")]
            def run(*args):
                subprocess.run(command + list(args), env=environment, cwd=project, check=True, capture_output=True, timeout=30)
            run("migrate", "library", "0001_initial", "--database", "library", "--noinput", "--verbosity", "0")
            run("shell", "--command", "from library.models import Bookmark; Bookmark.objects.create(original_url='https://example.org/',canonical_url='https://example.org/',domain='example.org',notes='retain previous backup')")
            generation = data / "generations/initial"
            package = root / "previous.bookmarklib.zip"
            create_backup(generation, package)
            run("migrate", "library", "0002_import_workflow", "--database", "library", "--noinput", "--verbosity", "0")
            candidate = root / "candidate"
            manifest = validate_package(package, candidate, expected_schema=generation / "library.sqlite3")
            database = candidate / "library.sqlite3"
            self.assertEqual(manifest["files"]["library.sqlite3"]["sha256"], digest(database))
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT notes FROM library_bookmark").fetchone()[0], "retain previous backup")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(library_importbatch)")}
                self.assertIn("classification_group_id", columns)
