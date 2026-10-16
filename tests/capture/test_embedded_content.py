"""Real-browser coverage for delayed PDF viewers and nested replay fidelity."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import html5lib
from playwright.sync_api import Frame, Page

from capture.models import CaptureError, CaptureRequest
from capture.rewrite import DocumentRewriter
from capture.runner import capture


PDF_TEXT = 'Actual embedded PDF page text'
GOOD_IMAGE = b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="green"/></svg>'


def pdf_viewer(delayed=True):
    render = '''setTimeout(() => {
      const canvas=document.createElement('canvas');canvas.width=300;canvas.height=180;
      const c=canvas.getContext('2d');c.fillStyle='#eee';c.fillRect(0,0,300,180);
      c.fillStyle='black';c.font='18px sans-serif';c.fillText('Rendered PDF page',15,30);
      holder.append(canvas);view.canvas=canvas;
      const text=document.createElement('span');text.id='pdf-text';text.textContent=%r;
      holder.append(text);view.renderingState=3;
    },2500);''' % PDF_TEXT if delayed else ''
    return '''<!doctype html><html><head><title>Embedded PDF fixture</title></head>
      <body style="margin:0"><div id="viewer" class="pdfViewer"><div class="page" data-page-number="1"
        style="width:300px;height:230px;background:#ddd"></div></div><script>
      const holder=document.querySelector('.page');
      const view={renderingState:1,div:holder,canvas:null};
      window.PDFViewerApplication={initialized:true,initializedPromise:Promise.resolve(),
        pdfDocument:{numPages:1},pdfViewer:{_pages:[view],currentPageNumber:1,
          pagesCount:1,getPageView(index){return index===0?view:null}}};
      %s
      </script></body></html>''' % render


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium and Monolith')
class EmbeddedContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/good.svg':
                    body, mime = GOOD_IMAGE, 'image/svg+xml'
                else:
                    if self.path in ('/pdf-delayed', '/pdf-never'):
                        html = pdf_viewer(self.path == '/pdf-delayed')
                    elif self.path == '/image-child':
                        html = '<html><body style="margin:0"><img width="80" height="80" src="/good.svg"></body></html>'
                    elif self.path == '/identity-child':
                        html = '<html><body><h2 id="original-child">Original embedded content</h2></body></html>'
                    elif self.path in ('/identity-parent', '/hidden-identity-parent', '/offscreen-identity-parent'):
                        style = {'/hidden-identity-parent': 'display:none',
                                 '/offscreen-identity-parent': 'position:absolute;top:2000px'}.get(self.path, '')
                        html = '<html><body><h1>Readable parent page</h1>' + (
                            f'<iframe src="/identity-child" width="400" height="200" style="{style}"></iframe></body></html>')
                    elif self.path in ('/pdf-parent', '/pdf-never-parent'):
                        child = '/pdf-delayed' if self.path == '/pdf-parent' else '/pdf-never'
                        html = f'<html><body style="margin:0"><iframe src="{child}" width="600" height="400"></iframe></body></html>'
                    else:
                        style = 'display:none' if self.path == '/hidden-image-parent' else ''
                        html = '<html><body><h1>Readable parent page</h1>' + (
                            f'<iframe src="/image-child" width="400" height="200" style="{style}"></iframe></body></html>')
                    body, mime = html.encode(), 'text/html; charset=utf-8'
                self.send_response(200)
                self.send_header('Content-Type', mime)
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

    def run_capture(self, path, corrupt=False, timeout=30, mutate_frozen_dom=None, child_freezes=None):
        original = DocumentRewriter.data_url
        original_evaluate = Page.evaluate
        original_frame_evaluate = Frame.evaluate
        mutated = False

        def data_url(rewriter, value, base):
            if corrupt and value.endswith('/good.svg'):
                return 'data:image/png;base64,broken-after-freeze'
            return original(rewriter, value, base)

        def evaluate(page, expression, *args, **kwargs):
            nonlocal mutated
            result = original_evaluate(page, expression, *args, **kwargs)
            if (mutate_frozen_dom and not mutated and isinstance(result, dict)
                    and {'html', 'text_excerpt', 'warnings'} <= result.keys()):
                # Model source-page scripts updating the DOM after the immutable
                # main clone exists, but before child frame HTML is collected.
                mutated = True
                original_evaluate(page, mutate_frozen_dom)
            return result

        def frame_evaluate(frame, expression, *args, **kwargs):
            if (child_freezes is not None and frame.parent_frame is not None and
                    isinstance(expression, str) and 'const serializeRoot = root =>' in expression):
                child_freezes.append(frame.url)
            return original_frame_evaluate(frame, expression, *args, **kwargs)

        with (tempfile.TemporaryDirectory() as directory,
              patch.object(DocumentRewriter, 'data_url', data_url),
              patch.object(Page, 'evaluate', evaluate),
              patch.object(Frame, 'evaluate', frame_evaluate)):
            request = CaptureRequest(attempt_uuid=str(uuid4()), generation='test', library_epoch=1,
                url=f'http://127.0.0.1:{self.server.server_port}{path}', output_dir=directory,
                development=True, allow_test_hosts=('127.0.0.1',), timeout_seconds=timeout)
            try:
                result = capture(request)
            except CaptureError:
                self.assertFalse(list(Path(directory).glob('*.html')), 'Failed embedded content must not produce an artifact')
                raise
            if mutate_frozen_dom:
                self.assertTrue(mutated, 'The source DOM must change after the main snapshot is frozen')
            return result, Path(result.artifact_path).read_text(encoding='utf8')

    def test_delayed_embedded_pdf_is_rendered_before_archiving(self):
        _result, html = self.run_capture('/pdf-parent')
        tree = html5lib.parse(html, namespaceHTMLElements=False)
        frame = next(tree.iter('iframe'))
        child = html5lib.parse(frame.get('srcdoc', ''), namespaceHTMLElements=False)
        text = child.find('.//span[@id="pdf-text"]')
        self.assertIsNotNone(text, 'The saved PDF must contain actual rendered page content, not an empty viewer shell')
        self.assertEqual(text.text, PDF_TEXT)
        self.assertTrue(any(e.get('data-capture-media') == 'frame' and
            e.get('src', '').startswith('data:image/png;base64,') for e in child.iter('img')))

    def test_unfinished_embedded_pdf_times_out_without_blank_artifact(self):
        started = time.monotonic()
        with self.assertRaises(CaptureError) as caught:
            self.run_capture('/pdf-never-parent', timeout=20)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertLess(time.monotonic() - started, 25, 'PDF readiness must remain bounded by the host deadline')

    def test_new_visible_damage_inside_iframe_is_rejected(self):
        with self.assertRaises(CaptureError) as caught:
            self.run_capture('/image-parent', corrupt=True)
        self.assertEqual(caught.exception.code, 'replay_failed')
        self.assertIn('first-screen', str(caught.exception))

    def test_hidden_iframe_damage_does_not_reject_visible_parent(self):
        _result, html = self.run_capture('/hidden-image-parent', corrupt=True)
        self.assertIn('Readable parent page', html)

    def assert_original_child_preserved(self, html):
        tree = html5lib.parse(html, namespaceHTMLElements=False)
        frames = list(tree.iter('iframe'))
        self.assertEqual(len(frames), 1, 'Only frames present in the frozen main DOM belong in this archive')
        child = html5lib.parse(frames[0].get('srcdoc', ''), namespaceHTMLElements=False)
        heading = child.find('.//h2[@id="original-child"]')
        self.assertIsNotNone(heading, 'DOM insertion must not change an existing frame identity')
        self.assertEqual(heading.text, 'Original embedded content')

    def test_dom_insertion_after_freeze_preserves_existing_iframe(self):
        _result, html = self.run_capture('/identity-parent', mutate_frozen_dom='''() => {
            const node=document.createElement('div');
            node.textContent='Inserted after main freeze';
            document.body.prepend(node);
        }''')
        self.assert_original_child_preserved(html)
        self.assertNotIn('Inserted after main freeze', html)

    def test_iframe_added_after_freeze_is_not_reported_as_lost_content(self):
        _result, html = self.run_capture('/identity-parent', mutate_frozen_dom='''() => new Promise(resolve => {
            const frame=document.createElement('iframe');
            frame.srcdoc='<html><body><h2>Late frame outside immutable snapshot</h2></body></html>';
            frame.onload=()=>resolve();
            document.body.append(frame);
        })''')
        self.assert_original_child_preserved(html)
        self.assertNotIn('Late frame outside immutable snapshot', html)

    def test_css_hidden_iframe_does_not_execute_its_freeze_script(self):
        freezes = []
        result, html = self.run_capture('/hidden-identity-parent', child_freezes=freezes)
        self.assertEqual(freezes, [], 'A hidden tracking frame must not block the visible document freezer')
        self.assertIn('hidden-frame-not-archived', result.warnings)
        self.assertIn('Readable parent page', html)
        self.assertNotIn('Original embedded content', html)

    def test_offscreen_rendered_iframe_keeps_its_content(self):
        freezes = []
        _result, html = self.run_capture('/offscreen-identity-parent', child_freezes=freezes)
        self.assertEqual(len(freezes), 1, 'Below-the-fold is not the same as CSS-hidden')
        self.assert_original_child_preserved(html)

    def test_iframe_hidden_after_parent_freeze_keeps_original_content(self):
        _result, html = self.run_capture('/identity-parent', mutate_frozen_dom='''() => {
            document.querySelector('iframe').style.display='none';
        }''')
        self.assert_original_child_preserved(html)

    def test_visible_iframe_removed_after_parent_freeze_cannot_publish_blank_frame(self):
        with self.assertRaises(CaptureError) as caught:
            self.run_capture('/identity-parent', mutate_frozen_dom='''() => {
                document.querySelector('iframe').remove();
            }''')
        self.assertEqual(caught.exception.code, 'replay_failed')
