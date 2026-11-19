# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Hide generated citation labels without changing report evidence or body text."""
from html import unescape
from html.parser import HTMLParser
import re


class _CitationPresentation(HTMLParser):
    # Keep original markup (including SVG attribute case), changing only the
    # application's own citation nodes. This is not a sanitizer.
    VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = dict(children=[])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), opening=self.get_starttag_text(), closing='', children=[])
        self.stack[-1]['children'].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]['tag'] == tag:
                self.stack[i]['closing'] = '</' + tag + '>'
                del self.stack[i:]
                return

    def handle_data(self, value):
        self.stack[-1]['children'].append(value)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')

    def handle_comment(self, value):
        self.handle_data('<!--' + value + '-->')

    def render(self, node):
        if isinstance(node, str):
            return node, bool(unescape(node).strip())
        attrs = node['attrs']
        classes = set((attrs.get('class') or '').split())
        ref = attrs.get('data-ref') or ''
        citation = (node['tag'] == 'a' and 'cite' in classes
                    and re.fullmatch(r'[1-9][0-9]*', ref)
                    and attrs.get('href') == '#source-' + ref)
        children = [] if citation else [self.render(child) for child in node['children']]
        visible = any(part[1] for part in children) or node['tag'] in ('img', 'svg', 'hr', 'br', 'table')
        opening = node['opening']
        if citation or (classes & {'source-citation', 'chapter-citations'} and not visible):
            # Keep the identity for saved edits and offline source audit, but no
            # glyph, tab stop, or empty citation-only row in the document.
            if 'hidden' not in attrs:
                opening = opening[:-1] + ' hidden>'
        return opening + ''.join(part[0] for part in children) + node['closing'], visible


def citation_free_body(html):
    parser = _CitationPresentation()
    parser.feed(html)
    parser.close()
    return ''.join(parser.render(node)[0] for node in parser.root['children'])
