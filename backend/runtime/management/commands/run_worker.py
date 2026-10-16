from django.core.management.base import BaseCommand
from library.worker import run_worker

class Command(BaseCommand):
    help = "Run the single business worker"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        from config.paths import DATA_ROOT
        from supervisor import InstanceLock
        with InstanceLock(DATA_ROOT / "runtime" / "worker.lock"):
            run_worker(once=options["once"])
