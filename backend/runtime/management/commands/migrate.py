"""Apply migrations only while this process exclusively owns the data directory."""
from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as DjangoMigrateCommand
from django.db import connections

from config.paths import DATA_ROOT
from runtime.instance_lock import InstanceLock


class Command(DjangoMigrateCommand):
    def execute(self, *args, **options):
        connection = connections[options["database"]]
        # Django's test runner migrates isolated in-memory SQLite stores. Those
        # stores cannot affect, or be owned by, a disk data-directory supervisor.
        if connection.vendor == "sqlite" and connection.creation.is_in_memory_db(connection.settings_dict["NAME"]):
            return super().execute(*args, **options)
        try:
            with InstanceLock(DATA_ROOT / "runtime" / "supervisor.lock"):
                return super().execute(*args, **options)
        except RuntimeError as error:
            raise CommandError(str(error)) from None
