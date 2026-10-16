"""Static archives retain audio controls without a media load blocking replay."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import html5lib
from playwright.sync_api import sync_playwright

from capture.rewrite import DocumentRewriter
from capture.runner import IMAGE_DIAGNOSTICS, IMAGE_VISIBILITY, REPLAY_CSP


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = '''<!doctype html><html><head><style>body{margin:8px}
audio{display:block;width:260px;height:54px;margin:6px;border:2px solid navy}</style></head>
<body><p>Readable page around preserved audio controls</p>
<audio id="sound" controls autoplay preload="auto">
<source src="data:application/octet-stream;base64," type="audio/ogg">
<source src="data:application/octet-stream;base64," type="audio/mpeg">
<track src="data:text/vtt,WEBVTT" kind="captions"></audio>
<img width="20" height="20" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20'%3E%3Crect width='20' height='20' fill='green'/%3E%3C/svg%3E">
</body></html>'''


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class AudioFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools/browsers'))
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_empty_audio_sources_cannot_block_archived_page_load(self):
        context = self.browser.new_context(viewport={'width':800,'height':600})
        try:
            source = context.new_page()
            source.set_content(FIXTURE, wait_until='domcontentloaded')
            appearance = '''e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {
                x:r.x,y:r.y,width:r.width,height:r.height,display:s.display,border:s.border,controls:e.controls}}'''
            before = source.locator('#sound').evaluate(appearance)
            frozen = source.evaluate('() => (' + (ROOT / 'backend/capture/freeze.js').read_text(encoding='utf8') + ')(' + IMAGE_VISIBILITY + ')')
            warnings = []
            rewriter = DocumentRewriter({}, warnings, 4 * 1024 * 1024)
            prepared = rewriter.html(frozen['html'], 'https://fixture.invalid/')
            with tempfile.TemporaryDirectory() as directory:
                subprocess.run([str(ROOT / 'tools/monolith/monolith.exe'), '-j', '-M', '-d', 'capture.invalid',
                    '-t', '1', '-b', 'https://fixture.invalid/', '-o', 'archive.html', '-'], input=prepared.encode(),
                    capture_output=True, check=True, timeout=10, cwd=directory)
                final = rewriter.html((Path(directory) / 'archive.html').read_text(encoding='utf8'),
                    'https://fixture.invalid/', restore_shadow=True)
            replay = context.new_page()
            outside = []
            def route(request):
                if request.request.url == 'https://snapshot.invalid/':
                    request.fulfill(status=200, body=final, headers={
                        'Content-Type':'text/html; charset=utf-8', 'Content-Security-Policy':REPLAY_CSP})
                else:
                    outside.append(request.request.url)
                    request.abort()
            replay.route('**/*', route)
            replay.goto('https://snapshot.invalid/', wait_until='load', timeout=2500)
            self.assertEqual(replay.evaluate('document.readyState'), 'complete')
            self.assertIn('Readable page around preserved audio controls', replay.locator('body').inner_text())
            self.assertEqual(replay.evaluate(IMAGE_DIAGNOSTICS)['new_visible_broken'], 0)
            self.assertEqual(outside, [])
            self.assertEqual(replay.locator('#sound').evaluate(appearance), before)
            self.assertEqual(replay.locator('#sound').evaluate('e=>({preload:e.preload,src:e.getAttribute("src"),autoplay:e.autoplay,children:e.children.length})'),
                {'preload':'none','src':None,'autoplay':False,'children':0})
            self.assertIn('audio-not-archived', frozen['warnings'])
            self.assertEqual(source.locator('#sound source').count(), 2, 'Freezing must change the clone, not source media')
        finally:
            context.close()

    def test_direct_audio_url_is_removed_but_controls_and_fallback_text_remain(self):
        context = self.browser.new_context()
        try:
            source = context.new_page()
            source.set_content('<audio controls autoplay preload="none" src="data:audio/wav;base64,">Audio fallback</audio>')
            frozen = source.evaluate('() => (' + (ROOT / 'backend/capture/freeze.js').read_text(encoding='utf8') + ')(' + IMAGE_VISIBILITY + ')')
            tree = html5lib.parse(frozen['html'], namespaceHTMLElements=False)
            audio = next(tree.iter('audio'))
            self.assertIsNone(audio.get('src'))
            self.assertIsNone(audio.get('autoplay'))
            self.assertEqual(audio.get('preload'), 'none')
            self.assertIn('controls', audio.attrib)
            self.assertEqual(audio.text, 'Audio fallback')
            self.assertIn('audio-not-archived', frozen['warnings'])
        finally:
            context.close()
