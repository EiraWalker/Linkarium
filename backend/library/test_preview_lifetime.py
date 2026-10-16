"""Real Linux process tests; no Django, browser, worker or production data."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from library.preview_lifetime import install_parent_death_guard


def wait_file(path):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.02)
    raise TimeoutError("Fixture did not become ready")


def fixture():
    role, directory = sys.argv[2:4]
    root = Path(directory)
    command = [sys.executable, str(Path(__file__).resolve()), "--fixture"]
    if role == "parent":
        subprocess.Popen(command + ["renderer", directory, str(os.getpid()), sys.argv[4]],
                         start_new_session=True)
        time.sleep(60)
    elif role == "renderer":
        expected_parent, late = int(sys.argv[4]), sys.argv[5] == "late"
        if not late:
            install_parent_death_guard(expected_parent)
        browser = subprocess.Popen(command + ["browser", directory], start_new_session=True)
        leaf = wait_file(root / "leaf.json")
        (root / "ready.json").write_text(json.dumps({"renderer": os.getpid(), "browser": browser.pid, "leaf": leaf}))
        if late:
            wait_file(root / "install.json")
            install_parent_death_guard(expected_parent)
        time.sleep(60)
    else:
        # Model Playwright's detached browser and a stubborn browser subprocess.
        child = subprocess.Popen([sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"])
        (root / "leaf.json").write_text(json.dumps(child.pid))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)


@unittest.skipUnless(sys.platform == "linux", "Linux process lifetime integration")
class LinuxPreviewLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reap our own orphaned fixture processes instead of leaving zombies to
        # the host init. Preserve the runner's original subreaper setting.
        cls.libc = ctypes.CDLL(None, use_errno=True)
        cls.previous = ctypes.c_int()
        if cls.libc.prctl(37, ctypes.byref(cls.previous), 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER failed")
        if cls.libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")

    @classmethod
    def tearDownClass(cls):
        cls.libc.prctl(36, cls.previous.value, 0, 0, 0)

    def reap(self, pids, timeout=5):
        remaining = set(pids)
        deadline = time.monotonic() + timeout
        while remaining and time.monotonic() < deadline:
            for pid in list(remaining):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                if not Path(f"/proc/{pid}").exists():
                    remaining.remove(pid)
            time.sleep(0.02)
        return remaining

    def check_parent_death(self, late=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # This unrelated process must survive renderer cleanup.
            sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            parent = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--fixture", "parent",
                directory, "late" if late else "ready"], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            owned = {}
            try:
                owned = wait_file(root / "ready.json")
                self.assertEqual(os.getpgid(owned["renderer"]), owned["renderer"])
                self.assertEqual(os.getpgid(owned["browser"]), owned["browser"])
                self.assertNotEqual(os.getpgid(owned["browser"]), os.getpgid(owned["renderer"]))
                parent.kill()
                parent.wait(timeout=5)
                if late:
                    (root / "install.json").write_text("true")
                self.assertFalse(self.reap(owned.values()), "Renderer or detached descendants survived parent death")
                self.assertIsNone(sentinel.poll(), "Cleanup touched an unrelated process")
            finally:
                # Only exact PIDs belonging to this fixture, using pidfds.
                for pid in owned.values():
                    try:
                        descriptor = os.pidfd_open(pid)
                        try:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                        finally:
                            os.close(descriptor)
                    except ProcessLookupError:
                        pass
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                sentinel.kill()
                sentinel.wait(timeout=5)
                self.reap(owned.values())

    def test_parent_death_removes_renderer_detached_browser_and_grandchild(self):
        self.check_parent_death()

    def test_parent_died_before_registration_removes_detached_descendants(self):
        self.check_parent_death(late=True)

    def test_refuses_shared_process_group_without_signalling_it(self):
        result = subprocess.run([sys.executable, "-c",
            "import os; from library.preview_lifetime import install_parent_death_guard; install_parent_death_guard(os.getppid())"],
            capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must own its process group", result.stderr)


class PreviewLifetimePlatformTests(unittest.TestCase):
    def test_non_linux_is_noop(self):
        with patch("library.preview_lifetime.sys.platform", "win32"):
            self.assertFalse(install_parent_death_guard(123))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fixture":
        fixture()
    else:
        unittest.main()
