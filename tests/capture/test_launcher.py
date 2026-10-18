import importlib.util
import json
import os
import hashlib
from pathlib import Path
import tempfile
import threading
import subprocess
import time
import unittest
from unittest.mock import patch, Mock
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


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'Linux root filesystem boundary checks; no sandbox attestation is created')
class LinuxSpoolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o755)
        spool = self.root / 'spool'
        spool.mkdir()
        self.launcher = module.Launcher({'spool_root': str(spool), 'app_uid': 65533, 'app_gid': 65533,
            'rootfs': '/fixture/rootfs', 'deny_hosts': [], 'deny_ipv4': [], 'resolvers': ['1.1.1.1']})
        self.launcher.initialize_storage()
        self.identity = (str(uuid4()), 'test', 1)

    def tearDown(self):
        self.directory.cleanup()

    def incoming(self, body=None):
        folder = self.launcher.spool / 'incoming' / self.identity[0]
        folder.mkdir()
        (folder / 'request.json').write_text(json.dumps(body or {}))
        return folder

    def app_process(self, source, *arguments):
        def demote():
            os.setgroups([])
            os.setgid(65533)
            os.setuid(65533)
        return subprocess.run([__import__('sys').executable, '-c', source, *map(str, arguments)],
                              capture_output=True, text=True, preexec_fn=demote, timeout=5)

    def test_app_cannot_replace_privileged_parents_or_access_private_jobs(self):
        incoming = self.incoming()
        os.chown(incoming, 65533, 65533)
        private = self.launcher.acquire(self.identity)
        result = self.app_process('''import os,sys
for source in sys.argv[1:]:
 try: os.rename(source,source+'-replaced')
 except PermissionError: continue
 raise SystemExit('application replaced root-controlled path')
''', self.launcher.spool / 'private', self.launcher.spool / 'results', private)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(private.is_dir())
        self.assertFalse(incoming.exists())

    def test_replacing_original_inbox_after_atomic_takeover_cannot_redirect_root(self):
        incoming = self.incoming()
        victim = self.root / 'protected'
        victim.mkdir()
        marker = victim / 'marker'
        marker.write_text('keep')
        original_rename = module.os.rename
        def substitute(*arguments, **keywords):
            original_rename(*arguments, **keywords)
            incoming.symlink_to(victim, target_is_directory=True)
        with patch.object(module.os, 'rename', side_effect=substitute):
            private = self.launcher.acquire(self.identity)
        (private / 'sealed.json').write_text('root-controlled')
        self.assertFalse((victim / 'sealed.json').exists())
        self.assertEqual(marker.read_text(), 'keep')
        self.launcher.remove_folder(private)
        self.launcher.remove_folder(incoming)
        self.assertEqual(marker.read_text(), 'keep')

    def test_symlink_request_and_unexpected_entries_are_rejected_without_following(self):
        folder = self.incoming()
        victim = self.root / 'victim'
        victim.write_text('keep')
        (folder / 'request.json').unlink()
        (folder / 'request.json').symlink_to(victim)
        with self.assertRaises((OSError, RuntimeError)):
            self.launcher.start(self.identity)
        self.assertEqual(victim.read_text(), 'keep')
        self.assertFalse((self.launcher.spool / 'private' / self.identity[0]).exists())
        folder = self.incoming()
        (folder / 'scratch').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.launcher.start(self.identity)
        self.assertEqual(victim.read_text(), 'keep')

    def test_error_cleanup_release_identity_and_capacity(self):
        self.incoming()
        with self.assertRaises(ValueError):
            self.launcher.start(self.identity)
        self.assertEqual(list((self.launcher.spool / 'private').iterdir()), [])
        output = self.launcher.spool / 'results' / self.identity[0]
        output.mkdir()
        (output / '.identity.json').write_text(json.dumps(self.identity))
        (output / 'archive.html').write_text('copied and published')
        self.incoming()
        with patch.object(module, 'MAX_SPOOL_ATTEMPTS', 1), self.assertRaises(RuntimeError):
            self.launcher.acquire(self.identity)
        with self.assertRaises(ValueError):
            self.launcher.release((self.identity[0], 'other', 1))
        self.launcher.release(self.identity)
        self.assertFalse(output.exists())
        self.assertFalse((self.launcher.spool / 'incoming' / self.identity[0]).exists())

    def test_privileged_stages_use_claimed_paths_and_output_is_readable_before_release(self):
        body = dict(attempt_uuid=self.identity[0], generation=self.identity[1], library_epoch=self.identity[2],
                    url='https://example.com/', variant='desktop', mode='capture')
        incoming = self.incoming(body)
        private = self.launcher.spool / 'private' / self.identity[0]
        victim = self.root / 'victim'
        victim.mkdir()
        archive = b'<html>published archive</html>'
        digest = hashlib.sha256(archive).hexdigest()
        child = Mock(returncode=0)
        def finish(**_arguments):
            (private / 'scratch' / (digest + '.html')).write_bytes(archive)
            response = {'ok': True, 'result': {'artifact_path': '/output/' + digest + '.html',
                'sha256': digest, 'byte_size': len(archive)}}
            return json.dumps(response), ''
        child.communicate.side_effect = finish
        commands = []
        def operation(*arguments, **keywords):
            commands.append(arguments)
            if arguments[0] == 'mount':
                # This pathname substitution used to redirect later root sinks.
                incoming.symlink_to(victim, target_is_directory=True)
            return subprocess.CompletedProcess(arguments, 0, '', '')
        with patch.object(module, 'run', side_effect=operation), patch.object(module.subprocess, 'Popen', return_value=child) as popen:
            response = self.launcher.start(self.identity)
        self.assertTrue(response['ok'])
        self.assertFalse(private.exists())
        self.assertEqual(list(victim.iterdir()), [])
        mount = next(command for command in commands if command[0] == 'mount')
        self.assertEqual(mount[-1], str(private / 'scratch'))
        argv = popen.call_args.args[0]
        self.assertIn('BindPaths=' + str(private / 'scratch') + ':/output', argv)
        output = self.launcher.spool / 'results' / self.identity[0] / (digest + '.html')
        read = self.app_process('import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())', output)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn('published archive', read.stdout)
        destination = self.root / 'permanent.html'
        destination.write_bytes(output.read_bytes())
        self.launcher.release(self.identity)
        self.assertFalse(output.exists())
        self.assertEqual(destination.read_bytes(), archive)
        self.assertEqual(list(victim.iterdir()), [])

    def test_expired_outputs_and_inbox_symlinks_are_reaped_but_active_work_is_kept(self):
        victim = self.root / 'victim'
        victim.mkdir()
        (victim / 'keep').write_text('keep')
        stale = self.launcher.spool / 'incoming' / str(uuid4())
        stale.symlink_to(victim, target_is_directory=True)
        os.utime(stale, (0, 0), follow_symlinks=False)
        result = self.launcher.spool / 'results' / str(uuid4())
        result.mkdir()
        os.utime(result, (0, 0))
        active = self.launcher.spool / 'private' / self.identity[0]
        active.mkdir()
        os.utime(active, (0, 0))
        self.launcher.pending.add(self.identity)
        self.launcher.reap()
        self.assertFalse(stale.is_symlink())
        self.assertFalse(result.exists())
        self.assertTrue((victim / 'keep').exists())
        self.assertTrue(active.exists())
