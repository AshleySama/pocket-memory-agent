# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Source-proven reading layout, separate from immutable evidence spans.

Whitespace is restored from the original file, not guessed from chunk text.
Only explicit, sequential Chinese section numbering supplies heading levels.
No model, topic selection, fact rewriting or source deduplication happens here.
"""
import re
from pathlib import Path

from .report_source_markup import format_outside_code

PROTOCOL = 'original-reading-layout-v1'


def _normalized(value):
    positions = [i for i, c in enumerate(value) if not c.isspace()]
    return ''.join(value[i] for i in positions), positions


def text_layout(original, units):
    """Recover only whitespace proven by a unique whole-chunk source match."""
    source, spos = _normalized(original)
    out = dict(edits=[], headings=[], continuations=[], units=[])
    previous = None
    for unit in units:
        body = unit['body']; normalized, pos = _normalized(body)
        at = source.find(normalized) if normalized else -1
        if at < 0 or source.find(normalized, at + 1) >= 0:
            out['units'].append(dict(unit_id=unit['unit_id'], status='unavailable'))
            previous = None
            continue
        out['units'].append(dict(unit_id=unit['unit_id'], status='matched', revision=unit['revision']))
        for i in range(len(pos) - 1):
            a, b = pos[i]+1, pos[i+1]
            whitespace = original[spos[at+i]+1:spos[at+i+1]]
            if body[a:b] != whitespace:
                out['edits'].append(dict(unit_id=unit['unit_id'], start=a, end=b,
                    replacement=whitespace, reason='original_text_whitespace'))
        if previous and previous['source_end'] == at:
            gap = original[spos[at-1]+1:spos[at]]
            out['continuations'].append(dict(from_unit_id=previous['unit_id'],
                to_unit_id=unit['unit_id'], from_end=previous['end'], to_start=0,
                separator=gap, reason='adjacent_original_text'))
        previous = dict(unit_id=unit['unit_id'], source_end=at+len(normalized), end=len(body))
    return out


def collect(store, selected, units):
    result = dict(protocol=PROTOCOL, edits=[], headings=[], continuations=[], units=[], receipts=[])
    for selected_source in selected:
        if selected_source['kind'] != 'document':
            continue
        same = [u for u in units if u['kind']=='document' and u['source_id']==selected_source['id']]
        try:
            doc = store.get_document(selected_source['id'])
            path = (store.data_dir / doc.stored_path).resolve()
            path.relative_to(store.data_dir.resolve())
            if doc.file_type.lower() == 'pdf':
                from .report_pdf_reading_layout import build_layout
                layout = build_layout(path, same)
            elif doc.file_type.lower() in ('txt', 'md', 'markdown'):
                original = path.read_text(encoding='utf-8-sig')
                layout = text_layout(original, same)
            else:
                continue
            for key in ('edits','headings','continuations','units'):
                result[key].extend(layout.get(key, []))
            result['receipts'].append(dict(source_id=doc.id, type=doc.file_type,
                receipt=layout.get('receipt', {})))
        except (OSError, ValueError, LookupError, AttributeError) as exc:
            # Missing/changed originals must not invalidate saved evidence or
            # authorize heuristic removal of content. Keep the prior display.
            result['receipts'].append(dict(source_id=selected_source['id'], status='unavailable',
                reason=type(exc).__name__))
    return result


def attach_compiled_layout(report, store):
    """One presentation step for new and unedited saved scoped compilations."""
    adaptive = report.get('adaptive', {})
    if (adaptive.get('reading_mode') in ('source_brief_v2', 'source_compiled_v1')
            and adaptive.get('status') == 'complete' and adaptive.get('chapters')
            and not adaptive.get('reading_layout')
            and 'document_html' not in report):
        layout = collect(store, report['selected'], report['units'])
        if any(u.get('status')=='matched' for u in layout['units']):
            adaptive['reading_layout'] = layout
    return report


def selected_span(item, units, refs, layout):
    """Only exact, fully cited document excerpts can receive source layout."""
    u = units.get(item.get('unit_id'))
    if not u or u['kind'] != 'document':
        return None
    a, b = item.get('source_start'), item.get('source_end')
    if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(u['body'])
            or item['text'] != u['body'][a:b]):
        return None
    if not any(x['unit_id']==u['unit_id'] and x.get('revision')==u['revision']
               and x.get('status')=='matched' for x in layout.get('units', [])):
        return None
    covered = a
    evidence = [refs[n] for n in item['refs']]
    for ref in sorted(evidence, key=lambda r:r['start']):
        if (ref['unit_id'] != u['unit_id'] or ref['start'] < a or ref['end'] > b
                or u['body'][covered:ref['start']].strip()):
            return None
        covered = max(covered, ref['end'])
    if not evidence or u['body'][covered:b].strip():
        return None
    return dict(unit_id=u['unit_id'], start=a, end=b, text=item['text'])


def append_selected_span(segments, current, units, layout):
    """Join only consecutive selected text; never restore an excluded passage."""
    previous = segments[-1]
    u, v = units[previous['unit_id']], units[current['unit_id']]
    if (u['kind'],u['source_id'],u['field']) != (v['kind'],v['source_id'],v['field']):
        return False
    if previous['unit_id'] == current['unit_id']:
        if previous['end'] > current['start'] or u['body'][previous['end']:current['start']].strip():
            return False
        # Merge before projection so a line-break edit spanning the cut is
        # applied once. The restored gap can contain whitespace only.
        previous.update(end=current['end'], text=u['body'][previous['start']:current['end']])
        return True
    continuation = next((c for c in layout.get('continuations', [])
        if c['from_unit_id']==previous['unit_id'] and c['to_unit_id']==current['unit_id']
        and c['from_end']==previous['end'] and c['to_start']==current['start']
        and not c['separator'].strip()), None)
    if not continuation:
        return False
    segments.append(current)
    return True


def _number(value):
    digits = dict(zip('一二三四五六七八九', range(1,10)))
    if value in digits: return digits[value]
    if value == '十': return 10
    if value.count('十') == 1:
        a, b = value.split('十')
        if (not a or a in digits) and (not b or b in digits):
            return digits.get(a,1)*10 + digits.get(b,0)
    return None


def numbered_headings(value):
    def convert(text):
        lines = text.splitlines(keepends=True)
        groups = {3: [], 4: []}
        for i, line in enumerate(lines):
            # Headings must be standalone short labels, not numbered full
            # paragraphs, inline examples, dates, amounts, code or table cells.
            match = re.fullmatch(r'\s*([一二三四五六七八九十]+)、([^。；;：:！？!?\n]{2,40})\s*', line)
            level = 3
            if not match:
                match = re.fullmatch(r'\s*[（(]([一二三四五六七八九十]+)[）)]([^。；;：:！？!?\n]{2,40})[。]?\s*', line)
                level = 4
            if match and _number(match[1]) is not None:
                groups[level].append((i, _number(match[1])))
        marked = {}
        for level, items in groups.items():
            run = []
            for item in items + [(-1, -1)]:
                if run and item[1] != run[-1][1]+1:
                    if len(run) >= 2:
                        marked.update({index: level for index, _ in run})
                    run = []
                run.append(item)
        return ''.join('\n'+'#'*marked[i]+' '+line.strip()+'\n\n' if i in marked else line
                       for i, line in enumerate(lines))
    return format_outside_code(value, convert)


def project(segment, unit, layout):
    """Project a known span; evidence and its character offsets stay unchanged."""
    a, b = segment['start'], segment['end']; body = unit['body']
    matched = next((x for x in layout.get('units', [])
                    if x['unit_id']==unit['unit_id'] and x.get('revision')==unit['revision']), None)
    if not matched:
        return segment['text']
    changes = []
    for e in layout.get('edits', []):
        if e['unit_id'] != unit['unit_id'] or not a <= e['start'] <= e['end'] <= b:
            continue
        old = body[e['start']:e['end']]; new = e['replacement']
        # Never allow a display receipt to rewrite source words or numbers.
        if new.strip() or (old.strip() and e['reason'] != 'proven_pdf_page_number'):
            continue
        changes.append(e)
    text = segment['text']
    cursor = b
    for e in sorted(changes, key=lambda e:(e['start'],e['end']), reverse=True):
        if e['end'] > cursor:
            raise ValueError('原文显示范围重叠')
        text = text[:e['start']-a]+e['replacement']+text[e['end']-a:]
        cursor = e['start']
    return text


def chapter_text(chapter, units, layout):
    texts = []; previous = None
    for s in chapter['segments']:
        u = units[s['unit_id']]
        value = project(s, u, layout)
        separator = '\n\n'
        if previous:
            if previous['unit_id'] == s['unit_id'] and previous['end']==s['start']:
                separator = ''
            else:
                continuation = next((c for c in layout.get('continuations', [])
                    if c['from_unit_id']==previous['unit_id'] and c['to_unit_id']==s['unit_id']
                    and c['from_end']==previous['end'] and c['to_start']==s['start']), None)
                if continuation and not continuation['separator'].strip():
                    separator = continuation['separator']
            texts[-1] = texts[-1].rstrip() if separator != '\n\n' else texts[-1]
        texts.extend([separator if previous else '', value.lstrip() if previous and separator!='\n\n' else value])
        previous = s
    return numbered_headings(''.join(texts))
