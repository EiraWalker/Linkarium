import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from capture.models import CaptureError, CaptureRequest, MAX_CAPTURE_BYTES


class CaptureBudgetTests(unittest.TestCase):
    def request(self, **kwargs):
        return CaptureRequest(str(uuid4()), 'test', 1, 'https://example.com/', 'unused', **kwargs)

    def test_default_budget_and_exact_upper_bound_are_96_mib(self):
        self.assertEqual(self.request().max_bytes, 96 * 1024 * 1024)
        request = self.request(max_bytes=96 * 1024 * 1024)
        request.validate()
        self.assertEqual(request.timeout_seconds, 120)

    def test_above_maximum_and_timeout_extension_are_rejected(self):
        for values in ({'max_bytes': 96 * 1024 * 1024 + 1}, {'timeout_seconds': 121}):
            with self.subTest(values=values), self.assertRaises(CaptureError) as caught:
                self.request(**values).validate()
            self.assertEqual(caught.exception.code, 'invalid_request')

    def test_smaller_explicit_capture_budget_remains_supported(self):
        request = self.request(max_bytes=64 * 1024 * 1024)
        request.validate()
        self.assertEqual(request.max_bytes, 64 * 1024 * 1024)

    def test_linux_launcher_rejects_caller_selected_budget(self):
        path = Path(__file__).resolve().parents[2] / 'deploy/capture/launcher.py'
        spec = importlib.util.spec_from_file_location('capture_budget_launcher', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            identity = (str(uuid4()), 'test', 1)
            folder = Path(directory) / identity[0]
            folder.mkdir()
            (folder / 'request.json').write_text(json.dumps(dict(attempt_uuid=identity[0], generation='test',
                library_epoch=1, url='https://example.com/', variant='desktop', mode='capture',
                max_bytes=96 * 1024 * 1024)), encoding='utf8')
            launcher = module.Launcher({'spool_root': directory})
            with self.assertRaisesRegex(ValueError, 'input identity mismatch'):
                # Exercise schema validation independently of Linux's atomic
                # filesystem takeover (covered by the root-only spool tests).
                launcher.execute(identity, folder)

    def test_root_owned_deployment_limits_match_the_application_contract(self):
        # These deployment modules run outside the application's import/runtime
        # boundary. Detect drift without importing caller-owned code into root.
        root = Path(__file__).resolve().parents[2] / 'deploy/capture'
        for name in ('launcher', 'preflight'):
            spec = importlib.util.spec_from_file_location('budget_' + name, root / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES)
            if name == 'launcher':
                self.assertGreaterEqual(module.CAPTURE_SCRATCH_BYTES, 3 * MAX_CAPTURE_BYTES + 16 * 1024 * 1024)
