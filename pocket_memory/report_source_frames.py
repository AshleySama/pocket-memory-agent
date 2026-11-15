# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, lossless source groups for report selection.

Returns ``(frames, omissions)``. Every non-whitespace source character belongs
to a frame range or an inherited, proven-watermark omission. Text is never
rewritten. Multi-unit text is serialized by joining exact ranges with ``\n``;
the top-level unit/start/end is only the first navigation anchor, and ranges
is always the complete citation identity.

Roles and kinds describe visible source structure, not truth or support.
Unknown roles, unclosed conditions and oversized groups remain available;
callers must not silently truncate them or claim semantic verification.
"""
from __future__ import annotations

import re


TARGET_CHARS = 550
_ROLE_LABELS = (
    ('quoted', re.compile(r'^(?:普通人(?:的)?(?:回答)?|初答|初步回答|原回答|'
                          r'错误(?:回答|示例|示范|说法)|反例|被质疑的回答)[：:]?$')),
    ('candidate', re.compile(r'^(?:高手(?:的)?(?:回答)?|修正(?:后)?(?:的)?回答|'
                             r'更正(?:后)?(?:的)?回答|正确回答|参考答案|正式(?:说明|回答))[：:]?$')),
    ('narrative', re.compile(r'^(?:开场(?:白)?|开头|引言|导语|结尾|结语|结束语|'
                             r'面试点评|点评|题外话)[：:]?$')),
)
_HEADING = re.compile(r'^(?:#{1,6}[ \t]+.+|第[一二三四五六七八九十百零〇\d]+[章节部分].+)$')
_NARRATIVE_PREFIX = re.compile(
    r'^(?:Hi\b|Hello\b|大家好|你好|我(?:是|们|认为|觉得|花|反问)|你(?:们|知道|觉得)|'
    r'他(?:说|们|要)|她(?:说|们)|那(?:么|请问)?|比如|例如|好吧|好的|嗯|额|'
    r'今天|本期|一个|一位|有个|有人|关于|对于|以上|另外|最后|首先|其次)', re.I)
_CONTINUATION_PREFIX = re.compile(r'^(?:它|这(?:个|种|些)?|该|其|而|由于|所以|因为|'
                                  r'如果|否则|但是|并且|同时|为了|从而|总的来说)')
_LIST_LINE = re.compile(
    r'(?m)^[ \t]*(?:[-*•][ \t]+|[（(][一二三四五六七八九十百\d]+[）)][ \t]*|'
    r'[一二三四五六七八九十百]+[、．][ \t]*|\d{1,3}[、）)][ \t]*|'
    r'\d{1,3}[.．](?:[ \t]+|(?=[^\d\s]))|第[一二三四五六七八九十]+[，、：:])')
_LIST_LEAD = re.compile(r'(?:如下|以下(?:几|[一二三四五六七八九十两\d]+)?(?:点|项|步|方面)?|'
                        r'[一二三四五六七八九十两\d]+(?:个方面|种(?:情况|机制|方式)|个(?:步骤|条件)|项|步))')
_CONDITION = re.compile(r'如果|否则|只有|除非|只要|倘若|假如|若是|若(?=[，、：:\s])|'
                        r'(?m:^[ \t]*(?:若|仅(?:当|在|有|对)?))|仅当|仅在|但(?:是)?|不过|然而')
_CONDITIONAL_FOLLOW = re.compile(r'^[ \t\n]*(?:否则|反之|相反|而|但(?:是)?|然而|只有|仅|'
                                  r'这些|以上|上述|此时|这种|该(?:情况|方式|条件))')
_SENTENCE_END = re.compile(r'[。！？]+[”’」』）)]*')
_CLOSED = re.compile(r'[。！？][”’」』）)]*\s*$')


def _role_label(text):
    # Markdown syntax is presentation only; the original marker stays a frame.
    clean = re.sub(r'^#{1,6}[ \t]+', '', text.strip()).strip()
    for role, pattern in _ROLE_LABELS:
        if pattern.fullmatch(clean):
            return role
    return None


def _stream(unit):
    # Continue roles across PDF pages, never across unrelated documents or notes.
    if unit.get('kind') == 'document':
        return 'document', unit.get('source_id')
    return 'unit', unit['unit_id']


def _range(unit, start, end):
    return dict(unit_id=unit['unit_id'], start=start, end=end,
                revision=unit['revision'], text=unit['body'][start:end],
                page=unit.get('tab', ''), title=unit.get('title', ''),
                field=unit.get('field'), source_id=unit.get('source_id'),
                kind=unit.get('kind'))


def _page_topic(unit, visible_end):
    """Choose a compact title run in the page's pre-answer prefix, once.

    Scan the entire prefix up to the first initial/formal answer label: the
    preceding topic's closing prose may occupy the first several lines. A title
    can wrap over multiple lines. Later rhetorical questions and answer-body
    questions never change the topic. Ambiguous continuations keep the prior
    hint instead of guessing a new subject.
    """
    body = unit['body'][:visible_end]
    lines = list(re.finditer(r'[^\n]+(?:\n|$)|\n', body))
    stop = next((i for i, m in enumerate(lines)
                 if _role_label(m.group().strip()) in ('quoted', 'candidate')), None)
    prefix = lines if stop is None else lines[:stop]

    def compact(text, first=False):
        if not 1 <= len(text) <= 85 or _role_label(text):
            return False
        if re.search(r'[，。；：,;:]|[.!！]$', text) or _NARRATIVE_PREFIX.search(text):
            return False
        if _LIST_LINE.search(text):
            return False
        if first and (len(text) < 4 or _CONTINUATION_PREFIX.search(text)):
            return False
        return True

    for i, line in enumerate(prefix):
        clean = line.group().strip()
        if not clean:
            continue
        explicit = bool(_HEADING.fullmatch(clean))
        # Without a local answer-role boundary only an explicit first heading
        # is trusted; a normal continuation page is not a new topic.
        if stop is None and (i != 0 or not explicit):
            continue
        if not explicit and not compact(clean, first=True):
            continue
        if i and not explicit:
            previous = prefix[i-1].group().strip()
            if previous and not re.search(r'[。！？!][”’」』）)]*$', previous) and not _role_label(previous):
                continue
        last = i
        total = len(clean)
        # Complete title sampling happens before a role is encountered. Do not
        # stop after a question mark: "ThreadLocal 是什么？ / 它的实现原理 / 呢？"
        # is one wrapped title, not three unrelated topics.
        for following in range(i+1, min(i+4, len(prefix))):
            next_text = prefix[following].group().strip()
            if not compact(next_text) or total + len(next_text) > 160:
                break
            total += len(next_text)
            last = following
        leading = len(line.group()) - len(line.group().lstrip())
        end_line = prefix[last]
        start = line.start() + leading
        end = end_line.end() - (len(end_line.group()) - len(end_line.group().rstrip()))
        return _range(unit, start, end)
    return None


def _unit_events(unit, visible_end, visible_start=0):
    """Yield exact body/heading ranges, including titles in a page's middle.

    An explicit PDF structure receipt (also an unavailable one) suppresses
    textual guessing. Thus a continuation-page question cannot silently
    replace the chapter identified from the original PDF font hierarchy.
    """
    headings = []
    if 'pdf_structure' in unit or 'structural_headings' in unit:
        for heading in unit.get('structural_headings', []):
            a, b = heading['start'], heading['end']
            if not (0 <= a < b <= len(unit['body'])
                    and heading.get('revision') == unit['revision']
                    and heading.get('text') == unit['body'][a:b]):
                raise ValueError('PDF 标题与保存原文的身份或区间不一致')
            if visible_start <= a and b <= visible_end:
                headings.append(_range(unit, a, b))
    else:
        guessed = _page_topic(unit, visible_end)
        if guessed and guessed['start'] >= visible_start:
            headings.append(guessed)
    headings.sort(key=lambda item: item['start'])
    cursor = visible_start

    def body_events(start, end):
        for match in re.finditer(r'[^\n]+(?:\n|$)|\n', unit['body'][start:end]):
            yield False, _range(unit, start+match.start(), start+match.end())

    for heading in headings:
        if heading['start'] < cursor:
            raise ValueError('PDF 标题区间重叠')
        yield from body_events(cursor, heading['start'])
        yield True, heading
        cursor = heading['end']
    yield from body_events(cursor, visible_end)


def _merge_ranges(ranges):
    result = []
    for item in ranges:
        if not item['text']:
            continue
        if result and result[-1]['unit_id'] == item['unit_id'] and result[-1]['end'] == item['start']:
            result[-1]['end'] = item['end']
            result[-1]['text'] += item['text']
        else:
            result.append(dict(item))
    return result


def _flatten(ranges):
    text = ''; positions = []
    for item in _merge_ranges(ranges):
        if text:
            text += '\n'
        start = len(text)
        text += item['text']
        positions.append((start, len(text), item))
    return text, positions


def _slice_ranges(positions, start, end):
    result = []
    for a, b, item in positions:
        left, right = max(a, start), min(b, end)
        if left >= right:
            continue
        piece = dict(item, start=item['start'] + left-a, end=item['start'] + right-a,
                     text=item['text'][left-a:right-a])
        if piece['text'].strip():
            result.append(piece)
    return _merge_ranges(result)


def _kind(text):
    if _CONDITION.search(text):
        return 'condition_pair'
    if _LIST_LINE.search(text) or _LIST_LEAD.search(text):
        return 'list'
    if re.search(r'定义|是指|指的是|称为|含义|所谓', text):
        return 'definition'
    if re.search(r'机制|原理|流程|步骤|实现|通过', text):
        return 'mechanism'
    return 'statement'


def _indivisible(text):
    return bool(_CONDITION.search(text) or _LIST_LINE.search(text) or _LIST_LEAD.search(text))


def _frame_parts(block):
    text, positions = _flatten(block['ranges'])
    if not text.strip():
        return []
    if block.get('marker') or _indivisible(text):
        return [(text, _merge_ranges(block['ranges']), not bool(_CLOSED.search(text)))]
    # Split only at complete Chinese sentences. PDF line wraps, decimal dots,
    # semicolons and a bare newline never authorize cutting a sentence.
    ends = [match.end() for match in _SENTENCE_END.finditer(text)]
    if not ends:
        return [(text, _merge_ranges(block['ranges']), True)]
    if text[ends[-1]:].strip():
        # Keep a trailing open clause with its preceding sentence.
        ends[-1] = len(text)
    else:
        ends[-1] = len(text)
    parts = []; start = 0; cap = 0
    for end in ends:
        if cap > start and end-start > TARGET_CHARS:
            selected = _slice_ranges(positions, start, cap)
            shown, _ = _flatten(selected)
            parts.append((shown, selected, not bool(_CLOSED.search(shown))))
            start = cap
        cap = end
    if cap > start:
        selected = _slice_ranges(positions, start, cap)
        shown, _ = _flatten(selected)
        parts.append((shown, selected, not bool(_CLOSED.search(shown))))
    return parts


def _prove_coverage(units, frames, omissions):
    intervals = {unit['unit_id']: [] for unit in units}
    originals = {unit['unit_id']: unit for unit in units}
    for item in frames:
        for span in item['ranges']:
            unit = originals[span['unit_id']]
            a, b = span['start'], span['end']
            if not (0 <= a < b <= len(unit['body']) and span['revision'] == unit['revision']
                    and unit['body'][a:b] == span['text']):
                raise ValueError('事实单元的原文身份或区间不一致')
            intervals[span['unit_id']].append((a, b))
    for span in omissions:
        unit = originals[span['unit_id']]
        a, b = span['start'], span['end']
        if not (0 <= a < b <= len(unit['body']) and unit['body'][a:b] == span['text']
                and span['revision'] == unit['revision']):
            raise ValueError('跳过区间缺少原文收据')
        intervals[span['unit_id']].append((a, b))
    for unit in units:
        cursor = 0
        for a, b in sorted(intervals[unit['unit_id']]):
            if a < cursor:
                raise ValueError('事实单元或跳过区间重复覆盖原文')
            if unit['body'][cursor:a].strip():
                raise ValueError('事实单元之间遗漏非空白原文')
            cursor = b
        if unit['body'][cursor:].strip():
            raise ValueError('事实单元未覆盖原文尾部')


def _watermark_omissions(units, inherited):
    """Union old tail receipts and exact PDF decoration ranges, once each."""
    by_id = {unit['unit_id']: unit for unit in units}
    candidates = {unit['unit_id']: [] for unit in units}
    for span in inherited:
        candidates[span['unit_id']].append(dict(span))
    for unit in units:
        for span in unit.get('structural_omissions', []):
            a, b = span['start'], span['end']
            if not (span.get('unit_id') == unit['unit_id']
                    and span.get('revision') == unit['revision']
                    and 0 <= a < b <= len(unit['body'])
                    and span.get('text') == unit['body'][a:b]
                    and span.get('reason') == 'proven_pdf_watermark'
                    and span.get('proof', {}).get('method') == 'same_page_full_chunk_font_mapping'):
                raise ValueError('PDF 水印缺少匹配保存原文的结构收据')
            candidates[unit['unit_id']].append(dict(span))
    result = []
    for unit_id, spans in candidates.items():
        unit = by_id[unit_id]
        groups = []
        for span in sorted(spans, key=lambda item: (item['start'], item['end'])):
            a, b = span['start'], span['end']
            if not 0 <= a < b <= len(unit['body']):
                raise ValueError('水印区间超出保存原文')
            if groups and a <= groups[-1]['end']:
                groups[-1]['end'] = max(groups[-1]['end'], b)
                groups[-1]['receipts'].append(span)
            else:
                groups.append(dict(start=a, end=b, receipts=[span]))
        for group in groups:
            reasons = list(dict.fromkeys(span['reason'] for span in group['receipts']))
            result.append(dict(_range(unit, group['start'], group['end']),
                               reason=reasons[0] if len(reasons) == 1 else 'proven_watermark_union',
                               reasons=reasons, proof_receipts=group['receipts'],
                               retained_in_original=True, semantic_verified=False))
    return result


def _visible_intervals(unit, omissions):
    cursor = 0
    for span in omissions:
        if span['unit_id'] != unit['unit_id']:
            continue
        if cursor < span['start']:
            yield cursor, span['start']
        cursor = span['end']
    if cursor < len(unit['body']):
        yield cursor, len(unit['body'])


def source_frames(units):
    """Return exact source frames and omission receipts; do not call a model.

    ``role`` is candidate/quoted/narrative/unknown. Only an explicit formal
    role label yields candidate; it still does NOT mean the statement is true.
    ``kind`` is a structural hint, not a technical-topic classifier. All frames
    carry ranges, and groups exceeding 550 characters are retained oversized.
    """
    from .report_digest import source_spans

    if len({u['unit_id'] for u in units}) != len(units):
        raise ValueError('原文单元身份重复')
    _, old_omissions = source_spans(units)
    omissions = _watermark_omissions(units, old_omissions)

    blocks = []; pending = []; state = None
    role = 'unknown'; topic = ''; topic_range = None

    def flush(marker=False, marker_role=None, sets_role=None):
        nonlocal pending
        if not pending:
            return
        text, _ = _flatten(pending)
        if text.strip():
            blocks.append(dict(ranges=_merge_ranges(pending), role=marker_role or role,
                               topic_hint=topic, topic_range=dict(topic_range) if topic_range else None,
                               marker=marker, sets_role=sets_role, stream=state))
        pending = []

    for unit in units:
        stream = _stream(unit)
        if stream != state:
            flush(); state = stream; role = 'unknown'; topic = ''; topic_range = None
        # A pure watermark page has no events: inherited role/topic is retained.
        events = (event for start, end in _visible_intervals(unit, omissions)
                  for event in _unit_events(unit, end, start))
        for is_heading, item in events:
            line = item['text']; clean = line.strip()
            if is_heading:
                flush()
                topic_range = item
                topic = item['text']
                # A title must never promote an initial/wrong answer to fact.
                if role != 'quoted':
                    role = 'unknown'
                pending = [dict(item)]
                flush(marker=True, marker_role='narrative')
                continue
            if not clean:
                pending_text, _ = _flatten(pending)
                if pending_text and _CLOSED.search(pending_text) and not _indivisible(pending_text):
                    flush()
                # Whitespace has no factual content and needs no omission claim.
                continue
            label = _role_label(clean)
            if label:
                flush(); pending = [item]
                flush(marker=True, marker_role='narrative', sets_role=label)
                role = label
                continue
            # A new paragraph beginning "otherwise/these..." stays with the
            # preceding block when the role/topic/document identity is unchanged.
            if not pending and blocks and _CONDITIONAL_FOLLOW.search(clean):
                previous = blocks[-1]
                if (not previous['marker'] and previous['stream'] == state
                        and previous['role'] == role and previous['topic_hint'] == topic):
                    pending = blocks.pop()['ranges']
            pending.append(item)
    flush()

    frames = []
    for block in blocks:
        for text, ranges, uncertain in _frame_parts(block):
            if not text.strip():
                continue
            first = ranges[0]
            frames.append(dict(id=f'F{len(frames)+1}', unit_id=first['unit_id'],
                               start=first['start'], end=first['end'], revision=first['revision'],
                               text=text, ranges=ranges, page=first['page'], title=first['title'],
                               role=block['role'], kind=_kind(text), topic_hint=block['topic_hint'],
                               topic_range=block['topic_range'], is_structure_marker=block['marker'],
                               sets_role=block['sets_role'], oversized=len(text)>TARGET_CHARS,
                               oversize=len(text)>TARGET_CHARS, structure_uncertain=uncertain,
                               indivisible=True, semantic_verified=False,
                               cross_unit=len({r['unit_id'] for r in ranges})>1,
                               serialization='exact_ranges_joined_with_newline'))
    _prove_coverage(units, frames, omissions)
    return frames, omissions
