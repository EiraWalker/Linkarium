import importlib.util
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4


launcher_file = Path(__file__).resolve().parents[2] / 'deploy' / 'capture' / 'launcher.py'
spec = importlib.util.spec_from_file_location('capture_launcher', launcher_file)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LauncherBoundaryTests(unittest.TestCase):
    def setup_launcher(self, directory):
        return module.Launcher({'spool_root': directory, 'verification_file': str(Path(directory) / 'NO_VERIFICATION'),
            'app_gid': 10001, 'rootfs': '/does-not-exist', 'resolvers': ['1.1.1.1'], 'deny_hosts': [], 'deny_ipv4': []})

    def test_unverified_launcher_never_starts_a_process(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.setup_launcher(directory)
            response = launcher.handle({'action':'start', 'attempt_uuid':str(uuid4()), 'generation':'test', 'library_epoch':1})
            self.assertFalse(response['ok'])
            self.assertEqual(response['error']['code'], 'isolation_unverified')
            self.assertFalse(launcher.active)

    def test_cancelled_pending_attempt_cannot_become_running(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.setup_launcher(directory)
            identity = (str(uuid4()), 'test', 1)
            launcher.pending.add(identity)
            fields = dict(attempt_uuid=identity[0], generation=identity[1], library_epoch=identity[2])
            response = launcher.handle({'action':'cancel', **fields})
            self.assertTrue(response['exited'])
            self.assertIn(identity, launcher.cancelled)
            self.assertTrue(launcher.handle({'action':'status', **fields})['exited'])
            # Different retries/epochs have independent identities and are not cancelled.
            self.assertNotIn((identity[0], 'test', 2), launcher.cancelled)

    def test_unknown_action_and_command_text_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.setup_launcher(directory)
            payload = {'action':'start', 'attempt_uuid':str(uuid4()), 'generation':'test', 'library_epoch':1}
            with self.assertRaises(ValueError):
                launcher.handle({**payload, 'command':'arbitrary shell text'})
            with self.assertRaises(ValueError):
                launcher.handle({**payload, 'action':'exec'})

    def test_restarted_launcher_checks_and_stops_persisted_matching_cgroup(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.setup_launcher(directory)
            identity = (str(uuid4()), 'test', 1)
            description = launcher.unit_description(identity)
            active = {'value': True}
            calls = []
            def systemd(*arguments, **kwargs):
                calls.append(arguments)
                if arguments[:2] == ('systemctl', 'stop'):
                    active['value'] = False
                    return subprocess.CompletedProcess(arguments, 0, '', '')
                state = 'active' if active['value'] else 'inactive'
                return subprocess.CompletedProcess(arguments, 0,
                    f'LoadState=loaded\nActiveState={state}\nDescription={description}\n', '')
            with patch.object(module.os, 'name', 'posix'), patch.object(module, 'run', side_effect=systemd):
                result = launcher.handle({'action':'cancel','attempt_uuid':identity[0],'generation':identity[1],'library_epoch':identity[2]})
            self.assertTrue(result['exited'])
            self.assertIn(('systemctl','stop','bmc-' + identity[0]), calls)

    def test_persisted_different_epoch_is_not_cancelled_or_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.setup_launcher(directory)
            identity = (str(uuid4()), 'test', 1)
            wrong = launcher.unit_description((identity[0], 'test', 2))
            response = subprocess.CompletedProcess([], 0,
                'LoadState=loaded\nActiveState=active\nDescription=' + wrong + '\n', '')
            with patch.object(module.os, 'name', 'posix'), patch.object(module, 'run', return_value=response) as fake:
                with self.assertRaises(RuntimeError):
                    launcher.handle({'action':'cancel','attempt_uuid':identity[0],'generation':identity[1],'library_epoch':identity[2]})
                self.assertEqual(fake.call_count, 1)
