# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Presentation-only focus inside an already cited, verified original span.

Never selects model evidence, changes an answer/citation, or claims entailment.
Broad context stays on the payload. Uncertain prose has context, not a guessed
highlight; structured records and complete-range proofs remain indivisible.
"""
import difflib
import re
from collections import Counter

from .pipeline import RagPipeline


def normalized(text):
    return ''.join(re.findall(r'[\w\u3400-\u9fff]', text)).casefold()


def literal_focus(claim, quote):
    a, b = normalized(claim), normalized(quote)
    if len(a) < 6:
        return False
    if a in b:
        return True
    # Allow a small word-order paraphrase only with all answer characters,
    # a substantial contiguous anchor, and matching numbers/identifiers.
    # This is a reading aid, not a semantic-support validator.
    if re.search(r'没有|不是|并非|未|不|仅|除|比较|相同|不同|高于|低于', claim):
        return False
    if Counter(a) - Counter(b):
        return False
    markers = re.findall(r'[A-Za-z_][A-Za-z_\d.]*|\d+(?:\.\d+)?', claim)
    if any(marker.casefold() not in b for marker in markers):
        return False
    anchor = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match().size
    pairs = {a[i:i+2] for i in range(len(a)-1)}
    return anchor >= 6 and sum(pair in b for pair in pairs) >= len(pairs) * .5


def presentation(source, answer, *, direct=False):
    """Return additional display fields; leave the supplied source untouched."""
    if (direct or source.get('kind') != 'content' or source.get('coverage_complete')
            or source.get('table_headers') or source.get('structured_rows')
            or re.search(r'<(?:table|tr)\b|\|[^\n]+\|', source.get('evidence', ''), re.I)):
        return {}  # preserve table headers, rows, OCR structure and range proofs
    base = dict(display_evidence='', display_spans=[], display_evidence_count=0,
                display_context=source.get('evidence', ''), display_evidence_kind='context_only')
    claims = [c['text'] for c in RagPipeline.answer_claims(answer)
              if source.get('index') in c['citations']]
    if not claims:
        return base
    candidates = []
    for member in source.get('citation_members', []):
        if member.get('verification') != 'exact_saved_original':
            continue
        for span in member.get('spans', []):
            if span.get('field') != 'content':
                continue  # never guess OCR row/sentence boundaries
            quote = span['quote']
            for match in re.finditer(r'[^。！？!?\n]+[。！？!?]?', quote):
                text = match.group().strip()
                if not text:
                    continue
                start = span['char_start'] + match.start() + len(match.group()) - len(match.group().lstrip())
                candidates.append(dict(field='content', char_start=start,
                    char_end_exclusive=start+len(text), quote=text,
                    source_revision=span['source_revision']))
    focused = []
    for claim in claims:
        matches = [s for s in candidates if literal_focus(claim, s['quote'])]
        if not matches:
            return base  # one unresolved claim must not disappear from the card
        focused.extend(matches)
    unique = {(s['field'], s['char_start'], s['char_end_exclusive']): s for s in focused}
    spans = [unique[key] for key in sorted(unique)]
    base.update(display_evidence='\n'.join(s['quote'] for s in spans), display_spans=spans,
                display_evidence_count=len(spans), display_evidence_kind='literal_sentence_focus')
    return base
