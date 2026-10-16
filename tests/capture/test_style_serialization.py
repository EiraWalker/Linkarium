"""CSS raw-text boundaries must preserve values without creating HTML nodes."""
import json
import os
from pathlib import Path
import unittest

import html5lib
from playwright.sync_api import sync_playwright
import tinycss2

from capture.rewrite import DocumentRewriter, Resource
from capture.runner import IMAGE_VISIBILITY


ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / 'backend' / 'capture' / 'freeze.js'
PAYLOAD = '</StYlE><h1 data-injected="yes">INSERTED</h1><style>'
SVG = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'>"
       "<style>rect{fill:rgb(18,52,86)}</style><rect width='16' height='16'/></svg>")


def css_tokens(css):
    """Compare CSS meaning independently of quote/escape serialization."""
    def token_value(token):
        kind = token.type
        if kind in ('comment', 'whitespace'):
            return None
        if kind == 'function':
            if token.lower_name == 'url':
                arguments = [part for part in token.arguments if part.type not in ('comment', 'whitespace')]
                if len(arguments) == 1 and arguments[0].type in ('string', 'url'):
                    return ('url', arguments[0].value)
            return (kind, token.name, tuple(filter(None, map(token_value, token.arguments))))
        if hasattr(token, 'content'):
            return (kind, tuple(filter(None, map(token_value, token.content))))
        return (kind, getattr(token, 'value', None))

    return tuple(filter(None, map(token_value, tinycss2.parse_component_value_list(css))))


class StyleSerializationTests(unittest.TestCase):
    def assert_only_original_nodes(self, markup):
        tree = html5lib.parse(markup, namespaceHTMLElements=False, scripting=True)
        self.assertEqual(len(list(tree.iter('h1'))), 0, 'CSS text became a new HTML element')
        self.assertEqual(len(list(tree.iter('img'))), 0, 'CSS text became an unintended image')
        return tree

    def test_stylesheet_strings_urls_and_bare_tokens_survive_two_html_roundtrips(self):
        cases = [
            '#target{--payload:' + json.dumps(PAYLOAD) + ';content:' + json.dumps(PAYLOAD) + '}',
            '#target{background-image:url(' + json.dumps(SVG) + ')}',
            '#target{--literal:</StYlE>;--escaped:foo\\\"bar;--comment:/* </STYLE> */red}',
            '#target{background-image:url(data:image/svg+xml,<svg><style></style></svg>)}',
        ]
        for css in cases:
            with self.subTest(css=css):
                resources = {'https://fixture.example/site.css': Resource(css.encode(), 'text/css',
                    'https://fixture.example/site.css')}
                markup = '<html><head><link rel="stylesheet" href="/site.css"></head><body>Original</body></html>'
                expected = css_tokens(css)
                for _ in range(2):
                    markup = DocumentRewriter(resources, [], 64 * 1024 * 1024).html(markup, 'https://fixture.example/')
                    tree = self.assert_only_original_nodes(markup)
                    styles = list(tree.iter('style'))
                    self.assertEqual(len(styles), 1)
                    self.assertEqual(css_tokens(styles[0].text or ''), expected)


@unittest.skipUnless(os.environ.get('RUN_CAPTURE_INTEGRATION') == '1', 'Requires Chromium')
class FreezeStyleSerializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools' / 'browsers'))
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def assert_style_path(self, path):
        with self.browser.new_context(viewport={'width': 900, 'height': 500}) as context:
            source, replay = context.new_page(), context.new_page()
            source.set_content('<!doctype html><html><head><style>body{margin:8px}#target{'
                'width:600px;height:120px;padding:8px;font:16px sans-serif;background-repeat:no-repeat;'
                'background-position:bottom right}#target::before{content:var(--payload)}</style></head>'
                '<body><div id="target">Original page</div></body></html>')
            source.evaluate('''({path,payload,svg}) => {
              const target = document.querySelector('#target');
              if (path === 'computed') {
                target.style.setProperty('--payload', JSON.stringify(payload));
                target.style.backgroundImage = `url(${JSON.stringify(svg)})`;
                target.style.setProperty('--bare', '</StYlE>');
              } else {
                const sheet = path === 'adopted' ? new CSSStyleSheet() : (() => {
                  const style = document.createElement('style'); document.head.appendChild(style); return style.sheet;
                })();
                sheet.insertRule(`#target{--payload:${JSON.stringify(payload)};--bare:</StYlE>;background-image:url(${JSON.stringify(svg)})}`);
                if (path === 'adopted') document.adoptedStyleSheets = [sheet];
              }
            }''', {'path': path, 'payload': PAYLOAD, 'svg': SVG})
            # Decode the same URL used by CSS before sampling pixels.
            source.evaluate('''src => new Promise((resolve,reject) => {
              const image = new Image(); image.onload=resolve; image.onerror=reject; image.src=src;
            })''', SVG)
            appearance = '''e => { const s=getComputedStyle(e),p=getComputedStyle(e,'::before'),r=e.getBoundingClientRect();
              return {content:p.content,background:s.backgroundImage,width:r.width,height:r.height}; }'''
            before = source.locator('#target').evaluate(appearance)
            pixels = source.locator('#target').screenshot()
            frozen = source.evaluate('() => (' + FREEZE.read_text(encoding='utf-8') + ')(' + IMAGE_VISIBILITY + ')')
            markup = frozen['html']
            for stage in ('freeze', 'rewrite', 'rewrite-again'):
                if stage != 'freeze':
                    markup = DocumentRewriter({}, [], 64 * 1024 * 1024).html(markup, 'https://fixture.example/')
                tree = html5lib.parse(markup, namespaceHTMLElements=False, scripting=True)
                self.assertFalse(list(tree.iter('h1')), f'{path}: {stage} turned CSS into HTML')
                self.assertFalse(list(tree.iter('img')), f'{path}: {stage} added an image')
            replay.set_content(markup)
            self.assertEqual(replay.locator('#target').evaluate(appearance), before)
            self.assertEqual(replay.locator('#target').screenshot(), pixels,
                f'{path}: CSS escaping changed the displayed string or embedded SVG pixels')

    def test_computed_style_strings_and_images_remain_css(self):
        self.assert_style_path('computed')

    def test_cssom_stylesheet_strings_and_images_remain_css(self):
        self.assert_style_path('cssom')

    def test_adopted_stylesheet_strings_and_images_remain_css(self):
        self.assert_style_path('adopted')


if __name__ == '__main__':
    unittest.main()
