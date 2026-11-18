# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Conservative, zero-generation plans from explicit source headings.

This module never edits a source or registers a citation. ``None`` means the
ordinary report path must decide how to organize the material. A returned plan
retains exact source ranges, including its section headings; ``title`` is only
a display label. Unsupported metadata is retained rather than guessed away.
"""
from __future__ import annotations

import json
import re
from collections import Counter


PROTOCOL = 'explicit-source-headings-v1'
_HEADING = re.compile(r'^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_NUMBER_TITLE = re.compile(r'(?:\d+[.．、)]|[（(]\d+[）)]|[一二三四五六七八九十百]+[、．])')
_WIKI_LINE = re.compile(r'(?:\[\[[^\[\]\r\n]+\]\][ \t]*)+')
_YAML_KEY = re.compile(r'([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:[ \t]*(.*)')
_DECIMAL_BREAK = re.compile(r'(?<![\d.])\d+\.[ \t]*\r?\n[ \t]*\d+')


def _yaml_value(value, nested=False):
    """Recognize a small, unambiguous subset of flat YAML scalar values."""
    value = value.strip()
    if not value:
        return True
    if value.startswith('"'):
        try:
            return isinstance(json.loads(value), str)
        except (ValueError, TypeError):
            return False
    if value.startswith("'"):
        return bool(re.fullmatch(r"'(?:[^']|'')*'", value))
    if value.startswith('['):
        if nested or not value.endswith(']'):
            return False
        inner = value[1:-1].strip()
        return not inner or all(_yaml_value(part, True) for part in inner.split(','))
    if value[0] in '{}[]!&*|>@`' or ': ' in value or '\t' in value:
        return False
    return not any(char in value for char in '[]{}')


def _flat_frontmatter(lines):
    """Only prove simple top-level mappings; complex/invalid YAML stays visible."""
    keys = set()
    for line in lines:
        text = line['content']
        if not text.strip() or text.lstrip().startswith('#'):
            continue
        match = _YAML_KEY.fullmatch(text)
        if not match or match.group(1) in keys or not _yaml_value(match.group(2)):
            return False
        keys.add(match.group(1))
    return bool(keys)


def _heading(line):
    if line['code']:
        return None
    match = _HEADING.fullmatch(line['content'])
    if not match:
        return None
    title = re.sub(r'[ \t]+#+[ \t]*$', '', match.group(2)).strip()
    return (len(match.group(1)), title) if title else None


def _lines(units):
    result = []
    fence = None
    for unit in units:
        for match in re.finditer(r'[^\n]*\n|[^\n]+$', unit['body']):
            content = match.group().rstrip('\r\n')
            if not result:
                content = content.lstrip('\ufeff')
            marker = _FENCE.fullmatch(content)
            code = fence is not None
            if fence:
                if (marker and marker.group(1)[0] == fence[0]
                        and len(marker.group(1)) >= fence[1] and not marker.group(2).strip()):
                    fence = None
            elif marker:
                fence = marker.group(1)[0], len(marker.group(1))
                code = True
            result.append(dict(unit=unit, start=match.start(), end=match.end(),
                               content=content, code=code))
    return result


def _noise(lines, kind):
    reasons = {}
    first = next((index for index, line in enumerate(lines) if line['content'].strip()), None)
    if first is not None and lines[first]['content'].strip() == '---':
        closing = next((index for index in range(first + 1, len(lines))
                        if lines[index]['content'].strip() in ('---', '...')), None)
        if closing is not None and _flat_frontmatter(lines[first + 1:closing]):
            for index in range(first, closing + 1):
                reasons[index] = 'yaml_frontmatter'
    for index, line in enumerate(lines):
        if not line['code'] and _WIKI_LINE.fullmatch(line['content'].strip()):
            reasons.setdefault(index, 'standalone_wiki_navigation')

    # A repeated H1 is a page header only at the first visible line of at least
    # two document units. The same text inside prose is never removed.
    if kind == 'document':
        first_by_unit = {}
        headers = {}
        for index, line in enumerate(lines):
            uid = line['unit']['unit_id']
            if index in reasons or not line['content'].strip() or uid in first_by_unit:
                continue
            first_by_unit[uid] = index
            heading = _heading(line)
            if heading and heading[0] == 1:
                headers.setdefault(heading[1], []).append(index)
        for indices in headers.values():
            if len(indices) >= 2:
                for index in indices:
                    reasons[index] = 'repeated_document_header'
    return reasons


def _title_continuation(line):
    text = line['content'].strip()
    if (line['code'] or not text or len(text) > 120 or _heading(line)
            or re.match(r'^(?:[-*+][ \t]|[>|]|\d+[.、．)][ \t]|---$)', text)
            or re.search(r'[。；;！!]$', text) or re.search(r'[：:]\s*\S', text)):
        return None
    return text


def _headings(lines, noise):
    result = []
    for index, line in enumerate(lines):
        heading = None if index in noise else _heading(line)
        if heading is None:
            continue
        level, title = heading
        last = index
        if _NUMBER_TITLE.fullmatch(title):
            following = next((i for i in range(index + 1, len(lines))
                              if i not in noise and lines[i]['content'].strip()), None)
            continuation = _title_continuation(lines[following]) if following is not None else None
            if continuation is None:
                continue
            title += ' ' + continuation
            last = following
        result.append(dict(index=index, last=last, level=level, title=title))
    return result


def _append_range(target, unit, start, end, reason=None):
    if start == end:
        return
    if (target and target[-1]['unit_id'] == unit['unit_id']
            and target[-1]['end'] == start and target[-1].get('reason') == reason):
        target[-1]['end'] = end
        target[-1]['text'] += unit['body'][start:end]
        return
    span = dict(unit_id=unit['unit_id'], start=start, end=end, text=unit['body'][start:end])
    if reason:
        span['reason'] = reason
    target.append(span)


def _nonspace(text):
    return sum(not char.isspace() for char in text)


def _coverage(units, chapters, omissions, level):
    retained = [span for chapter in chapters for span in chapter['segments']]
    ranges = {unit['unit_id']: [] for unit in units}
    for span in retained + omissions:
        ranges[span['unit_id']].append(span)
    for unit in units:
        cursor = 0
        for span in sorted(ranges[unit['unit_id']], key=lambda value: value['start']):
            start, end = span['start'], span['end']
            if not (cursor <= start < end <= len(unit['body'])
                    and span['text'] == unit['body'][start:end]):
                raise ValueError('结构成稿的原文范围重复或不匹配')
            if unit['body'][cursor:start].strip():
                raise ValueError('结构成稿遗漏非空白原文')
            cursor = end
        if unit['body'][cursor:].strip():
            raise ValueError('结构成稿遗漏原文尾部')
    issues = []
    for unit in units:
        for match in _DECIMAL_BREAK.finditer(unit['body']):
            issues.append(dict(kind='possible_split_decimal', unit_id=unit['unit_id'],
                               start=match.start(), end=match.end(), text=match.group(),
                               uncertain=True,
                               message='疑似小数被断行；保留原文，未自动合并或改写数值。'))
    total = sum(_nonspace(unit['body']) for unit in units)
    kept = sum(_nonspace(span['text']) for span in retained)
    omitted = sum(_nonspace(span['text']) for span in omissions)
    return dict(protocol=PROTOCOL, total_units=len(units), heading_level=level,
                total_chars=sum(len(unit['body']) for unit in units),
                total_nonspace_chars=total, retained_nonspace_chars=kept,
                omitted_nonspace_chars=omitted, covered_nonspace_chars=kept + omitted,
                complete=kept + omitted == total, model_calls=0, display_issues=issues)


def plan(units, focus=''):
    """Return exact ``chapters/omissions/coverage`` or defer with ``None``.

    Eligibility is deliberately narrow: no extra user focus, one source, no
    OCR channels, and at least two distinct explicit peer Markdown headings.
    Lower-level headings and continuation text stay in their parent section.
    A source's headings and facts are not a certification of factual accuracy.
    """
    if not isinstance(focus, str) or focus.strip() or not isinstance(units, (list, tuple)) or not units:
        return None
    if any(not isinstance(unit, dict) or not isinstance(unit.get('body'), str)
           or not isinstance(unit.get('unit_id'), str)
           or unit.get('kind') not in ('document', 'note')
           or unit.get('source_id') is None
           or unit.get('field') not in ('document_text', 'content') for unit in units):
        return None
    if len({unit['unit_id'] for unit in units}) != len(units):
        return None
    sources = {(unit['kind'], unit['source_id']) for unit in units}
    if len(sources) != 1:
        return None
    lines = _lines(units)
    noise = _noise(lines, units[0]['kind'])
    headings = _headings(lines, noise)
    counts = Counter(heading['level'] for heading in headings)
    levels = [level for level, count in counts.items() if count >= 2 and
              len({heading['title'] for heading in headings if heading['level'] == level}) >= 2]
    if not levels:
        return None
    level = min(levels)
    selected = [heading for heading in headings if heading['level'] == level]
    if any(heading['level'] < level and heading['index'] >= selected[0]['index'] for heading in headings):
        return None
    for position, heading in enumerate(selected):
        stop = selected[position + 1]['index'] if position + 1 < len(selected) else len(lines)
        if not any(index not in noise and lines[index]['content'].strip()
                   and _heading(lines[index]) is None for index in range(heading['last'] + 1, stop)):
            return None

    by_index = {heading['index']: heading for heading in selected}
    chapters = []
    current = dict(title='引言', segments=[])
    omissions = []
    for index, line in enumerate(lines):
        if index in noise:
            _append_range(omissions, line['unit'], line['start'], line['end'], noise[index])
            continue
        if index in by_index:
            if any(span['text'].strip() for span in current['segments']):
                chapters.append(current)
            current = dict(title=by_index[index]['title'], segments=[])
        _append_range(current['segments'], line['unit'], line['start'], line['end'])
    if any(span['text'].strip() for span in current['segments']):
        chapters.append(current)
    coverage = _coverage(units, chapters, omissions, level)
    return dict(chapters=chapters, omissions=omissions, coverage=coverage)
