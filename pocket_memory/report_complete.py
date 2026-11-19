# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Complete selected-source compilation without inferred filtering or an LLM.

Only the explicit content-range field may request selection. The report title
is a label, never an implicit exclusion rule. Keep original spans immutable;
reuse the established heading reader per independent stream when possible.
"""
from . import report_structured as structure

PROTOCOL = 'complete-selected-sources-v1'


def _restore_navigation(part, stream):
    """Standalone links can be requirements/materials, not disposable noise."""
    order = {u['unit_id']: i for i, u in enumerate(stream)}
    key = lambda s: (order[s['unit_id']], s['start'])
    owned = [(s, ch) for ch in part['chapters'] for s in ch['segments']]
    retained_omissions = []
    for omission in part['omissions']:
        if omission['reason'] != 'standalone_wiki_navigation':
            retained_omissions.append(omission)
            continue
        preceding = [(s, ch) for s, ch in owned if key(s) < key(omission)]
        owner = max(preceding, key=lambda pair: key(pair[0]))[1] if preceding else part['chapters'][0]
        owner['segments'].append({k: omission[k] for k in ('unit_id', 'start', 'end', 'text')})
    by_id = {u['unit_id']: u for u in stream}
    for chapter in part['chapters']:
        merged = []
        for span in sorted(chapter['segments'], key=key):
            structure._append_range(merged, by_id[span['unit_id']], span['start'], span['end'])
        chapter['segments'] = merged
    part['omissions'] = retained_omissions
    return part


def plan(units, focus=''):
    if not isinstance(focus, str) or focus.strip():
        return None
    if not units:
        raise ValueError('所选资料没有可编排的原文')
    identities = set()
    streams = {}
    for unit in units:
        uid = unit.get('unit_id')
        if (not isinstance(uid, str) or not uid or uid in identities
                or not isinstance(unit.get('body'), str)
                or unit.get('kind') not in ('document', 'note')
                or unit.get('source_id') is None):
            raise ValueError('所选资料原文身份无效或重复，未继续编排')
        identities.add(uid)
        # Documents may continue across pages. Note tabs and OCR channels are
        # independent even when their visible labels happen to be identical.
        key = ((unit['kind'], unit['source_id'], unit.get('field'))
               if unit['kind'] == 'document' else ('unit', uid))
        streams.setdefault(key, []).append(unit)

    chapters, omissions, receipts = [], [], []
    labelled = len(streams) > 1
    for stream in streams.values():
        part = structure.plan(stream)
        first = stream[0]
        label = first.get('title') or '资料'
        if first['kind'] == 'note':
            label += ' · ' + (first.get('tab') or '页签1')
        if first.get('field') == 'ocr_text':
            label += ' · 图片识别原文'
        if part is None:
            segments = [dict(unit_id=u['unit_id'], start=0, end=len(u['body']), text=u['body'])
                        for u in stream if u['body'].strip()]
            part_chapters = [dict(title=label, segments=segments)] if segments else []
            part_omissions = []
            mode = 'complete_source'
        else:
            part = _restore_navigation(part, stream)
            part_chapters, part_omissions = part['chapters'], part['omissions']
            mode = 'explicit_headings'
        for chapter in part_chapters:
            chapters.append(dict(chapter, source_label=label if labelled and chapter['title'] != label else ''))
        omissions.extend(part_omissions)
        receipts.append(dict(unit_ids=[u['unit_id'] for u in stream], mode=mode,
                             chapters=len(part_chapters)))
    if not chapters:
        raise ValueError('所选资料没有可编排的正文')
    # Existing span verifier proves every nonspace source character is owned
    # once, either by visible content or an explicit legacy metadata receipt.
    coverage = structure._coverage(units, chapters, omissions, None)
    coverage.update(protocol=PROTOCOL, partitions=receipts)
    return dict(chapters=chapters, omissions=omissions, coverage=coverage)
