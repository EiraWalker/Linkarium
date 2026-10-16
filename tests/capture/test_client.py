import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from capture import CaptureRequest, LauncherClient


class ClientSpoolContractTests(unittest.TestCase):
    def test_host_output_path_is_ignored_and_never_sent_to_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forbidden = root / 'caller-selected-host-path'
            attempt = str(uuid4())
            payload = b'<!doctype html><html><body>Example</body></html>'
            digest = hashlib.sha256(payload).hexdigest()
            client = LauncherClient('/unused-test-socket', root / 'fixed-spool')
            request = CaptureRequest(attempt, 'generation-1', 9, 'https://example.com/', str(forbidden))
            observed = {}

            def fake_launcher(message):
                observed['message'] = message
                folder = client.spool_path / attempt
                observed['request'] = json.loads((folder / 'request.json').read_text())
                (folder / 'output').mkdir()
                (folder / 'output' / (digest + '.html')).write_bytes(payload)
                return {'ok': True, 'result': {
                    'artifact_path': '/output/' + digest + '.html', 'title':'Example', 'description':'',
                    'text_excerpt':'Example', 'final_url':'https://example.com/', 'viewport_width':1366,
                    'viewport_height':768, 'warnings':[], 'sha256':digest, 'byte_size':len(payload)}}

            with patch.object(client, '_call', side_effect=fake_launcher):
                result = client.capture(request)
            self.assertFalse(forbidden.exists())
            self.assertNotIn('output_dir', observed['request'])
            self.assertEqual(set(observed['message']), {'action','attempt_uuid','generation','library_epoch'})
            self.assertEqual(Path(result.artifact_path), client.spool_path / attempt / 'output' / (digest + '.html'))

    def test_default_spool_respects_supervisor_environment(self):
        with patch.dict(os.environ, {'BOOKMARK_CAPTURE_SPOOL':'/configured-shared-spool'}):
            self.assertEqual(LauncherClient('/unused').spool_path, Path('/configured-shared-spool'))
