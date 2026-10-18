"""Upgrade only an exact trusted previous schema, in an isolated scratch library."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing


TRUSTED_LIBRARY_MIGRATIONS = (
    '0001_initial',
    '0002_import_workflow',
    '0003_classificationtaxonomy_folder_created_by_ai',
    '0004_import_execution_budgets',
)

# Register before Django opens its migration connection. Only shipped migration
# code runs, but the rows it processes still originate in an untrusted backup.
MIGRATE_TRUSTED_LIBRARY = '''
import os,sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
from django.db.backends.signals import connection_created
def restrict_sqlite(sender,connection,**kwargs):
    if connection.vendor == 'sqlite':
        connection.connection.enable_load_extension(False)
        connection.connection.execute('PRAGMA trusted_schema=OFF')
connection_created.connect(restrict_sqlite,weak=False)
import django
django.setup()
from django.core.management import call_command
call_command('migrate','library',sys.argv[1],database='library',interactive=False,verbosity=0)
'''


def upgrade_previous_library(database, expected_schema):
    from config.paths import PROJECT_ROOT, DATA_ROOT
    from .maintenance import MaintenanceError, _validate_database, schema_signature
    trusted_target = schema_signature(expected_schema) if isinstance(expected_schema, (str, Path)) else expected_schema
    incoming_schema = schema_signature(database)
    # Reconstruct every supported release layout from shipped migrations. A
    # backup's migration claims never choose which Python code runs or is skipped.
    with tempfile.TemporaryDirectory(prefix="schema-upgrade-", dir=DATA_ROOT / "work") as directory:
        root = Path(directory)
        environment = dict(os.environ, BOOKMARK_DATA_ROOT=str(root))
        environment.pop("DJANGO_SECRET_KEY", None)
        def migrate(target):
            try:
                result = subprocess.run([sys.executable, "-X", "utf8", '-c', MIGRATE_TRUSTED_LIBRARY, target],
                                        env=environment, cwd=PROJECT_ROOT, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise MaintenanceError("BACKUP_SCHEMA_INCOMPATIBLE") from error
            if result.returncode:
                raise MaintenanceError("BACKUP_SCHEMA_INCOMPATIBLE")
        scratch = root / "generations" / "initial" / "library.sqlite3"
        source_version = target_version = None
        for index, migration in enumerate(TRUSTED_LIBRARY_MIGRATIONS):
            migrate(migration)
            reconstructed = schema_signature(scratch)
            if reconstructed == incoming_schema:
                source_version = index
            if reconstructed == trusted_target:
                target_version = index
        if source_version is None or target_version is None or source_version > target_version:
            raise MaintenanceError('BACKUP_SCHEMA_INCOMPATIBLE')
        _validate_database(database, expected_schema=incoming_schema)
        if source_version == target_version:
            return
        # Reset migration bookkeeping to the trusted baseline before running
        # local migrations; imported rows cannot select or skip migration code.
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(scratch)) as target:
            for connection in (source, target):
                connection.enable_load_extension(False)
                connection.execute('PRAGMA trusted_schema=OFF')
            source.backup(target)
            target.execute("DELETE FROM django_migrations")
            target.executemany("INSERT INTO django_migrations(app,name,applied) VALUES('library',?,CURRENT_TIMESTAMP)",
                               [(migration,) for migration in TRUSTED_LIBRARY_MIGRATIONS[:source_version + 1]])
            target.commit()
        migrate(TRUSTED_LIBRARY_MIGRATIONS[target_version])
        _validate_database(scratch, expected_schema=trusted_target)
        with closing(sqlite3.connect(scratch)) as source, closing(sqlite3.connect(database)) as target:
            for connection in (source, target):
                connection.enable_load_extension(False)
                connection.execute('PRAGMA trusted_schema=OFF')
            source.backup(target)
