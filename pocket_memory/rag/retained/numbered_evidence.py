# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Atomic original evidence for complete labelled lists."""
import hashlib
from .context_contract import Original

def complete_list_units(question, pool, notes):
    """Rank a requested numbered list as one original unit, not siblings.

    This does not choose a source or answer the question. All other source
    candidates remain eligible. Conditional/filtered questions fail the
    existing closed request contract and keep the prior selection path.
    """
    from .enumerated_list import MARKER, label_records, original_span
    from .request_contract import original_label_request
    result = dict(pool)
    def verified_note(unit):
        return (unit.get('span') and unit.get('source_type', 'note') == 'note'
                and unit.get('kind') == 'content'
                and unit.get('source_verification') == 'unique_original_span'
                and unit['source_id'] in notes)
    for sid in sorted({s['source_id'] for s in pool.values() if verified_note(s)}):
        body = notes[sid]['content']
        original = Original(body)
        starts = original.articles or [0]
        for left, right in zip(starts, starts[1:]+[len(body)]):
            local = body[left:right]
            labels = sorted({m['label'] for m in MARKER.finditer(local)})
            request = original_label_request(question, labels, notes)
            if not request or len(request['labels']) != 1: continue
            records = label_records(local, request['labels'][0], notes[sid])
            if not records or not 2 <= len(records) <= 16: continue
            if [r['ordinal'] for r in records] != list(range(1,len(records)+1)): continue
            lo, hi = left+records[0]['start'], left+records[-1]['end']
            if hi-lo > 1800: continue
            matches = [(k,u) for k,u in result.items() if verified_note(u) and u['source_id']==sid
                       and lo <= u['span'][0] < u['span'][1] <= hi]
            if not matches: continue
            parents = [(a,b) for a,b in original.parents(lo) if b<=lo]
            raw = [original.raw(a,b) for a,b in parents]+[original.raw(lo,hi)]
            content = '\n'.join(r['quote'] for r in raw)
            identity = (matches[0][1]['source_key'], lo, hi, 'complete_numbered_list')
            node_id = 'list:'+hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
            unit = dict(matches[0][1], id=node_id,node_id=node_id,span=[lo,hi],parents=parents,
                        text=content,evidence=content,raw_evidence=raw,
                        source_verification='unique_original_span',complete_sentence=True,
                        anchors=list(dict.fromkeys(a for _,u in matches for a in u['anchors'])),
                        numbered_members=len(records),
                        requested_list_spans=[original_span(body,left+r['display_start'],
                            left+r['display_end'],notes[sid]) for r in records])
            # Preserve the first retrieved member's position; promoting the
            # complete group must not move it behind unrelated candidates.
            keys = {key for key,_ in matches}
            assembled = {}; inserted = False
            for key,value in result.items():
                if key in keys:
                    if not inserted: assembled[identity] = unit; inserted = True
                else: assembled[key] = value
            result = assembled
    return result
