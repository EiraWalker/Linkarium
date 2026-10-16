"""HTML tree and CSS token resource rewriting, not an HTML security boundary."""
import base64
from dataclasses import dataclass
import re
from urllib.parse import urldefrag, urljoin, urlsplit
import xml.etree.ElementTree as ET

import html5lib
import tinycss2

from .models import CaptureError


_STYLE_TOKENS = re.compile(
    r'''\\[\s\S]|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|/\*[\s\S]*?\*/|(?<![-\w])url\(\s*(?:\\[\s\S]|[^"'()\\\s])*\s*\)|</style''',
    re.IGNORECASE,
)
_STYLE_END_TAG = re.compile(r'</style', re.IGNORECASE)


def _style_raw_text(css: str) -> str:
    """Keep CSS values intact when HTML reparses raw-text style contents."""
    def escape(match):
        token = match.group()
        if _STYLE_END_TAG.match(token):
            return '<' + '/**/' + token[1:]
        return _STYLE_END_TAG.sub(lambda end_tag: '\\3c ' + end_tag.group()[1:], token)

    return _STYLE_TOKENS.sub(escape, css)


@dataclass
class Resource:
    body: bytes
    content_type: str
    final_url: str


class DocumentRewriter:
    def __init__(self, resources: dict[str, Resource], warnings: list[str], max_bytes: int):
        self.resources = resources
        self.warnings = warnings
        self.max_bytes = max_bytes
        self._active_css = set()

    def data_url(self, value: str, base: str) -> str:
        value = value.strip()
        if not value or value.startswith('#'):
            return value
        if value.startswith('data:'):
            if len(value) > self.max_bytes * 2:
                raise CaptureError('byte_budget', 'Embedded resource exceeds capture budget')
            header, separator, payload = value.partition(',')
            mime, *parameters = header.lower().split(';')
            if separator and mime == 'data:image/svg+xml' and 'base64' not in parameters:
                # Chromium accepts XML declarations with a literal '?' in data
                # SVGs, but Monolith treats it as a query boundary and truncates
                # the image. Escape only the payload, preserving the actual
                # fragment and already-encoded %23 characters inside SVG data.
                content, marker, fragment = payload.partition('#')
                value = header + ',' + content.replace('?', '%3F') + marker + fragment
            return value
        absolute = urljoin(base, value)
        document_url, fragment = urldefrag(absolute)
        # Monolith expands CSS url(#id) into the original document's absolute URL.
        # Keep these SVG paint/mask/clip references within the replay document;
        # another path or query remains an external resource and must be inlined.
        if fragment and document_url == urldefrag(base)[0]:
            return '#' + fragment
        resource = self.resources.get(absolute) or self.resources.get(absolute.split('#')[0])
        if resource is None:
            self.warnings.append('missing-resource:' + urlsplit(absolute).netloc)
            return 'data:application/octet-stream;base64,'
        mime = resource.content_type.split(';', 1)[0].strip() or 'application/octet-stream'
        # HTML/SVG resource scripts are inert under the mandatory replay and serving CSP.
        return f'data:{mime};base64,{base64.b64encode(resource.body).decode("ascii")}'

    def css(self, css: str, base: str, depth=0) -> str:
        if depth > 12:
            raise CaptureError('css_depth', 'Stylesheet import nesting exceeds budget')
        rules = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
        output = []
        for rule in rules:
            if rule.type == 'error':
                # Browsers ignore malformed top-level rules. tinycss2 instead
                # returns ParseError nodes, some of which cannot be serialized.
                # Keep valid sibling rules; retain token-level errors inside
                # declarations so an invalid value cannot become a valid one.
                if 'css-invalid-rule-ignored' not in self.warnings:
                    self.warnings.append('css-invalid-rule-ignored')
                continue
            if rule.type == 'at-rule' and rule.lower_at_keyword == 'import':
                meaningful = [token for token in rule.prelude if token.type not in ('whitespace', 'comment')]
                if not meaningful:
                    continue
                first = meaningful[0]
                target = self._url_value(first)
                if target is None:
                    continue
                absolute = urljoin(base, target)
                resource = self.resources.get(absolute)
                if resource and absolute not in self._active_css:
                    self._active_css.add(absolute)
                    imported = self.css(resource.body.decode('utf-8', 'replace'), resource.final_url, depth + 1)
                    self._active_css.remove(absolute)
                    rest = tinycss2.serialize(meaningful[1:]).strip()
                    # Complex supports()/layer imports need an explicit compatibility record.
                    if rest and ('layer' in rest or 'supports(' in rest):
                        self.warnings.append('css-import-layer-or-supports-flattened')
                        output.append(imported)
                    else:
                        output.append(f'@media {rest}{{{imported}}}' if rest else imported)
                else:
                    self.warnings.append('missing-css-import:' + urlsplit(absolute).netloc)
                continue
            self._walk(rule, base)
            output.append(rule.serialize())
        return ''.join(output)

    @staticmethod
    def _url_value(token):
        if token.type in ('url', 'string'):
            return token.value
        if token.type == 'function' and token.lower_name == 'url':
            arguments = [part for part in token.arguments if part.type not in ('whitespace', 'comment')]
            if len(arguments) == 1 and arguments[0].type in ('string', 'url', 'ident'):
                return arguments[0].value
        return None

    def _walk(self, node, base):
        value = self._url_value(node)
        if value is not None and (node.type == 'url' or node.type == 'function'):
            rewritten = self.data_url(value, base)
            replacement = tinycss2.parse_component_value_list('url("' + rewritten.replace('"', '%22') + '")')[0]
            # Replacing objects in parent arrays is simpler than changing differing AST slots.
            return replacement
        for attribute in ('prelude', 'content', 'arguments', 'value'):
            children = getattr(node, attribute, None)
            if isinstance(children, list):
                for index, child in enumerate(children):
                    replacement = self._walk(child, base)
                    if replacement is not None:
                        children[index] = replacement
        return None

    def inline_css(self, css, base):
        tokens = tinycss2.parse_component_value_list(css)
        for index, token in enumerate(tokens):
            replacement = self._walk(token, base)
            if replacement is not None:
                tokens[index] = replacement
        return tinycss2.serialize(tokens)

    def html(self, markup: str, base: str, frames: dict[str, str] | None = None, depth=0, restore_shadow=False) -> str:
        if depth > 8:
            raise CaptureError('frame_depth', 'Frame nesting exceeds budget')
        # The captured browser executed scripts. Parse its dormant noscript
        # contents as raw text before removal, including those inside <head>.
        # Otherwise the inert replay activates unfetched fallback images/CSS.
        tree = html5lib.parse(markup, namespaceHTMLElements=False, scripting=True)
        for parent in list(tree.iter()):
            for child in list(parent):
                tag = child.tag.split('}')[-1] if isinstance(child.tag, str) else ''
                if tag in ('script', 'noscript', 'object', 'embed', 'base') or (tag == 'meta' and child.get('http-equiv')):
                    parent.remove(child)
        for element in list(tree.iter()):
            if not isinstance(element.tag, str):
                continue
            tag = element.tag.split('}')[-1]
            if restore_shadow and element.get('data-capture-shadow') == 'declarative-placeholder':
                element.tag = 'template'
                element.set('shadowrootmode', 'open')
                element.attrib.pop('data-capture-shadow', None)
            for name in list(element.attrib):
                local = name.split('}')[-1].lower()
                if local.startswith('on') or local in ('nonce', 'integrity', 'crossorigin', 'ping', 'srcset', 'action', 'formaction', 'autofocus'):
                    del element.attrib[name]
            if tag == 'img':
                candidate = element.attrib.pop('data-capture-raster-src', None)
                if candidate and candidate.startswith('data:image/png;base64,'):
                    original = element.get('src', '')
                    absolute = urljoin(base, original)
                    resource = self.resources.get(absolute) or self.resources.get(absolute.split('#')[0])
                    original_size = (len(original) if original.startswith('data:') else
                        4 * ((len(resource.body) + 2) // 3) if resource else 0)
                    if len(candidate) < original_size:
                        element.set('src', candidate)
                        if 'image-display-resolution' not in self.warnings:
                            self.warnings.append('image-display-resolution')
                # freeze.js has already selected the browser's actual currentSrc.
                # Monolith prefers data-src over src, so a stale lazy-load hint
                # would replace our embedded responsive image with an unfetched URL.
                element.attrib.pop('data-src', None)
            if element.get('style'):
                element.set('style', self.inline_css(element.get('style'), base))
            if tag == 'style':
                # tinycss2 may decode the original CSS escape while serializing
                # strings or URLs, so protect the final HTML boundary again.
                element.text = _style_raw_text(self.css(element.text or '', base))
            if tag == 'link':
                if 'stylesheet' in element.get('rel', '').lower().split():
                    absolute = urljoin(base, element.get('href', ''))
                    resource = self.resources.get(absolute)
                    element.tag = 'style'
                    element.attrib.clear()
                    if resource:
                        element.text = _style_raw_text(self.css(resource.body.decode('utf-8', 'replace'), resource.final_url))
                    else:
                        element.text = ''
                        self.warnings.append('missing-stylesheet:' + urlsplit(absolute).netloc)
                else:
                    element.attrib.pop('href', None)
            if tag in ('a', 'area'):
                element.attrib.pop('href', None)
                element.attrib.pop('target', None)
            if tag == 'iframe':
                nested = (frames or {}).get(element.get('data-capture-frame', ''))
                if nested is None and element.get('srcdoc'):
                    nested = self.html(element.get('srcdoc'), base, depth=depth + 1, restore_shadow=restore_shadow)
                element.attrib.pop('src', None)
                element.set('sandbox', '')
                element.attrib.pop('allow', None)
                if nested:
                    element.set('srcdoc', nested)
                else:
                    element.set('srcdoc', '<html><body></body></html>')
                    self.warnings.append('unavailable-frame')
            else:
                for attribute in ('src', 'poster', 'background', '{http://www.w3.org/1999/xlink}href'):
                    if element.get(attribute):
                        element.set(attribute, self.data_url(element.get(attribute), base))
                # SVG <use>/<image> non-xlink href resource references.
                if tag in ('use', 'image') and element.get('href'):
                    element.set('href', self.data_url(element.get('href'), base))
            if tag == 'body':
                element.set('inert', '')
            if tag == 'form':
                element.set('inert', '')
        head = tree.find('head')
        if head is not None:
            meta = ET.Element('meta', {'charset': 'utf-8'})
            head.insert(0, meta)
        return '<!doctype html>' + html5lib.serialize(tree, tree='etree', quote_attr_values='always', omit_optional_tags=False)
