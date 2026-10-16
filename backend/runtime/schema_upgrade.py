"""Upgrade only an exact trusted previous schema, in an isolated scratch library."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing


def upgrade_previous_library(database, expected_schema):
    from config.paths import PROJECT_ROOT, DATA_ROOT
    from .maintenance import MaintenanceError, _validate_database
    # Rebuild the known previous schema from shipped migrations, never from a
    # backup's SQL or django_migrations rows. This retains strict schema checking.
    with tempfile.TemporaryDirectory(prefix="schema-upgrade-", dir=DATA_ROOT / "work") as directory:
        root = Path(directory)
        environment = dict(os.environ, BOOKMARK_DATA_ROOT=str(root))
        environment.pop("DJANGO_SECRET_KEY", None)
        def migrate(target):
            try:
                result = subprocess.run([sys.executable, "-X", "utf8", str(PROJECT_ROOT / "backend" / "manage.py"),
                                         "migrate", "library", target, "--database", "library", "--noinput", "--verbosity", "0"],
                                        env=environment, cwd=PROJECT_ROOT, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise MaintenanceError("BACKUP_SCHEMA_INCOMPATIBLE") from error
            if result.returncode:
                raise MaintenanceError("BACKUP_SCHEMA_INCOMPATIBLE")
        migrate("0001_initial")
        scratch = root / "generations" / "initial" / "library.sqlite3"
        _validate_database(database, expected_schema=scratch)
        # Reset migration bookkeeping to the trusted baseline before running
        # local migrations; imported rows cannot select or skip migration code.
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(scratch)) as target:
            source.backup(target)
            target.execute("DELETE FROM django_migrations")
            target.execute("INSERT INTO django_migrations(app,name,applied) VALUES('library','0001_initial',CURRENT_TIMESTAMP)")
            target.commit()
        migrate("0002_import_workflow")
        _validate_database(scratch, expected_schema=expected_schema)
        with closing(sqlite3.connect(scratch)) as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
