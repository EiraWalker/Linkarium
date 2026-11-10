#!/usr/bin/env python3
"""Offline artifact compression/rollback. Defaults to validation-only dry run.

Windows read-only inputs or recovery files fail with readonly_artifact before
artifact writes/cleanup, including dry runs; shared-inode permissions are never
relaxed. POSIX read-only mode handling is unchanged.

tree_allocated_bytes_reduction is a tree inventory delta, not physical reclamation
(and can be negative on restore). filesystems reports deduplicated device rows
with free_before/free_after/free_delta observations in bytes. Free deltas include
other filesystem activity; open readers can delay release of unlinked allocation.
Measurement errors fail the operation. Each group uses current actual free space,
never projected savings or observed deltas, for its capacity check.
"""
import argparse
import json
from pathlib import Path
import sys

# The release/repository layout works without Django settings or PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from library.storage_optimization import optimize_artifacts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, action="append", required=True,
                        help="Explicit data directory; repeat for every hardlinked backup data directory")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes (default: dry run; only supervisor lock files may be created)")
    parser.add_argument("--restore-raw", action="store_true",
                        help="Restore legacy .html bytes and hardlinks; requires --apply to write")
    parser.add_argument("--safety-margin-mib", type=int, default=64,
                        help="Free-space reserve per filesystem, in MiB (default: 64)")
    args = parser.parse_args(argv)
    report = optimize_artifacts(args.data_root, apply=args.apply,
                                restore_raw=args.restore_raw,
                                safety_margin_bytes=args.safety_margin_mib * 1024 * 1024)
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
