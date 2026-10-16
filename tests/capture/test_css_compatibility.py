"""Malformed site CSS must not discard the valid rules around it."""
import unittest

from capture.rewrite import DocumentRewriter, Resource


class CssCompatibilityTests(unittest.TestCase):
    def test_unserializable_trailing_rule_keeps_valid_styles_and_images(self):
        warnings = []
        rewriter = DocumentRewriter({'https://example.test/photo.svg':
            Resource(b'<svg/>', 'image/svg+xml', 'https://example.test/photo.svg')}, warnings, 100000)
        result = rewriter.css('.card{color:green;background:url(photo.svg)}\n@broken;', 'https://example.test/')
        self.assertIn('color:green', result)
        self.assertIn('data:image/svg+xml;base64,', result)
        # A bare selector at EOF is a top-level ParseError in tinycss2.
        result = rewriter.css(result + '\nthis rule has no block', 'https://example.test/')
        self.assertIn('color:green', result)
        self.assertNotIn('this rule has no block', result)
        self.assertIn('css-invalid-rule-ignored', warnings)

    def test_invalid_imported_css_does_not_hide_other_styles(self):
        warnings = []
        resources = {'https://example.test/site.css': Resource(b'.header{display:flex} invalid eof',
            'text/css', 'https://example.test/site.css')}
        rewriter = DocumentRewriter(resources, warnings, 100000)
        result = rewriter.html('<link rel="stylesheet" href="site.css"><p>Visible content</p>', 'https://example.test/')
        self.assertIn('.header{display:flex}', result)
        self.assertIn('Visible content', result)
        self.assertIn('css-invalid-rule-ignored', warnings)

    def test_invalid_declaration_tokens_remain_invalid(self):
        rewriter = DocumentRewriter({}, [], 100000)
        result = rewriter.css('.card{color:green;content:"bad\nstring";width:40px}', 'https://example.test/')
        self.assertIn('[bad string]', result)
        self.assertIn('color:green', result)
