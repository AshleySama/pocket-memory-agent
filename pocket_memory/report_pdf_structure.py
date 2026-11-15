# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Recover PDF heading events without changing any saved source text.

Font hierarchy proposes headings; only a unique whitespace-insensitive match
to the saved text on the same physical page authorizes an event. This is a
layout annotation, not evidence of factual or semantic correctness.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re


PROTOCOL = 'pdf-font-structure-v2'


def _normalized(text):
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    return ''.join(text[i] for i in positions), positions


def _page_number(unit):
    match = re.search(r'第\s*(\d+)\s*页', str(unit.get('tab', '')))
    return int(match.group(1)) if match else None


def _occurrences(text, needle):
    start = 0
    while needle:
        at = text.find(needle, start)
        if at < 0:
            return
        yield at
        start = at + 1


def _attach_watermark_ranges(pages, by_page, body_size, repetitions):
    """Map proven page decoration through the *whole* saved chunk identity.

    A short text match alone cannot remove content. The complete normalized
    chunk must occur in its original PDF page, and every possible occurrence
    must identify the removed characters as oversized/repeated decoration.
    This also covers a watermark split across multiple imported chunks.
    """
    mapped = 0
    unmapped = []
    for page_no, lines in pages:
        page_text = ''.join(line['normalized'] for line in lines)
        proof_at = []
        for line_no, line in enumerate(lines, 1):
            repeated_pages = sorted(repetitions[line['normalized']])
            # A large watermark sharing a PDF text line with small body spans
            # does not authorize dropping the body spans as decoration.
            min_size = min((size for size, count in line['sizes'].items() if count), default=0)
            oversized = min_size >= body_size*2.8
            proven = ((oversized and len(repeated_pages) >= 2)
                      or (line['rotated'] and min_size >= body_size*1.7
                          and len(repeated_pages) >= 3))
            proof = None
            if proven:
                proof = dict(pdf_line=line_no, pdf_text=line['text'],
                             font_size=line['size'], rotated=line['rotated'],
                             repeated_pages=repeated_pages, bbox=line['bbox'])
            proof_at.extend([proof]*len(line['normalized']))
        if not any(proof_at):
            continue
        for unit, normalized, positions in by_page.get(page_no, []):
            if not normalized:
                continue
            matches = list(_occurrences(page_text, normalized))
            if not matches:
                unmapped.append(dict(unit_id=unit['unit_id'], page=page_no,
                                     reason='whole_saved_chunk_not_in_pdf_page'))
                continue
            run_start = None
            run_proofs = {}

            def close_run(stop):
                nonlocal run_start, mapped, run_proofs
                if run_start is None:
                    return
                a, b = positions[run_start], positions[stop-1]+1
                unit['structural_omissions'].append(dict(
                    unit_id=unit['unit_id'], start=a, end=b,
                    text=unit['body'][a:b], revision=unit['revision'],
                    reason='proven_pdf_watermark', page=page_no,
                    proof=dict(method='same_page_full_chunk_font_mapping',
                               body_font_size=body_size,
                               matching_chunk_occurrences=len(matches),
                               pdf_lines=list(run_proofs.values()))))
                mapped += 1
                run_start = None
                run_proofs = {}

            for offset in range(len(normalized)):
                proofs = [proof_at[at+offset] for at in matches]
                if all(proof is not None for proof in proofs):
                    if run_start is None:
                        run_start = offset
                    for proof in proofs:
                        run_proofs[proof['pdf_line']] = proof
                else:
                    close_run(offset)
            close_run(len(normalized))
    return mapped, unmapped


def enrich_pdf_units(path, units):
    """Annotate supplied units in place; return an inspectable layout receipt.

    Missing/unreadable PDFs produce an ``unavailable`` receipt, no headings,
    and an explicit marker disabling textual topic guesses. Bodies, revisions,
    source identities and stored documents are never altered.
    """
    from .report_source_frames import _role_label

    receipt = dict(protocol=PROTOCOL, status='unavailable', pages=0,
                   body_font_size=None, heading_min_size=None,
                   candidates=0, matched_headings=0, unmatched=[],
                   watermark_ranges=0, watermark_unmapped=[])
    by_page = defaultdict(list)
    for unit in units:
        page = _page_number(unit)
        unit['structural_headings'] = []
        unit['structural_omissions'] = []
        unit['pdf_structure'] = dict(protocol=PROTOCOL, status='unavailable', page=page)
        if page is not None:
            normalized, positions = _normalized(unit['body'])
            by_page[page].append((unit, normalized, positions))
    if not Path(path).is_file():
        receipt['reason'] = 'original_pdf_missing'
        return receipt
    try:
        import pymupdf
        pages = []
        font_weights = Counter()
        repetitions = defaultdict(set)
        with pymupdf.open(path) as document:
            for page_no, page in enumerate(document, 1):
                lines = []
                for block in page.get_text('dict').get('blocks', []):
                    for line in block.get('lines', []):
                        spans = line.get('spans', [])
                        text = ''.join(str(span.get('text', '')) for span in spans)
                        normalized, _ = _normalized(text)
                        if not normalized:
                            continue
                        direction = line.get('dir', (1, 0))
                        rotated = abs(float(direction[1])) > .05 or float(direction[0]) < .95
                        sizes = Counter()
                        for span in spans:
                            count = len(_normalized(str(span.get('text', '')))[0])
                            size = round(float(span.get('size', 0))*2)/2
                            sizes[size] += count
                            if not rotated and size > 0:
                                font_weights[size] += count
                        size = max(sizes, key=sizes.get) if sizes else 0
                        repetitions[normalized].add(page_no)
                        lines.append(dict(text=text, normalized=normalized, size=size,
                                          sizes=sizes, rotated=rotated,
                                          bbox=list(line.get('bbox', (0, 0, 0, 0)))))
                pages.append((page_no, lines))
        if not font_weights:
            receipt['reason'] = 'no_pdf_text_font_hierarchy'
            return receipt
        body_size = max(font_weights, key=font_weights.get)
        threshold = body_size*1.7
        receipt.update(status='complete', pages=len(pages), body_font_size=body_size,
                       heading_min_size=threshold)
        for unit in units:
            unit['pdf_structure'].update(status='complete', body_font_size=body_size,
                                         heading_min_size=threshold)
        receipt['watermark_ranges'], receipt['watermark_unmapped'] = _attach_watermark_ranges(
            pages, by_page, body_size, repetitions)

        def heading_line(line):
            text = line['normalized']
            if line['rotated'] or not 2 <= len(text) <= 160 or _role_label(text):
                return False
            if line['size'] < threshold:
                return False
            # Oversized repeated page decoration is not a chapter title.
            if line['size'] >= body_size*2.8 and len(repetitions[text]) >= 2:
                return False
            large_chars = sum(n for size, n in line['sizes'].items() if size >= threshold)
            return large_chars >= len(text)*.75

        for page_no, lines in pages:
            groups = []
            for index, line in enumerate(lines):
                if not heading_line(line):
                    continue
                if groups:
                    previous = groups[-1][-1]
                    gap = line['bbox'][1]-previous['bbox'][3]
                    aligned = abs(line['bbox'][0]-previous['bbox'][0]) <= max(line['size'], previous['size'])*1.5
                    if (previous['index']+1 == index and -.25*line['size'] <= gap <= line['size']*1.5
                            and abs(line['size']-previous['size']) <= body_size*.2 and aligned
                            and sum(len(x['normalized']) for x in groups[-1])+len(line['normalized']) <= 160):
                        groups[-1].append(dict(line, index=index))
                        continue
                groups.append([dict(line, index=index)])
            for group_no, group in enumerate(groups, 1):
                needle = ''.join(line['normalized'] for line in group)
                if not 4 <= len(needle) <= 160:
                    continue
                receipt['candidates'] += 1
                matches = []
                for unit, normalized, positions in by_page.get(page_no, []):
                    for at in _occurrences(normalized, needle):
                        a, b = positions[at], positions[at+len(needle)-1]+1
                        # A font heading must match complete saved text lines.
                        # A quotation of the same title inside narrative prose
                        # is a different occurrence, not an ambiguous heading.
                        before=unit['body'][unit['body'].rfind('\n',0,a)+1:a]
                        line_end=unit['body'].find('\n',b)
                        after=unit['body'][b:line_end if line_end>=0 else len(unit['body'])]
                        if not before.strip() and not after.strip():
                            matches.append((unit, a, b))
                if len(matches) != 1:
                    receipt['unmatched'].append(dict(page=page_no, text=needle,
                        reason='ambiguous_saved_text' if matches else 'not_in_saved_page', matches=len(matches)))
                    continue
                unit, a, b = matches[0]
                event = dict(id=f'pdf-heading-{page_no}-{group_no}', start=a, end=b,
                             text=unit['body'][a:b], revision=unit['revision'],
                             page=page_no, font_size=max(line['size'] for line in group),
                             bbox=[min(line['bbox'][0] for line in group), min(line['bbox'][1] for line in group),
                                   max(line['bbox'][2] for line in group), max(line['bbox'][3] for line in group)],
                             method='pdf_font_hierarchy')
                unit['structural_headings'].append(event)
                receipt['matched_headings'] += 1
        for unit in units:
            unit['structural_headings'].sort(key=lambda event: event['start'])
        return receipt
    except Exception as exc:
        # Keep the saved source usable, but never replace missing structure by
        # guessed question sentences. No partial events survive a failed read.
        for unit in units:
            unit['structural_headings'] = []
            unit['structural_omissions'] = []
            unit['pdf_structure']['status'] = 'unavailable'
        receipt.update(status='unavailable', matched_headings=0, watermark_ranges=0,
                       reason='pdf_structure_read_failed', error_type=type(exc).__name__)
        return receipt
