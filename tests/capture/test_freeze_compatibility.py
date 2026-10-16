"""Browser regressions for compact computed styles and static media fallbacks."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

from capture.rewrite import DocumentRewriter
from capture.runner import IMAGE_VISIBILITY
from capture.development import run_development_capture
from capture.models import CaptureRequest


ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / 'backend' / 'capture' / 'freeze.js'


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium')
class FreezeCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools' / 'browsers'))
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1366, 'height': 768})
        self.source = self.context.new_page()
        self.replay = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def freeze(self):
        return self.source.evaluate('() => (' + FREEZE.read_text(encoding='utf-8') + ')(' + IMAGE_VISIBILITY + ')')

    def restore(self, frozen):
        warnings = []
        html = DocumentRewriter({}, warnings, 64 * 1024 * 1024).html(
            frozen['html'], 'https://fixture.example/', restore_shadow=True)
        self.replay.set_content(html)
        return html

    @staticmethod
    def appearance(page, selectors):
        return page.evaluate('''selectors => selectors.map(selector => {
          const e = document.querySelector(selector), s = getComputedStyle(e), r = e.getBoundingClientRect();
          const before = getComputedStyle(e, '::before');
          return {width:r.width,height:r.height,x:r.x,y:r.y,color:s.color,background:s.backgroundColor,
            font:s.font,display:s.display,transform:s.transform,before:before.content,beforeColor:before.color};
        })''', selectors)

    def assert_appearance(self, before, after):
        self.assertEqual(len(before), len(after))
        for original, archived in zip(before, after):
            for name, value in original.items():
                if isinstance(value, (int, float)):
                    # Computed CSS serialization rounds subpixel track widths.
                    self.assertAlmostEqual(archived[name], value, delta=0.1, msg=name)
                else:
                    self.assertEqual(archived[name], value, name)

    def test_repeated_computed_styles_stay_compact_and_preserve_layout(self):
        self.source.set_content('''<!doctype html><html><head><style>
          body{margin:12px;font-family:sans-serif}main{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
          article{padding:16px;border:1px solid #ddd;border-radius:4px;background:#f5f6f7}
          article::before{content:"Saved";color:blue}strong{display:block;color:#123456}
          </style></head><body><main>''' + ''.join(
              f'<article id="card{i}"><strong>Site {i}</strong><span>Description {i}</span></article>' for i in range(600)
          ) + '</main></body></html>')
        before = self.appearance(self.source, ['#card0', '#card299', '#card599'])
        started = time.monotonic()
        frozen = self.freeze()
        elapsed = time.monotonic() - started
        size = len(frozen['html'].encode())
        nodes = self.source.locator('*').count()
        print(f'freeze-fixture: nodes={nodes} cards=600 bytes={size} seconds={elapsed:.3f}', flush=True)
        self.assertLess(size, 1_200_000, 'Repeated computed styles must not create megabytes of inline duplication')
        self.restore(frozen)
        self.assert_appearance(before, self.appearance(self.replay, ['#card0', '#card299', '#card599']))

    def test_layer_inline_dynamic_and_pseudo_styles_survive(self):
        self.source.set_content('''<!doctype html><html><head><style id="dynamic">
          @layer original { #target {color:purple!important;background:red!important} }
          #target {width:50vw;padding:8px;border:2px solid black;transform:translateX(3px)}
          #target::before {content:"State";color:green!important}
          </style></head><body><div id="target" style="color:blue!important;background:yellow!important">Text</div>
          <div id="flow">Following flow</div><script>
          document.querySelector('#dynamic').sheet.insertRule('#flow{margin-top:20px;color:rgb(13,24,35)}');
          </script></body></html>''')
        before = self.appearance(self.source, ['#target', '#flow'])
        frozen = self.freeze()
        self.restore(frozen)
        self.assertEqual(self.appearance(self.replay, ['#target', '#flow']), before)

    def test_large_partial_inherited_values_are_shared_without_leaking_scope(self):
        payload = 'x' * 12000
        tokens = ''.join(f'--theme-token-{i}:rgb(1,2,3);' for i in range(200))
        self.source.set_content('<html><head><style>.scoped{--payload:"' + payload + '";' + tokens + 'color:blue}</style></head>'
            '<body><aside id="outside">Outside scope</aside><main class="scoped">' + ''.join(
                f'<div id="item{i}" style="width:{i + 100}px">Item {i}</div>' for i in range(120)
            ) + '</main></body></html>')
        before = self.appearance(self.source, ['#outside', '#item0', '#item119'])
        frozen = self.freeze()
        self.assertLess(len(frozen['html'].encode()), 300000,
            'Inherited values absent on another branch must not repeat on every styled element')
        self.restore(frozen)
        self.assert_appearance(before, self.appearance(self.replay, ['#outside', '#item0', '#item119']))
        for page in (self.source, self.replay):
            self.assertEqual(page.locator('#outside').evaluate("e=>getComputedStyle(e).getPropertyValue('--payload')"), '')
            self.assertEqual(page.locator('#item119').evaluate("e=>getComputedStyle(e).getPropertyValue('--payload')"), '"' + payload + '"')

    def test_large_document_retains_content_and_first_screen_layout(self):
        self.source.set_content('''<!doctype html><html><head><style>
          body{margin:12px;font:16px sans-serif}li{line-height:24px}
          #first{color:blue}#first::before{content:"API: ";color:green}
          </style></head><body><h1 id="first">Python API</h1><ul>''' + ''.join(
              f'<li><a><span><code>Function {i}</code></span></a></li>' for i in range(9000)
          ) + '</ul></body></html>')
        before = self.appearance(self.source, ['#first', 'li:first-child', 'li:last-child'])
        frozen = self.freeze()
        self.assertIn('large-dom-first-screen-styles', frozen['warnings'])
        self.assertLess(len(frozen['html'].encode()), 4_000_000)
        self.restore(frozen)
        self.assertEqual(self.replay.locator('li').count(), 9000)
        self.assertEqual(self.replay.locator('li:last-child').inner_text(), 'Function 8999')
        self.assert_appearance(before, self.appearance(self.replay, ['#first', 'li:first-child', 'li:last-child']))

    def test_document_hard_node_budget_is_still_enforced(self):
        self.source.set_content('<html><body>' + '<i></i>' * 100001 + '</body></html>')
        with self.assertRaisesRegex(Exception, 'DOM exceeds capture node budget'):
            self.freeze()

    def test_media_without_frame_or_readable_canvas_has_static_fallback(self):
        self.source.set_content('''<!doctype html><html><body><h1>Media fixture</h1>
          <video id="empty" width="320" height="180"></video>
          <canvas id="tainted" width="240" height="120"></canvas>
          <script>document.querySelector('canvas').toDataURL=()=>{throw new DOMException('tainted','SecurityError')}</script>
          </body></html>''')
        before = self.appearance(self.source, ['#empty', '#tainted'])
        frozen = self.freeze()
        self.assertIn('media-placeholder:VIDEO', frozen['warnings'])
        self.assertIn('media-placeholder:CANVAS', frozen['warnings'])
        self.assertFalse(any(warning.startswith('unreadable-media:') for warning in frozen['warnings']))
        self.restore(frozen)
        self.assertEqual(self.replay.locator('video,canvas').count(), 0)
        self.assertTrue(self.replay.locator('#empty').evaluate('e=>e.complete&&e.naturalWidth>0'))
        self.assertTrue(self.replay.locator('#tainted').evaluate('e=>e.complete&&e.naturalWidth>0'))
        self.assertEqual(self.appearance(self.replay, ['#empty', '#tainted']), before)

    def test_video_poster_is_an_archivable_image(self):
        poster = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="320" height="180"%3E%3Crect width="320" height="180" fill="blue"/%3E%3C/svg%3E'
        self.source.set_content('<!doctype html><html><body><video id="poster" width="320" height="180"></video></body></html>')
        self.source.locator('#poster').evaluate('(e,src)=>e.poster=src', poster)
        frozen = self.freeze()
        self.assertIn('media-poster:VIDEO', frozen['warnings'])
        self.restore(frozen)
        self.assertEqual(self.replay.locator('#poster').evaluate('e=>e.tagName'), 'IMG')
        self.assertTrue(self.replay.locator('#poster').evaluate('e=>e.complete&&e.naturalWidth>0'))

    def test_large_raster_image_preserves_visible_pixels_at_display_resolution(self):
        self.source.set_content('<html><body><img id="large" style="width:200px;height:120px"></body></html>')
        self.source.evaluate('''async () => {
          const canvas = document.createElement('canvas'); canvas.width = 4000; canvas.height = 3000;
          const c = canvas.getContext('2d'); c.fillStyle = '#1466aa'; c.fillRect(0, 0, 2000, 1500);
          c.fillStyle = '#e9a32c'; c.fillRect(2000, 1500, 2000, 1500);
          const image = document.querySelector('img'); image.src = canvas.toDataURL('image/png'); await image.decode();
        }''')
        for fit in ('contain', 'cover', 'fill'):
            with self.subTest(object_fit=fit):
                self.source.locator('#large').evaluate('(e,fit)=>e.style.objectFit=fit', fit)
                before = self.source.locator('#large').screenshot()
                frozen = self.freeze()
                html = self.restore(frozen)
                self.replay.locator('#large').evaluate('e=>e.decode()')
                self.assertLess(self.replay.locator('#large').evaluate('e=>e.naturalWidth'), 1000)
                self.assertEqual(self.replay.locator('#large').screenshot(), before,
                    'Archiving a large individual image must preserve its actual first-screen pixels and transparency')
                self.assertNotIn('data-capture-raster-src', html)

    def test_large_unreadable_image_keeps_original_resource(self):
        self.source.set_content('<html><body><img id="large" style="width:200px;height:150px"></body></html>')
        original = self.source.evaluate('''async () => {
          const canvas = document.createElement('canvas'); canvas.width=4000; canvas.height=3000;
          canvas.getContext('2d').fillRect(0, 0, 4000, 3000);
          const image=document.querySelector('img'); image.src=canvas.toDataURL('image/png'); await image.decode();
          HTMLCanvasElement.prototype.toDataURL=()=>{throw new DOMException('tainted','SecurityError')};
          return image.src;
        }''')
        self.restore(self.freeze())
        self.assertEqual(self.replay.locator('#large').get_attribute('src'), original)
        self.assertEqual(self.replay.locator('#large').evaluate('e=>e.naturalWidth'), 4000)

    def test_original_pixel_size_and_pixel_art_sampling_are_preserved(self):
        self.source.set_content('<html><body><img id="large" style="width:200px;height:120px"></body></html>')
        self.source.evaluate('''async () => {
          const canvas=document.createElement('canvas'); canvas.width=4000; canvas.height=3000;
          const c=canvas.getContext('2d');c.fillStyle='blue';c.fillRect(1990,1490,20,20);
          const image=document.querySelector('img');image.src=canvas.toDataURL('image/png');await image.decode();
        }''')
        for fit, rendering in (('none', 'auto'), ('fill', 'pixelated')):
            with self.subTest(object_fit=fit, image_rendering=rendering):
                self.source.locator('#large').evaluate('(e,v)=>{e.style.objectFit=v[0];e.style.imageRendering=v[1]}', [fit,rendering])
                before = self.source.locator('#large').screenshot()
                self.restore(self.freeze())
                self.replay.locator('#large').evaluate('e=>e.decode()')
                self.assertEqual(self.replay.locator('#large').evaluate('e=>e.naturalWidth'),4000)
                self.assertEqual(self.replay.locator('#large').screenshot(),before)

    def test_shadow_styles_and_slot_layout_survive(self):
        self.source.set_content('''<!doctype html><html><body><div id="host"><span>Slotted text</span></div>
          <script>const root=document.querySelector('#host').attachShadow({mode:'open'});
          root.innerHTML='<style>:host{display:block}section{padding:14px;background:#ddd}section::before{content:"Shadow";color:blue}</style><section><slot></slot></section>';
          </script></body></html>''')
        expression = '''()=>{const e=document.querySelector('#host').shadowRoot.querySelector('section');
          const r=e.getBoundingClientRect(),s=getComputedStyle(e);
          return {width:r.width,height:r.height,color:s.color,padding:s.padding,before:getComputedStyle(e,'::before').content}}'''
        before = self.source.evaluate(expression)
        self.restore(self.freeze())
        self.assertEqual(self.replay.evaluate(expression), before)

    def test_sampled_animation_values_do_not_restart(self):
        self.source.set_content('''<!doctype html><html><head><style>
          @keyframes change {from{transform:translateX(0px);color:blue}to{transform:translateX(200px);color:red}}
          #animated{animation:change 20s linear infinite;width:200px;height:30px}
          </style></head><body><div id="animated">Animation frame</div></body></html>''')
        sampled = self.source.evaluate('''() => {
          const animation = document.getAnimations()[0];
          animation.currentTime = 10000;
          animation.play();
          const frozen = (''' + FREEZE.read_text(encoding='utf-8') + ')(' + IMAGE_VISIBILITY + ''');
          return {frozen, pending:animation.pending, currentTime:animation.currentTime};
        }''')
        self.assertFalse(sampled['pending'], 'Freeze must finish the pending pause before sampling styles')
        self.assertEqual(sampled['currentTime'], 10000)
        self.source.evaluate('async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame)}')
        self.assertEqual(self.source.evaluate('()=>document.getAnimations()[0].currentTime'), 10000)
        frozen = sampled['frozen']
        before = self.appearance(self.source, ['#animated'])
        self.restore(frozen)
        self.assert_appearance(before, self.appearance(self.replay, ['#animated']))
        self.assertEqual(self.replay.locator('#animated').evaluate('e=>getComputedStyle(e).animationName'), 'none')

    def test_compact_styles_and_media_survive_actual_monolith_pipeline(self):
        markup = '''<!doctype html><html><head><title>Archived media fixture</title><style>
          @layer first { #content {color:purple!important} }
          #content{padding:12px;background:#ddeeff;width:350px}#content::before{content:"Frozen";color:blue}
          </style></head><body><div id="content" style="color:green!important">Readable article content</div>
          <video id="video" width="320" height="180"></video>
          <canvas id="canvas" width="240" height="120"></canvas>
          <script>document.querySelector('canvas').toDataURL=()=>{throw new DOMException('tainted','SecurityError')}</script>
          </body></html>'''

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = markup.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/'
            self.source.goto(url)
            before = self.appearance(self.source, ['#content', '#video', '#canvas'])
            with tempfile.TemporaryDirectory() as directory:
                request = CaptureRequest(attempt_uuid=str(uuid.uuid4()), generation='test', library_epoch=1,
                    url=url, output_dir=directory, development=True, allow_test_hosts=('127.0.0.1',))
                result = run_development_capture(request, lambda: True)
                self.assertIn('media-placeholder:VIDEO', result.warnings)
                self.assertIn('media-placeholder:CANVAS', result.warnings)
                self.replay.set_content(Path(result.artifact_path).read_text(encoding='utf-8'))
                self.assert_appearance(before, self.appearance(self.replay, ['#content', '#video', '#canvas']))
                self.assertEqual(self.replay.locator('video,canvas,script').count(), 0)
                self.assertEqual(self.replay.evaluate('()=>Array.from(document.images).filter(e=>!e.complete||!e.naturalWidth).length'), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
