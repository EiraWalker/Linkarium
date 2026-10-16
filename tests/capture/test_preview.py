import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from capture import CaptureError, CaptureRequest, LauncherClient, capture
from capture.preview import MAX_PREVIEW_BYTES, screenshot_preview, validate_preview
from tests.capture.test_capture import fixture_server
from tests.capture.test_launcher import module as launcher


def jpeg_envelope(width=1366, height=768):
    # A header fixture for trust-boundary tests. Real decodability is checked by
    # the browser capture integration test, not inferred from this envelope.
    return (b'\xff\xd8\xff\xc0\x00\x11\x08' + height.to_bytes(2, 'big')
        + width.to_bytes(2, 'big') + b'\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01'
        + b'\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\x00\xff\xd9')


class PreviewContractTests(unittest.TestCase):
    def result(self, content):
        digest = hashlib.sha256(content).hexdigest()
        return {'artifact_path': '/output/archive.html', 'viewport_width': 1366, 'viewport_height': 768,
                'preview_path': '/output/' + digest + '.jpg', 'preview_sha256': digest,
                'preview_byte_size': len(content)}

    def test_jpeg_dimensions_hash_and_bytes_are_checked_at_both_boundaries(self):
        content = jpeg_envelope()
        with tempfile.TemporaryDirectory() as directory:
            result = self.result(content)
            path = Path(directory) / Path(result['preview_path']).name
            path.write_bytes(content)
            actual = validate_preview(path, result['preview_sha256'], len(content), 1366, 768)
            self.assertEqual(actual['width'], 1366)
            self.assertEqual(launcher.validated_preview(result, Path(directory), 'desktop'), (path.name, content))
            for changes in ({'preview_sha256': 'f' * 64}, {'preview_byte_size': len(content) + 1},
                            {'viewport_width': 390}, {'preview_path': '/output/unrelated.jpg'},
                            {'artifact_path': None}):
                with self.subTest(changes=changes), self.assertRaises((RuntimeError, OSError)):
                    launcher.validated_preview({**result, **changes}, Path(directory), 'desktop')
            with self.assertRaises(CaptureError):
                validate_preview(path, 'f' * 64)
            with self.assertRaises(CaptureError):
                validate_preview(path, expected_size=len(content) + 1)
            with self.assertRaises(CaptureError):
                validate_preview(path, width=390, height=844)

    def test_active_or_malformed_content_cannot_be_published_as_jpeg(self):
        bad_images = (b'<svg onload="fetch(\"/api\")"></svg>', b'\xff\xd8\xff\xd9',
                      jpeg_envelope(390, 844), jpeg_envelope()[:-1],
                      b'\xff\xd8' + b'A' * MAX_PREVIEW_BYTES + b'\xff\xd9')
        with tempfile.TemporaryDirectory() as directory:
            for content in bad_images:
                result = self.result(content)
                path = Path(directory) / Path(result['preview_path']).name
                path.write_bytes(content)
                with self.subTest(length=len(content)), self.assertRaises(CaptureError):
                    validate_preview(path, width=1366, height=768)
                with self.subTest(length=len(content)), self.assertRaises(RuntimeError):
                    launcher.validated_preview(result, Path(directory), 'desktop')

    def test_hardlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            content = jpeg_envelope()
            result = self.result(content)
            path = Path(directory) / Path(result['preview_path']).name
            source = Path(directory) / 'source.jpg'
            source.write_bytes(content)
            os.link(source, path)
            with self.assertRaises(CaptureError):
                validate_preview(path)
            with self.assertRaises(RuntimeError):
                launcher.validated_preview(result, Path(directory), 'desktop')

    def test_old_launcher_results_without_previews_remain_valid(self):
        self.assertIsNone(launcher.validated_preview({'artifact_path': '/output/archive.html'}, Path('.'), 'desktop'))
        with self.assertRaises(RuntimeError):
            launcher.validated_preview({'preview_sha256': 'f' * 64}, Path('.'), 'desktop')

    def test_client_uses_fixed_spool_and_rejects_wrong_preview_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for wrong in (False, True):
                request = CaptureRequest(str(uuid4()), 'test', 1, 'https://example.com', directory)
                client = LauncherClient('/unused', root / 'spool')
                content = jpeg_envelope(390, 844) if wrong else jpeg_envelope()
                archive = b'<html><body>Archive</body></html>'
                digest = hashlib.sha256(archive).hexdigest()
                result = {**self.result(content), 'artifact_path': '/output/' + digest + '.html',
                          'sha256': digest, 'byte_size': len(archive), 'title': '', 'description': '',
                          'text_excerpt': '', 'final_url': 'https://example.com'}

                def respond(_message):
                    output = client.spool_path / request.attempt_uuid / 'output'
                    output.mkdir()
                    (output / (digest + '.html')).write_bytes(archive)
                    (output / Path(result['preview_path']).name).write_bytes(content)
                    return {'ok': True, 'result': result}

                with patch.object(client, '_call', side_effect=respond):
                    if wrong:
                        with self.assertRaises(CaptureError) as caught:
                            client.capture(request)
                        self.assertEqual(caught.exception.code, 'launcher_protocol')
                    else:
                        response = client.capture(request)
                        self.assertEqual(Path(response.preview_path).parent,
                            client.spool_path / request.attempt_uuid / 'output')


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Opt-in real Chromium/Monolith integration')
class PreviewCaptureTests(unittest.TestCase):
    def test_preview_is_first_screen_at_css_scale_even_on_a_long_high_dpi_page(self):
        from playwright.sync_api import sync_playwright
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path(__file__).resolve().parents[2] / 'tools/browsers'))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2)
                page.set_content('<html><body style="margin:0"><div style="height:844px;background:#1266cc">'
                                 'First screen</div><div style="height:4000px;background:red">Offscreen</div></body></html>')
                content = screenshot_preview(page, 390, 844)
                decoded = page.evaluate('''async encoded => {
                    const image = new Image(); image.src = 'data:image/jpeg;base64,' + encoded;
                    await image.decode(); const canvas = document.createElement('canvas');
                    canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
                    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
                    return {size: [image.naturalWidth, image.naturalHeight],
                            bottom: Array.from(context.getImageData(100, 800, 1, 1).data)};
                }''', base64.b64encode(content).decode())
                self.assertEqual(decoded['size'], [390, 844])
                self.assertLess(decoded['bottom'][0], 30)
                self.assertGreater(decoded['bottom'][2], 180)
            finally:
                browser.close()

    def test_preview_disk_failure_does_not_discard_valid_html(self):
        with fixture_server() as url, tempfile.TemporaryDirectory() as directory:
            request = CaptureRequest(str(uuid4()), 'test', 1, url, directory,
                development=True, allow_test_hosts=('127.0.0.1',))
            with patch('capture.runner.write_preview', side_effect=OSError('fixture full disk')):
                result = capture(request)
            self.assertTrue(Path(result.artifact_path).is_file())
            self.assertIsNone(result.preview_path)
            self.assertIn('preview-generation-failed', result.warnings)


if __name__ == '__main__':
    unittest.main()
