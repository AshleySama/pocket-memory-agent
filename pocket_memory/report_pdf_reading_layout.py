# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Read-only PDF layout receipts tied to immutable source character ranges.

``build_layout(path, units)`` returns JSON-safe ``edits``, ``headings``,
``continuations``, ``units`` and ``receipt``. A page is eligible only when its
complete non-whitespace text is covered once by uniquely matching saved units.
Only proven margin page numbers may remove non-whitespace characters. All
other edits replace whitespace; no words, punctuation or numbers are guessed.

Continuations authorize joining whole adjacent unit displays, not evidence:
``from_end`` is len(previous body), ``to_start`` is zero. Their proof records
the actual last/first content ranges, after an independently proven page number.
Tables, code, rotation, columns and uncertain geometry retain the old layout.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re
from statistics import median


PROTOCOL = 'pdf-reading-layout-v1'
_ENDS = re.compile(r'[。！？!?；;.]([”’"\'）)】\]]*)$')
_LITERAL = re.compile(r'(?m)^\s*(?:`{3,}|~{3,})|<(?:pre|code|table)\b|\t|^\s*\|?.*\|.*\n\s*\|?\s*:?-{3,}', re.I)


def _normal(value):
    positions = [i for i, char in enumerate(value) if not char.isspace()]
    return ''.join(value[i] for i in positions), positions


def _page_number(unit):
    match = re.fullmatch(r'\s*第\s*(\d+)\s*页\s*', str(unit.get('tab', '')))
    return int(match[1]) if match else None


def _overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _rounded(box):
    return [round(float(value), 3) for value in box]


def _read_page(page, number):
    lines, characters, text_parts = [], [], []
    fonts = Counter()
    table_boxes = [list(table.bbox) for table in page.find_tables().tables]
    for block in page.get_text('dict').get('blocks', []):
        for raw in block.get('lines', []):
            pieces, attributes = [], []
            sizes, names = Counter(), Counter()
            spans = raw.get('spans', [])
            mono = False
            spaced = False
            previous = None
            for span in spans:
                value = str(span.get('text', ''))
                size = round(float(span.get('size', 0)), 2)
                font = str(span.get('font', ''))
                count = len(_normal(value)[0])
                sizes[size] += count
                names[font] += count
                fonts[size] += count
                mono = mono or bool(int(span.get('flags', 0)) & 8) or bool(re.search(r'courier|mono', font, re.I))
                box = span.get('bbox', (0, 0, 0, 0))
                if previous is not None and box[0] - previous[2] > max(2 * size, 20):
                    spaced = True
                previous = box
                pieces.append(value)
                attributes.extend([(font, size)] * len(value))
            text = ''.join(pieces)
            normalized, positions = _normal(text)
            if not normalized:
                continue
            box = _rounded(raw.get('bbox', (0, 0, 0, 0)))
            direction = raw.get('dir', (1, 0))
            rotated = abs(float(direction[1])) > .05 or float(direction[0]) < .95
            index = len(lines)
            line = dict(index=index, text=text, normalized=normalized, positions=positions,
                        bbox=box, size=max(sizes, key=sizes.get), font=max(names, key=names.get),
                        protected=rotated or mono or spaced or any(_overlap(box, t) for t in table_boxes),
                        normal_start=len(characters), normal_end=len(characters)+len(normalized))
            lines.append(line)
            characters.extend((index, pos, *attributes[pos]) for pos in positions)
            text_parts.append(normalized)
    positive = {size: count for size, count in fonts.items() if size > 0 and count}
    return dict(page=number, width=float(page.rect.width), height=float(page.rect.height),
                lines=lines, characters=characters, normalized=''.join(text_parts),
                body_size=max(positive, key=positive.get) if positive else 0,
                table_regions=len(table_boxes))


def _footer_candidates(pages):
    candidates = []
    for page in pages.values():
        for line in page['lines']:
            x0, y0, x1, y1 = line['bbox']
            if (line['normalized'] == str(page['page']) and not line['protected']
                    and line['size'] <= page['body_size'] * .85
                    and y0 >= page['height'] * .90
                    and abs((x0+x1)/2 - page['width']/2) <= page['body_size'] * 1.5):
                candidates.append((page, line))
    proven = {}
    for page, line in candidates:
        peers = [other['page'] for other, other_line in candidates
                 if abs(other_line['bbox'][1]/other['height'] - line['bbox'][1]/page['height']) <= .008
                 and abs(other_line['size'] - line['size']) <= .5]
        if len(set(peers)) >= 2:
            proven[(page['page'], line['index'])] = sorted(set(peers))
    return proven


def _flow(page, footers):
    size = page['body_size']
    content = [line for line in page['lines'] if (page['page'], line['index']) not in footers]
    if page['table_regions'] or size <= 0 or len(content) < 2:
        return None
    # PDF extraction order must also be an unambiguous vertical reading order.
    if any(b['bbox'][1] - a['bbox'][1] < size * .5 for a, b in zip(content, content[1:])):
        return None
    body = [line for line in content if not line['protected']
            and abs(line['size']-size) <= size*.08
            and line['bbox'][2]-line['bbox'][0] >= page['width']*.35]
    if len(body) < 3:
        return None
    left_counts = Counter(round(line['bbox'][0]) for line in body)
    left = max(left_counts, key=left_counts.get)
    aligned = [line for line in body if abs(line['bbox'][0]-left) <= size*.35]
    if len(aligned) < 3:
        return None
    steps = [b['bbox'][1]-a['bbox'][1] for a, b in zip(content, content[1:])
             if size*.8 <= b['bbox'][1]-a['bbox'][1] <= size*3]
    if not steps:
        return None
    font_counts = Counter()
    for line in body:
        font_counts[line['font']] += len(line['normalized'])
    return dict(left=float(left), right=median(line['bbox'][2] for line in aligned),
                step=median(steps), size=size, font=max(font_counts, key=font_counts.get),
                first=content[0], last=content[-1])


def _font_heading(line, page, flow, repeated):
    if line['protected'] or repeated[line['normalized']] > 1 or not 4 <= len(line['normalized']) <= 100:
        return False
    size = flow['size']
    centered = abs((line['bbox'][0]+line['bbox'][2])/2-page['width']/2) <= size*1.5
    return (line['font'] != flow['font'] and line['size'] >= size*1.1
            and (centered or line['size'] >= size*1.3))


def _same_line_gap(page, left, right):
    line = page['lines'][left[0]]
    gap = line['text'][left[1]+1:right[1]]
    # A font-distinguished inline section label is not ordinary wrapped prose.
    if left[2] != right[2] and re.search(r'[。！？!?：:]$', line['text'][:left[1]+1]):
        return '\n\n', 'pdf_inline_style_boundary'
    return gap, 'pdf_same_visual_line'


def _body_boundary(boundary, line, flow):
    """Classify the actual joining span, not a mixed line's majority font."""
    if abs(boundary[3]-flow['size']) > flow['size']*.08:
        return False
    if boundary[2] == flow['font']:
        return True
    # A body line may use a separate Latin/digit font at its edge. This is
    # supported only when the rest of that same line already establishes body
    # style; a distinct CJK title span is never accepted by this exception.
    return line['font'] == flow['font'] and line['text'][boundary[1]].isascii()


def _between_lines(previous, following, flow, heading_ids, left, right):
    if previous['protected'] or following['protected']:
        return None
    step = following['bbox'][1]-previous['bbox'][1]
    if step <= 0 or step > flow['step']*1.65:
        return ('\n\n', 'pdf_paragraph_spacing') if step > flow['step']*1.65 else None
    size = flow['size']
    if (previous['index'] in heading_ids or following['index'] in heading_ids
            or not _body_boundary(left, previous, flow) or not _body_boundary(right, following, flow)):
        return '\n\n', 'pdf_font_boundary'
    if following['bbox'][0] >= flow['left'] + size*.8:
        return '\n\n', 'pdf_first_line_indent'
    if (abs(following['bbox'][0]-flow['left']) <= size*.35
            and abs(left[3]-right[3]) <= size*.08
            and previous['bbox'][2] >= flow['right']-size*1.25):
        return '', 'pdf_continuation_line'
    return '\n\n', 'pdf_short_line_paragraph'


def _join_space(left, right):
    # Chinese joins need no added space. Keep Latin word boundaries, without
    # inventing a gap inside a numeric value split at a physical line ending.
    return ' ' if left.isascii() and right.isascii() and left.isalpha() and right.isalpha() else ''


def _unit_summary(unit, page, status, **extra):
    return dict(unit_id=unit.get('unit_id'), revision=unit.get('revision'), page=page,
                status=status, **extra)


def build_layout(path, units):
    """Build a conservative display receipt; never mutate units or their bodies."""
    result = dict(edits=[], headings=[], continuations=[], units=[],
                  receipt=dict(protocol=PROTOCOL, status='unavailable', matched_pages=0,
                               matched_units=0, edited_whitespace=0, removed_page_numbers=0))
    requested = list(units)
    if not Path(path).is_file():
        result['receipt']['reason'] = 'original_pdf_missing'
        result['units'] = [_unit_summary(u, _page_number(u), 'unavailable') for u in requested]
        return result
    try:
        import pymupdf
        grouped = defaultdict(list)
        for unit in requested:
            page_no = _page_number(unit)
            if (unit.get('kind') != 'document' or unit.get('field') != 'document_text'
                    or not page_no or not isinstance(unit.get('body'), str)
                    or not isinstance(unit.get('unit_id'), str)):
                result['units'].append(_unit_summary(unit, page_no, 'unavailable', reason='unsupported_source_identity'))
            else:
                grouped[page_no].append(unit)
        pages = {}
        with pymupdf.open(path) as document:
            result['receipt']['pdf_pages'] = len(document)
            for page_no in grouped:
                if 1 <= page_no <= len(document):
                    pages[page_no] = _read_page(document[page_no-1], page_no)
        footers = _footer_candidates(pages)
        repeated = Counter(line['normalized'] for page in pages.values() for line in page['lines'])
        matched = {}
        flows = {}
        for page_no, members in grouped.items():
            page = pages.get(page_no)
            mappings = []
            failure = 'pdf_page_unavailable' if page is None else None
            for unit in members:
                normalized, positions = _normal(unit['body'])
                at = page['normalized'].find(normalized) if page and normalized else -1
                if at < 0 or page['normalized'].find(normalized, at+1) >= 0:
                    failure = failure or 'whole_unit_not_uniquely_matched'
                    break
                mappings.append(dict(unit=unit, normalized=normalized, positions=positions,
                                     begin=at, end=at+len(normalized)))
            if not failure:
                cursor = 0
                for mapping in sorted(mappings, key=lambda m:m['begin']):
                    if mapping['begin'] != cursor:
                        failure = 'page_snapshot_has_gap_or_overlap'
                        break
                    cursor = mapping['end']
                if cursor != len(page['normalized']):
                    failure = 'page_snapshot_is_not_complete'
            if failure:
                result['units'].extend(_unit_summary(u, page_no, 'unavailable', reason=failure) for u in members)
                continue
            result['receipt']['matched_pages'] += 1
            flow = _flow(page, footers)
            flows[page_no] = flow
            heading_ids = {line['index'] for line in page['lines']
                           if flow and _font_heading(line, page, flow, repeated)}
            for mapping in mappings:
                unit, pos = mapping['unit'], mapping['positions']
                body = unit['body']; uid = unit['unit_id']
                literal = bool(_LITERAL.search(body))
                mapping.update(page=page, flow=flow, literal=literal, heading_ids=heading_ids)
                matched[uid] = mapping
                result['units'].append(_unit_summary(unit, page_no, 'matched',
                    matched_nonspace_chars=len(pos), pdf_start=mapping['begin'], pdf_end=mapping['end'],
                    reading_flow='literal_preserved' if literal else 'single_column' if flow else 'unavailable',
                    body_font_size=page['body_size'], table_regions=page['table_regions']))
                result['receipt']['matched_units'] += 1
                removed_ranges = []
                mapping['removed_ranges'] = removed_ranges
                if not flow or literal:
                    continue
                for line in page['lines']:
                    peers = footers.get((page_no, line['index']))
                    if not peers or not mapping['begin'] <= line['normal_start'] < line['normal_end'] <= mapping['end']:
                        continue
                    a = pos[line['normal_start']-mapping['begin']]
                    b = pos[line['normal_end']-mapping['begin']-1]+1
                    line_start = body.rfind('\n', 0, a)+1
                    newline = body.find('\n', b)
                    line_end = newline+1 if newline >= 0 else len(body)
                    if body[line_start:line_end].strip() != str(page_no):
                        continue
                    result['edits'].append(dict(unit_id=uid, start=line_start, end=line_end, replacement='',
                        reason='proven_pdf_page_number', proof=dict(page=page_no, bbox=line['bbox'],
                            font_size=line['size'], repeated_margin_pages=peers, whole_page_unique_match=True)))
                    removed_ranges.append((line_start, line_end))
                    result['receipt']['removed_page_numbers'] += 1
                for line in page['lines']:
                    if line['index'] in heading_ids and mapping['begin'] <= line['normal_start'] < line['normal_end'] <= mapping['end']:
                        a = pos[line['normal_start']-mapping['begin']]
                        b = pos[line['normal_end']-mapping['begin']-1]+1
                        result['headings'].append(dict(unit_id=uid, start=a, end=b, level=2,
                            reason='pdf_distinct_large_font', proof=dict(page=page_no, bbox=line['bbox'],
                                font_size=line['size'], body_font_size=flow['size'])))
                for index in range(len(pos)-1):
                    a, b = pos[index]+1, pos[index+1]
                    if any(start < b and end > a for start, end in removed_ranges):
                        continue
                    left = page['characters'][mapping['begin']+index]
                    right = page['characters'][mapping['begin']+index+1]
                    previous, following = page['lines'][left[0]], page['lines'][right[0]]
                    if ((page_no, left[0]) in footers or (page_no, right[0]) in footers
                            or previous['protected'] or following['protected']):
                        continue
                    if left[0] == right[0]:
                        if '\n' not in body[a:b] and '\r' not in body[a:b]:
                            continue
                        replacement, reason = _same_line_gap(page, left, right)
                    elif right[0] == left[0]+1:
                        decision = _between_lines(previous, following, flow, heading_ids, left, right)
                        if decision is None:
                            continue
                        replacement, reason = decision
                        if not replacement:
                            replacement = _join_space(body[a-1], body[b])
                    else:
                        continue
                    if body[a:b] != replacement:
                        result['edits'].append(dict(unit_id=uid, start=a, end=b,
                            replacement=replacement, reason=reason,
                            proof=dict(page=page_no, pdf_lines=[left[0]+1, right[0]+1],
                                       bboxes=[previous['bbox'], following['bbox']])))
                        result['receipt']['edited_whitespace'] += 1
        # Only selected adjacent pages of the same document stream may continue.
        for previous_unit, next_unit in zip(requested, requested[1:]):
            previous = matched.get(previous_unit.get('unit_id'))
            following = matched.get(next_unit.get('unit_id'))
            if not previous or not following or previous['literal'] or following['literal']:
                continue
            if (previous_unit.get('source_id') != next_unit.get('source_id')
                    or following['page']['page'] != previous['page']['page']+1
                    or not previous['flow'] or not following['flow']):
                continue
            left, right = previous['flow']['last'], following['flow']['first']
            left_edge = previous['page']['characters'][left['normal_end']-1]
            right_edge = following['page']['characters'][right['normal_start']]
            size = following['flow']['size']
            if (left['protected'] or right['protected'] or _ENDS.search(left['text'].rstrip())
                    or right['index'] in following['heading_ids'] or left['index'] in previous['heading_ids']
                    or not _body_boundary(left_edge, left, previous['flow'])
                    or not _body_boundary(right_edge, right, following['flow'])
                    or abs(right['bbox'][0]-following['flow']['left']) > size*.35
                    or left['bbox'][2] < previous['flow']['right']-previous['flow']['size']*1.25
                    or not previous['begin'] <= left['normal_end']-1 < previous['end']
                    or not following['begin'] <= right['normal_start'] < following['end']):
                continue
            # Source prefixes/suffixes must contain only whitespace or the exact
            # page number independently authorized above; no header is skipped.
            left_end = previous['positions'][left['normal_end']-previous['begin']-1]+1
            right_start = following['positions'][right['normal_start']-following['begin']]
            def residual(mapping, start, end):
                value = mapping['unit']['body'][start:end]
                for a, b in sorted(mapping['removed_ranges'], reverse=True):
                    if start <= a <= b <= end:
                        value = value[:a-start]+value[b-start:]
                return value.strip()
            if residual(previous, left_end, len(previous_unit['body'])) or residual(following, 0, right_start):
                continue
            result['continuations'].append(dict(from_unit_id=previous_unit['unit_id'], to_unit_id=next_unit['unit_id'],
                from_end=len(previous_unit['body']), to_start=0,
                separator=_join_space(left['normalized'][-1], right['normalized'][0]),
                reason='consecutive_pdf_page_continuation', proof=dict(
                    pages=[previous['page']['page'], following['page']['page']],
                    from_content_end=left_end, to_content_start=right_start,
                    bboxes=[left['bbox'], right['bbox']], next_line_indented=False)))
        result['edits'].sort(key=lambda edit:(edit['unit_id'], edit['start'], edit['end']))
        unavailable = sum(unit['status'] == 'unavailable' for unit in result['units'])
        result['receipt'].update(status='unavailable' if not matched else 'partial' if unavailable else 'complete',
                                 unavailable_units=unavailable, continuations=len(result['continuations']),
                                 headings=len(result['headings']))
        return result
    except Exception as exc:
        # A failed PDF read cannot leave partially trusted edits behind.
        return dict(edits=[], headings=[], continuations=[],
                    units=[_unit_summary(u, _page_number(u), 'unavailable') for u in requested],
                    receipt=dict(protocol=PROTOCOL, status='unavailable', reason='pdf_layout_read_failed',
                                 error_type=type(exc).__name__))
