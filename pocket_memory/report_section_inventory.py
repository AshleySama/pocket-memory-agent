# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Selectable sections from proved source headings, with exact original spans.

Reuses the existing structural reader. A section owns its children and page
continuations; source snapshots are never rewritten. Unsupported structures
return None to the existing conservative path.
"""
from __future__ import annotations

import copy
import re

from . import report_adaptive as a
from . import report_compiled as c
from . import report_structured as structure

PROTOCOL = 'explicit-section-selection-v1'


def _single_stream(units, register):
    planned = structure.plan(units)
    if planned is None:
        return None
    by_unit = {u['unit_id']: u for u in units}
    blocks, groups, omissions = [], [], copy.deepcopy(planned['omissions'])
    preamble = None
    for section in planned['chapters']:
        # A bare document heading is not substantive introductory evidence.
        # Any prose in the introduction remains a compulsory dependency.
        if section['title'] == '引言':
            text = '\n'.join(s['text'] for s in section['segments'])
            if all(not line.strip() or re.fullmatch(r' {0,3}#{1,6}\s+.+', line)
                   for line in text.splitlines()):
                omissions.extend(dict(s, reason='document_title') for s in section['segments'])
                continue
        ids = []
        for span in section['segments']:
            if not span['text'].strip():
                continue
            unit = by_unit[span['unit_id']]
            if unit['body'][span['start']:span['end']] != span['text']:
                raise ValueError('章节原文与引用范围不一致')
            sid = f'S{len(blocks)+1}'
            blocks.append(dict(id=sid, unit_id=unit['unit_id'], start=span['start'], end=span['end'],
                text=span['text'], title=unit['title'], tab=unit['tab'], field=unit['field'],
                kind='section', required_ids=[]))
            ids.append(sid)
        if not ids:
            continue
        group = dict(id=f'G{len(groups)+1}', title=section['title'], source_ids=ids,
            unit_ids=list(dict.fromkeys(s['unit_id'] for s in section['segments'])),
            frame_ids=[], kinds=['explicit_section'], roles=['source_section'],
            chars=sum(len(b['text']) for b in blocks if b['id'] in ids),
            conservative=False, indivisible=True, semantic_verified=False, source_sections=True,
            required_group_ids=[preamble] if preamble else [])
        groups.append(group)
        if section['title'] == '引言':
            preamble = group['id']
    if not groups:
        return None
    facts = a.direct_source_facts(blocks, units, register)
    for group in groups:
        group['fact_ids'] = [f['id'] for f in facts if set(f['source_ids']) <= set(group['source_ids'])]
        if not group['fact_ids']:
            raise ValueError('章节缺少完整原文事实')
    contract = dict(c.source_contract(units), section_protocol=PROTOCOL,
                    section_sha256=c._hash(dict(blocks=blocks, groups=groups, omissions=omissions)))
    return dict(blocks=blocks, facts=facts, groups=groups, frames=[], omissions=omissions,
        contract=contract, source_cache=dict(key=c._hash(contract), hit=False, invalidated=None),
        coverage=planned['coverage'])


def build(units, register, *, fallback_groups=None):
    """Compose independent inventories, never join unrelated source boundaries.

    Document chunks may continue onto the next page. Ordinary note tabs are
    independent; OCR cannot establish heading boundaries for saved正文. Keep
    the original single-stream contract (and its cache identity) unchanged.
    """
    if not units:
        return None
    if len({u['unit_id'] for u in units}) != len(units):
        raise ValueError('原文单元身份重复')
    streams = {}
    for unit in units:
        key = (('document', unit['source_id']) if unit['kind'] == 'document'
               else ('unit', unit['unit_id']))
        streams.setdefault(key, []).append(unit)
    if len(streams) == 1:
        return _single_stream(units, register)
    parts = [(stream, _single_stream(stream, lambda *_: 0)) for stream in streams.values()]
    # No proved headings: retain the original conservative whole-batch path.
    if not any(part is not None for _, part in parts):
        return None
    if fallback_groups is None:
        from .report_brief import atomic_groups as fallback_groups
    from . import report_source_frames as sf
    by_unit = {u['unit_id']: u for u in units}
    blocks, facts, groups, frames, omissions, receipts = [], [], [], [], [], []
    for stream, part in parts:
        sectioned = part is not None
        if not sectioned:
            bb, ff, _, _ = c.load_inventory(stream, lambda *_: 0)
            fr, om = sf.source_frames(stream)
            part = dict(blocks=bb, facts=ff, frames=fr, omissions=om,
                        groups=fallback_groups(stream, bb, ff, fr))
        part = copy.deepcopy(part)
        block_ids = {b['id']: f'S{len(blocks)+i}' for i, b in enumerate(part['blocks'], 1)}
        fact_ids = {f['id']: f'F{len(facts)+i}' for i, f in enumerate(part['facts'], 1)}
        group_ids = {g['id']: f'G{len(groups)+i}' for i, g in enumerate(part['groups'], 1)}
        frame_ids = {f['id']: f'R{len(frames)+i}' for i, f in enumerate(part['frames'], 1)}
        for block in part['blocks']:
            block['id'] = block_ids[block['id']]
            block['required_ids'] = [block_ids[sid] for sid in block.get('required_ids', [])]
        for fact in part['facts']:
            fact['id'] = fact_ids[fact['id']]
            fact['source_ids'] = [block_ids[sid] for sid in fact['source_ids']]
            for span in fact['evidence']:
                span['source_id'] = block_ids[span['source_id']]
            fact['refs'] = list(dict.fromkeys(register(by_unit[e['unit_id']], e['start'], e['end'])
                                             for e in fact['evidence']))
        for frame in part['frames']:
            frame['id'] = frame_ids[frame['id']]
        for group in part['groups']:
            group['id'] = group_ids[group['id']]
            group['source_ids'] = [block_ids[sid] for sid in group['source_ids']]
            group['fact_ids'] = [fact_ids[fid] for fid in group['fact_ids']]
            group['frame_ids'] = [frame_ids[fid] for fid in group['frame_ids']]
            if 'required_group_ids' in group:
                group['required_group_ids'] = [group_ids[gid] for gid in group['required_group_ids']]
        blocks.extend(part['blocks']); facts.extend(part['facts']); groups.extend(part['groups'])
        frames.extend(part['frames']); omissions.extend(part['omissions'])
        receipts.append(dict(unit_ids=[u['unit_id'] for u in stream],
                             mode='explicit_sections' if sectioned else 'conservative',
                             group_ids=[g['id'] for g in part['groups']]))
    # Prove the entire source inventory remains accounted for before selection.
    # Fallback omission receipts can overlap its blocks; count original positions
    # once rather than claiming duplicate content as extra coverage.
    retained = {uid: set() for uid in by_unit}; omitted = {uid: set() for uid in by_unit}
    for spans, target in ((blocks, retained), (omissions, omitted)):
        for span in spans:
            u = by_unit[span['unit_id']]; start, end = span['start'], span['end']
            if not (0 <= start < end <= len(u['body']) and u['body'][start:end] == span['text']):
                raise ValueError('来源分区后的原文与引用范围不一致')
            target[u['unit_id']].update(i for i in range(start, end) if not u['body'][i].isspace())
    for uid, u in by_unit.items():
        if retained[uid] | omitted[uid] != {i for i, char in enumerate(u['body']) if not char.isspace()}:
            raise ValueError('来源分区遗漏原文，未继续选材')
    coverage = dict(protocol='independent-source-sections-v1', total_units=len(units),
                    total_chars=sum(len(u['body']) for u in units), complete=True,
                    total_nonspace_chars=sum(len(retained[uid] | omitted[uid]) for uid in by_unit),
                    retained_nonspace_chars=sum(map(len, retained.values())),
                    omitted_nonspace_chars=sum(len(omitted[uid] - retained[uid]) for uid in by_unit),
                    partitions=receipts, model_calls=0)
    coverage['covered_nonspace_chars'] = coverage['total_nonspace_chars']
    contract = dict(c.source_contract(units), section_protocol=coverage['protocol'],
                    section_sha256=c._hash(dict(blocks=blocks, groups=groups, frames=frames, omissions=omissions)))
    return dict(blocks=blocks, facts=facts, groups=groups, frames=frames, omissions=omissions,
                contract=contract, source_cache=dict(key=c._hash(contract), hit=False, invalidated=None),
                coverage=coverage)
