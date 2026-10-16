"""Preview or enqueue one recovery batch for currently failed snapshot devices."""
import json

from django.core.management.base import BaseCommand, CommandError

from library.errors import ApiError
from library.queue import retry_failed_captures


class Command(BaseCommand):
    help = "检查当前失败的快照；只有 --execute 才创建恢复任务，不修改历史批次。"

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true", help="将仍然失败的设备快照加入一个恢复批次")
        parser.add_argument("--expected-generation", help="要求当前收藏库 generation 与预览一致")
        parser.add_argument("--expected-library-epoch", type=int, help="要求当前收藏库 epoch 与预览一致")

    def handle(self, *args, **options):
        try:
            report = retry_failed_captures(execute=options["execute"],
                expected_generation=options["expected_generation"],
                expected_library_epoch=options["expected_library_epoch"])
        except ApiError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False))
