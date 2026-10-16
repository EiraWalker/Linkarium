"""An initially empty PDF.js viewer is loading, not a hidden ordinary page."""
import os
from pathlib import Path
import time
import unittest

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from capture.frame_content import wait_for_pdf
from capture.runner import IMAGE_VISIBILITY


ROOT = Path(__file__).resolve().parents[2]


def empty_viewer_markup(render_after_ms=None, hidden=False):
    # The production SSD viewer starts without any .page or canvas element.
    # Its empty display:block container has height:auto -> 0, while its enclosing
    # reader and toolbar are visible. The earlier fixture preallocated a page.
    render = '''() => {
        const page=document.createElement('div');
        page.className='page';page.setAttribute('data-page-number','1');
        page.style.cssText='width:600px;height:360px;background:#eee';
        const canvas=document.createElement('canvas');canvas.width=600;canvas.height=360;
        const drawing=canvas.getContext('2d');drawing.fillStyle='white';drawing.fillRect(0,0,600,360);
        drawing.fillStyle='black';drawing.font='20px sans-serif';drawing.fillText('Actual PDF page',20,40);
        page.append(canvas);document.querySelector('.pdfViewer').append(page);
        const text=document.createElement('span');text.id='pdf-text';text.textContent='Rendered PDF content';
        page.append(text);
        view.div=page;view.canvas=canvas;view.renderingState=3;
        PDFViewerApplication.pdfDocument={numPages:1};
        PDFViewerApplication.pdfViewer.pagesCount=1;
        document.querySelector('#count').textContent='1 / 1';
    }'''
    timer = f'setTimeout({render}, {int(render_after_ms)});' if render_after_ms is not None else ''
    wrapper_style = 'display:none' if hidden else 'display:block'
    return '''<!doctype html><html><head><style>
        body{margin:0}#reader{width:700px;height:450px;background:#444}
        #toolbar{height:30px;background:#aaa}.pdfViewer{display:block;width:600px;height:auto}
        </style></head><body><p>Readable surrounding page</p>
        <div id="wrapper" style="%s"><div id="reader"><div id="toolbar">Pages <span id="count">0 / 0</span></div>
        <div id="viewer" class="pdfViewer"></div></div></div><script>
        const view={renderingState:1,div:null,canvas:null};
        window.PDFViewerApplication={initialized:true,pdfDocument:null,
          pdfViewer:{pagesCount:0,currentPageNumber:1,getPageView(index){return index===0?view:null}}};
        %s</script></body></html>''' % (wrapper_style, timer)


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium')
class EmptyPDFViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools/browsers'))
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1000, 'height': 800})
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def assert_empty_visible_reader(self):
        state = self.page.locator('.pdfViewer').evaluate('''viewer => {
            const bounds=viewer.getBoundingClientRect(),style=getComputedStyle(viewer);
            return {height:bounds.height,width:bounds.width,display:style.display,
                visibility:style.visibility,pages:viewer.querySelectorAll('.page').length};
        }''')
        self.assertEqual(state, {'height': 0, 'width': 600, 'display': 'block',
            'visibility': 'visible', 'pages': 0})

    def test_zero_height_loading_viewer_waits_for_rendered_canvas(self):
        self.page.set_content(empty_viewer_markup(render_after_ms=700), wait_until='domcontentloaded')
        self.assert_empty_visible_reader()
        ready = wait_for_pdf(self.page.main_frame, IMAGE_VISIBILITY, timeout_ms=2500)
        self.assertTrue(ready, 'An empty, visible reader must wait for the PDF instead of becoming an ordinary page')
        self.assertEqual(self.page.locator('#count').inner_text(), '1 / 1')
        self.assertEqual(self.page.locator('#pdf-text').inner_text(), 'Rendered PDF content')
        self.assertEqual(self.page.locator('.pdfViewer canvas').evaluate('e=>[e.width,e.height]'), [600, 360])

    def test_zero_height_viewer_that_never_renders_times_out(self):
        self.page.set_content(empty_viewer_markup(), wait_until='domcontentloaded')
        self.assert_empty_visible_reader()
        started = time.monotonic()
        with self.assertRaises(PlaywrightTimeout):
            wait_for_pdf(self.page.main_frame, IMAGE_VISIBILITY, timeout_ms=500)
        self.assertLess(time.monotonic() - started, 2, 'The PDF readiness deadline must remain bounded')
        self.assertEqual(self.page.locator('#count').inner_text(), '0 / 0')
        self.assertEqual(self.page.locator('.pdfViewer canvas').count(), 0)

    def test_css_hidden_reader_does_not_wait_for_a_pdf_that_will_never_render(self):
        self.page.set_content(empty_viewer_markup(hidden=True), wait_until='domcontentloaded')
        self.assertEqual(self.page.locator('#wrapper').evaluate('e=>getComputedStyle(e).display'), 'none')
        started = time.monotonic()
        ready = wait_for_pdf(self.page.main_frame, IMAGE_VISIBILITY, timeout_ms=1200)
        self.assertFalse(ready)
        self.assertLess(time.monotonic() - started, 0.6, 'A CSS-hidden viewer must not consume the PDF wait budget')
