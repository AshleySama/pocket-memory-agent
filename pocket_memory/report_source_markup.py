# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Inert display markup for source excerpts; never edits evidence or offsets."""
from html import escape
from html.parser import HTMLParser
import re

from markdown_it import MarkdownIt
from markdown_it.rules_block.table import escapedSplit, getLine, table as parse_table
from markdown_it.token import Token


_TAGS = frozenset('p div section article aside span strong b em i u s strike mark '
    'small sub sup ul ol li blockquote br hr table caption thead tbody tfoot tr th td '
    'pre code h1 h2 h3 h4 h5 h6 dl dt dd'.split())
_BLOCKS = frozenset('p div section article aside ul ol li blockquote table caption '
    'thead tbody tfoot tr th td pre h1 h2 h3 h4 h5 h6 dl dt dd hr'.split())
_DROP = frozenset('script style iframe object embed svg math canvas audio video '
    'template noscript textarea select'.split())
_VOID = frozenset('br hr img input meta link source track embed area base wbr col param'.split())
_CLASSES = frozenset('source-field source-field-label source-steps source-tip compiled-table'.split())
_FIELD = re.compile(r'^([^：:\n]{1,14}[：:])\s*(.+)$', re.S)
_STEP = re.compile(r'^([ \t]*)(\d{1,9})[.)、]([ \t]+)(.*)$')


class _InertMarkup(HTMLParser):
    """Allow text formatting only, with no URLs, resources or citation identity."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.dropped = []

    def _close_from(self, index):
        for _, rendered in reversed(self.stack[index:]):
            self.out.append('</' + rendered + '>')
        del self.stack[index:]

    def handle_starttag(self, tag, attrs):
        if self.dropped:
            if tag not in _VOID:
                self.dropped.append(tag)
            return
        if tag in _DROP:
            if tag not in _VOID:
                self.dropped.append(tag)
            return
        raw = dict(attrs)
        if tag == 'img':
            # Keep an image's supplied text, never a remote resource request.
            self.out.append(escape(raw.get('alt') or ''))
            return
        if tag not in _TAGS:
            return
        if tag in _BLOCKS:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == 'p':
                    self._close_from(index)
                    break
        # HTML permits omitted closing tags for common list/table elements.
        # Close siblings, but never close an outer list item through a nested list.
        boundaries = {'li': {'ul', 'ol'}, 'dt': {'dl'}, 'dd': {'dl'},
                      'tr': {'table', 'thead', 'tbody', 'tfoot'}, 'td': {'tr'}, 'th': {'tr'}}
        if tag in boundaries:
            peers = {'td', 'th'} if tag in ('td', 'th') else {'dt', 'dd'} if tag in ('dt', 'dd') else {tag}
            for index in range(len(self.stack) - 1, -1, -1):
                previous = self.stack[index][0]
                if previous in boundaries[tag]:
                    break
                if previous in peers:
                    self._close_from(index)
                    break
        rendered = 'h' + str(max(3, min(5, int(tag[1])))) if re.fullmatch(r'h[1-6]', tag) else tag
        accepted = []
        classes = [name for name in (raw.get('class') or '').split() if name in _CLASSES]
        if tag == 'ol' and 'source-steps' not in classes:
            classes.append('source-steps')
        if tag == 'table' and 'compiled-table' not in classes:
            classes.append('compiled-table')
        if classes:
            accepted.append(('class', ' '.join(dict.fromkeys(classes))))
        for name in ('start',) if tag == 'ol' else ('value',) if tag == 'li' else ('colspan', 'rowspan') if tag in ('th', 'td') else ():
            value = raw.get(name) or ''
            if re.fullmatch(r'\d{1,9}', value):
                accepted.append((name, value))
        if tag == 'ol' and not any(name == 'start' for name, _ in accepted):
            accepted.append(('start', '1'))
        self.out.append('<' + rendered + ''.join(' ' + name + '="' + escape(value, quote=True) + '"'
                                                for name, value in accepted) + '>')
        if tag not in _VOID:
            self.stack.append((tag, rendered))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.dropped:
            if tag in self.dropped:
                del self.dropped[len(self.dropped) - 1 - self.dropped[::-1].index(tag):]
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self._close_from(index)
                break

    def handle_data(self, value):
        if not self.dropped:
            if not any(tag in ('pre', 'code') for tag, _ in self.stack):
                value = _wiki_labels(value)
            self.out.append(escape(value))

    def result(self):
        self.close()
        self._close_from(0)
        return ''.join(self.out)


def _safe_html(value):
    parser = _InertMarkup()
    parser.feed(value)
    return parser.result()


def _parser():
    # HTML is parsed as input, then reduced to inert formatting by _safe_html.
    # Linkification and typographic substitutions would alter source wording.
    parser = MarkdownIt('commonmark', dict(html=True, breaks=True, linkify=False,
        typographer=False)).enable(['table', 'strikethrough'])
    parser.block.ruler.at('table', _lossless_table, dict(alt=['paragraph', 'reference']))
    return parser


def _table_columns(line):
    # Match the parser's handling of optional edge pipes and escaped cell pipes.
    cells = escapedSplit(line.strip())
    if cells and cells[0] == '':
        cells.pop(0)
    if cells and cells[-1] == '':
        cells.pop()
    return len(cells)


def _lossless_table(state, start_line, end_line, silent):
    """Keep a nonrectangular Markdown table as text instead of dropping cells."""
    first_token = len(state.tokens)
    if not parse_table(state, start_line, end_line, silent):
        return False
    if silent:
        return True
    first_line, last_line = state.tokens[first_token].map
    columns = _table_columns(getLine(state, first_line))
    if any(_table_columns(getLine(state, line)) != columns
           for line in range(first_line + 2, last_line)):
        # getLine uses the parser's active list/blockquote offsets. The actual
        # table map keeps the replacement bounded to precisely the parsed block.
        original = '\n'.join(getLine(state, line) for line in range(first_line, last_line))
        del state.tokens[first_token:]
        token = state.push('code_block', 'code', 0)
        token.map = [first_line, last_line]
        token.content = original + '\n'
    return True


def format_outside_code(value, formatter):
    """Apply existing display line-wrap repairs only outside literal code."""
    protected = set()
    for token in _parser().parse(value):
        code = token.type in ('fence', 'code_block')
        html_code = token.type == 'html_block' and re.search(r'<(?:pre|code)\b', token.content, re.I)
        if (code or html_code) and token.map:
            protected.update(range(*token.map))
    lines = value.splitlines(keepends=True)
    result = []
    pending = []
    for index, line in enumerate(lines):
        if index in protected:
            if pending:
                result.append(formatter(''.join(pending)))
                pending.clear()
            result.append(line)
        else:
            pending.append(line)
    if pending:
        result.append(formatter(''.join(pending)))
    return ''.join(result)


def _wiki_labels(value):
    return re.sub(r'\[\[([^\[\]\r\n]+)\]\]', r'《\1》', value)


def render_inline(value):
    return _safe_html(_parser().renderInline(value))


def _source_lists(value, parser):
    """Keep existing Chinese step markers and non-one starting steps readable.

    Only display input changes. Fenced/indented code and literal HTML retain
    their contents, so a code example cannot accidentally become a list.
    """
    protected = set()
    for token in parser.parse(value):
        if token.type in ('fence', 'code_block', 'html_block') and token.map:
            protected.update(range(*token.map))
    lines = value.splitlines(keepends=True)
    result = []
    for index, line in enumerate(lines):
        match = _STEP.match(line.rstrip('\r\n')) if index not in protected else None
        if match:
            previous = lines[index - 1].rstrip('\r\n') if index else ''
            if (index and previous.strip() and len(match[1]) < 4
                    and index - 1 not in protected and not _STEP.match(previous)
                    and not re.match(r'^\s*(?:[-+*]\s|#{1,6}\s|>)', previous)):
                result.append('\n')
            ending = line[len(line.rstrip('\r\n')):]
            line = match[1] + match[2] + '.' + match[3] + match[4] + ending
        result.append(line)
    return ''.join(result)


def _html_token(value):
    token = Token('html_inline', '', 0)
    token.content = value
    return token


def render_body(value):
    parser = _parser()
    tokens = parser.parse(_source_lists(value, parser))
    for index, token in enumerate(tokens):
        if token.type == 'list_item_open' and token.info.isdigit():
            token.attrSet('value', token.info)
        elif token.type == 'blockquote_open':
            token.attrSet('class', 'source-tip')
        elif token.type == 'paragraph_open' and token.level == 0 and index + 2 < len(tokens):
            body = tokens[index + 1]
            field = _FIELD.match(body.content) if body.type == 'inline' else None
            if field:
                token.attrSet('class', 'source-field')
                body.children = ([_html_token('<strong class="source-field-label">')]
                    + (parser.parseInline(field[1])[0].children or [])
                    + [_html_token('</strong><span>')]
                    + (parser.parseInline(field[2])[0].children or [])
                    + [_html_token('</span>')])
    return _safe_html(parser.renderer.render(tokens, parser.options, {}))
