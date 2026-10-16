"""Run existing monitoring regression suites and retain per-test outcomes.

This runner changes no application code. Django suites must be launched with an
independent BOOKMARK_DATA_ROOT; live browser collection is a separate command.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend"), str(ROOT / "scripts")]


def redacted(value):
    value = re.sub(r"https?://[^\s<>'\"]+", "<URL_REDACTED>", str(value))
    value = re.sub(r"(?i)(authorization|cookie|password|api[_-]?key|token)(\s*[:=]\s*)[^\s,;]+", r"\1\2<REDACTED>", value)
    return value[:16000]


class DetailedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.active = {}

    def startTest(self, test):
        super().startTest(test)
        self.active[id(test)] = {"id": test.id(), "status": "running", "started": time.perf_counter()}

    def _update(self, test, status, details=None):
        row = self.active.get(id(test))
        if row is None:
            row = {"id": test.id(), "duration_ms": None}
            self.cases.append(row)
        # Subtest failures must survive a later successful sibling subtest.
        if row.get("status") not in ("failure", "error", "unexpected_success") or status != "passed":
            row["status"] = status
        if details:
            row["details"] = (row.get("details", "") + "\n" + redacted(details)).strip()

    def addSuccess(self, test):
        super().addSuccess(test)
        self._update(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._update(test, "failure", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._update(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._update(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._update(test, "expected_failure", self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._update(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            self._update(test, "failure" if issubclass(err[0], test.failureException) else "error",
                self._exc_info_to_string(err, subtest))

    def stopTest(self, test):
        row = self.active.pop(id(test), None)
        if row:
            row["duration_ms"] = round((time.perf_counter() - row.pop("started")) * 1000, 3)
            if row["status"] == "running":
                row["status"] = "passed"
            self.cases.append(row)
        super().stopTest(test)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("monitoring", "backend", "capture"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(argv)
    report = {"schema_version": 1, "name": options.suite, "started_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    started = time.perf_counter()
    result = None
    failure = False
    try:
        if options.suite == "backend":
            data_root = os.environ.get("BOOKMARK_DATA_ROOT")
            if not data_root or Path(data_root).resolve() == (ROOT / "data").resolve():
                raise ValueError("Backend regression requires an independent BOOKMARK_DATA_ROOT")
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
            import django
            django.setup()
            from django.test.runner import DiscoverRunner
            class RecordedRunner(DiscoverRunner):
                def get_test_runner_kwargs(self):
                    return {**super().get_test_runner_kwargs(), "resultclass": DetailedResult}

                def run_suite(self, suite, **kwargs):
                    nonlocal result
                    result = super().run_suite(suite, **kwargs)
                    return result
            labels = ["runtime.test_performance", "library.tests", "library.test_preview_cache", "library.test_capture_budget", "runtime.test_capture_recovery", "runtime.test_capture_network", "tests.portability"]
            report["scope"] = "Independent temporary databases; HTTP/API, AI transport fixtures, imports, maintenance, snapshots and monitoring"
            report["labels"] = labels
            failed_count = RecordedRunner(verbosity=2, interactive=False, parallel=0).run_tests(labels)
            failure = failed_count != 0
        else:
            loader = unittest.defaultTestLoader
            if options.suite == "monitoring":
                suite = loader.discover(str(ROOT / "tests" / "performance"), pattern="test_*.py")
                report["scope"] = "Monitoring contracts, read-only database/exports, process attribution and isolated headless method guard"
            else:
                suite = loader.loadTestsFromNames(["tests.capture.test_preview", "tests.capture.test_preview_backfill", "tests.capture.test_capture"])
                report["scope"] = "Selected capture/first-screen JPEG/HTML replay contracts and real local Chromium/Monolith fixtures; not the full capture compatibility suite"
            result = unittest.TextTestRunner(verbosity=2, resultclass=DetailedResult).run(suite)
            failure = not result.wasSuccessful()
        if result:
            report["cases"] = result.cases
            report["counts"] = {"tests": result.testsRun,
                "passed": sum(row["status"] == "passed" for row in result.cases),
                "failures": sum(row["status"] in ("failure", "unexpected_success") for row in result.cases),
                "errors": sum(row["status"] == "error" for row in result.cases),
                "skipped": sum(row["status"] == "skipped" for row in result.cases),
                "expected_failures": sum(row["status"] == "expected_failure" for row in result.cases)}
    except BaseException as error:
        failure = True
        report.update(harness_error=type(error).__name__, details=redacted(traceback.format_exc()))
        raise
    finally:
        report.update(status="failed" if failure else "passed", duration_ms=round((time.perf_counter() - started) * 1000, 3), finished_at=datetime.now(timezone.utc).isoformat())
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
