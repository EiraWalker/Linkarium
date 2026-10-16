"""Passive sampler ownership and numeric semantics; no real process control."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "performance_resources.py"
spec = importlib.util.spec_from_file_location("performance_resources", SCRIPT)
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


class ServiceOwnershipTests(unittest.TestCase):
    def test_only_exact_entrypoints_and_run_commands_belong_to_project(self):
        root = Path("D:/website")
        cases = [
            (["python.exe", r"D:\website\backend\supervisor.py"], True),
            (["python.exe", r"d:\WEBSITE\backend\manage.py", "run_web", "--port", "8001"], True),
            (["python.exe", r"D:\website\backend\manage.py", "run_worker"], True),
            (["python.exe", r"D:\website-other\backend\manage.py", "run_web"], False),
            (["python.exe", r"D:\website\backend\manage.py", "migrate"], False),
            (["python.exe", r"D:\website\backend\manage.py", "test"], False),
            (["python.exe", "-c", r"D:\website\backend\manage.py run_web"], False),
            (["python.exe", r"D:\website\backend\not_supervisor.py"], False),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(resources._is_service_command(arguments, root), expected)

    def test_reused_root_pid_cannot_adopt_its_new_descendants(self):
        sampler = resources.WindowsMetrics.__new__(resources.WindowsMetrics)
        sampler.roots = {10, 20}
        sampler.root_identities = {10: 100, 20: 200}
        sampler._entries = lambda: [(10, 0, "python.exe"), (11, 10, "child.exe"),
            (20, 0, "python.exe"), (21, 20, "chromium.exe"), (22, 21, "child.exe"),
            (30, 0, "unrelated.exe")]
        sampler.identity = lambda pid: {10: 999, 20: 200, 21: 201, 22: 202}[pid]
        self.assertEqual(sampler.processes(), {20, 21, 22})

    def test_descendant_cannot_belong_to_an_older_lifetime_of_same_parent_pid(self):
        sampler = resources.WindowsMetrics.__new__(resources.WindowsMetrics)
        sampler.roots = {20}
        sampler.root_identities = {20: 200}
        sampler._entries = lambda: [(20, 0, "python.exe"), (21, 20, "old-child.exe"),
            (22, 20, "real-child.exe")]
        sampler.identity = lambda pid: {20: 200, 21: 100, 22: 201}[pid]
        self.assertEqual(sampler.processes(), {20, 22})


class ResourceCalculationTests(unittest.TestCase):
    def test_cpu_uses_deltas_and_ignores_reused_pid_cpu_history(self):
        sampler = resources.ResourceSampler.__new__(resources.ResourceSampler)
        sampler.started = 0
        sampler.previous = (1.0, {"cpu_total_ticks": 100, "cpu_idle_ticks": 20},
            [{"pid": 10, "identity": "old", "cpu_seconds": 1000.0},
             {"pid": 20, "identity": "same", "cpu_seconds": 1.0}])
        current_system = {"cpu_total_ticks": 200, "cpu_idle_ticks": 80,
            "physical_total_bytes": 10000, "physical_available_bytes": 4000,
            "commit_limit_bytes": 20000, "commit_available_bytes": 5000}
        rows = [{"pid": 10, "identity": "new", "cpu_seconds": 9999.0, "rss_bytes": 3, "private_commit_bytes": 2},
                {"pid": 20, "identity": "same", "cpu_seconds": 2.0, "rss_bytes": 7, "private_commit_bytes": 5}]
        class Backend:
            def sample(self):
                return current_system, rows
        sampler.backend = Backend()
        with patch.object(resources.time, "perf_counter", side_effect=[2.0, 2.02]), \
                patch.object(resources.os, "cpu_count", return_value=4), \
                patch.object(resources.os, "name", "nt"):
            record = sampler.sample()
        self.assertEqual(record["system"]["cpu_percent"], 40.0)
        self.assertEqual(record["application"]["cpu_percent_of_machine"], 25.0)
        self.assertEqual(record["application"]["private_commit_sum_bytes"], 7)
        self.assertEqual(record["application"]["rss_sum_bytes"], 10)
        self.assertEqual(record["sampling_ms"], 20.0)


if __name__ == "__main__":
    unittest.main()
