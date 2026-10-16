"""An iframe must keep the sampled animation state in its static archive."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import html5lib
from playwright.sync_api import Page

from capture.models import CaptureRequest
from capture.runner import capture


CHILD_TEXT = 'Visible embedded content must survive freezing'
APPEARANCE = '''() => {
    const frame = document.querySelector('iframe');
    const rect = frame.getBoundingClientRect(), style = getComputedStyle(frame);
    return {x:rect.x, y:rect.y, width:rect.width, height:rect.height,
        opacity:Number(style.opacity), visibility:style.visibility};
}'''


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class IframeAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/child':
                    markup = '<!doctype html><html><body><h2 id="child-text">' + CHILD_TEXT + '</h2></body></html>'
                else:
                    markup = '''<!doctype html><html><head><style>
                        body {margin:8px}
                        @keyframes reveal {from {opacity:0;transform:translateX(0px)}
                                           to {opacity:1;transform:translateX(120px)}}
                        iframe {width:400px;height:160px;border:0;animation:reveal 2s linear both}
                        </style></head><body><h1>Readable parent content</h1>
                        <iframe src="/child"></iframe></body></html>'''
                body = markup.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def capture_animation_frame(self, sampled_ms):
        original_evaluate, original_goto = Page.evaluate, Page.goto
        observations = {}

        def evaluate(page, expression, *args, **kwargs):
            if isinstance(expression, str) and 'const serializeRoot = root =>' in expression:
                # Select a precise source frame immediately before the real
                # freezer samples it. Do not alter the immutable output/replay.
                count = original_evaluate(page, '''milliseconds => {
                    const animations=document.querySelector('iframe').getAnimations();
                    for (const animation of animations) {
                        animation.pause(); animation.currentTime=milliseconds;
                    }
                    return animations.length;
                }''', sampled_ms)
                self.assertEqual(count, 1, 'The fixture must have a real iframe CSS animation')
                observations['source'] = original_evaluate(page, APPEARANCE)
            return original_evaluate(page, expression, *args, **kwargs)

        def goto(page, target, *args, **kwargs):
            response = original_goto(page, target, *args, **kwargs)
            if target.startswith('http://snapshot.invalid/'):
                # Give an accidentally restarted animation enough time to
                # reveal an iframe whose content was skipped as transparent.
                page.wait_for_timeout(250)
                observations['replay'] = original_evaluate(page, APPEARANCE)
                observations['replay_child_text'] = '\n'.join(
                    frame.locator('body').inner_text() for frame in page.frames if frame != page.main_frame)
            return response

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(Page, 'evaluate', evaluate), patch.object(Page, 'goto', goto):
                result = capture(CaptureRequest(
                    attempt_uuid=str(uuid4()), generation='test', library_epoch=1,
                    url=f'http://127.0.0.1:{self.server.server_port}/', output_dir=directory,
                    development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=30))
            markup = Path(result.artifact_path).read_text(encoding='utf-8')
        self.assertIn('source', observations)
        self.assertIn('replay', observations)
        return result, markup, observations

    def assert_same_appearance(self, observations):
        source, replay = observations['source'], observations['replay']
        for key in ('x', 'y', 'width', 'height', 'opacity'):
            with self.subTest(property=key):
                self.assertAlmostEqual(replay[key], source[key], delta=0.01,
                    msg=f'Archived iframe {key} must remain at its sampled animation state')
        self.assertEqual(replay['visibility'], source['visibility'])

    def test_transparent_sampled_frame_cannot_animate_into_visible_empty_content(self):
        result, markup, observations = self.capture_animation_frame(0)
        self.assertEqual(observations['source']['opacity'], 0)
        self.assertIn('hidden-frame-not-archived', result.warnings)
        self.assertNotIn(CHILD_TEXT, markup)
        self.assertEqual(observations['replay_child_text'], '')
        self.assert_same_appearance(observations)

    def test_visible_sampled_iframe_keeps_geometry_opacity_and_nested_text(self):
        _result, markup, observations = self.capture_animation_frame(1000)
        self.assertAlmostEqual(observations['source']['opacity'], 0.5, delta=0.01)
        self.assert_same_appearance(observations)
        self.assertIn(CHILD_TEXT, observations['replay_child_text'])
        tree = html5lib.parse(markup, namespaceHTMLElements=False)
        frame = next(tree.iter('iframe'))
        child = html5lib.parse(frame.get('srcdoc', ''), namespaceHTMLElements=False)
        self.assertEqual(child.find('.//h2[@id="child-text"]').text, CHILD_TEXT)
