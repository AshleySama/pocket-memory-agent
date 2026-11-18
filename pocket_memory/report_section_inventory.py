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


def build(units, register):
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
