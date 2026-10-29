# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Isolated, zero-generation citation analysis; no corpus or gold access.

Text overlap is a discovery hint, never entailment. Proposal generation and
application are separate; fragment additions, deletions and footer stripping
are deliberately not executable actions. Original code/records stay immutable.
"""
import copy
import difflib
import re
import unicodedata
from pathlib import Path

from . import citation_reconcile as exact


def normalized_statement(text):
    """Typography normalization must not erase operators, units or ratios."""
    chars, positions = [], []
    excluded = {i for m in exact.CITE.finditer(text) for i in range(m.start(), m.end())}
    for i, ch in enumerate(text):
        if i in excluded:
            continue
        for c in unicodedata.normalize('NFKC', ch):
            numeric_separator = (c in '.:∶' and i > 0 and i+1 < len(text)
                                 and text[i-1].isdigit() and text[i+1].isdigit())
            if c.isalnum() or c in '%/+-=_*^|&~' or unicodedata.category(c).startswith('S') or numeric_separator:
                chars.append(c)
                positions.append(i)
    return ''.join(chars), positions


# This is a private module instance, loaded from the immutable prior file;
# its helpers resolve this stricter normalizer. No shared module, v4 prepare,
# model input or source object is changed.
exact.normalized = normalized_statement
VERSION = 'clause-attribution-v2'
CITE = exact.CITE
# Adjacent markers, even when line-wrapped, form one citation group.
REFS = re.compile(r'\[\d+\](?:\s*[、,，]?\s*\[\d+\])*')
FOOTER = re.compile(r'(?:支持资料编号|支持来源|引用来源|来源编号)\s*[:：]')
LIST = re.compile(r'(?m)^\s*(?:[-*•]\s+|\d+[.)、]\s+)')
NUMBER = re.compile(r'(?<!\w)[+-]?\d+(?:\.\d+)?(?:%|％|万元|亿元|元|年|月|日)?')


def units(text):
    """Offset-preserving citation units, not a claim of semantic atomization.

    Soft line wrapping is ignored. List starts, blank paragraphs, unquoted
    sentence punctuation and citation groups are boundaries. A citation after
    punctuation stays attached to its preceding unit, not to the next sentence.
    Commas/colons/decimals do not split conditions or numerical expressions.
    """
    fm = FOOTER.search(text)
    end = fm.start() if fm else len(text)
    starts = {m.start() for m in LIST.finditer(text[:end])}
    starts.update(m.end() for m in re.finditer(r'\n\s*\n', text[:end]))
    groups = {m.start(): m for m in REFS.finditer(text[:end])}
    result = []
    start = 0
    quote = []
    pairs = {'“': '”', '‘': '’', '「': '」', '『': '』', '（': '）', '(': ')'}

    def push(stop):
        nonlocal start
        if stop <= start:
            return
        segment = text[start:stop]
        if CITE.sub('', segment).strip():
            refs = list(REFS.finditer(segment))
            ambiguous = any(text[start+m.end():].lstrip()[:1] not in ('', '。', '！', '？', '；', ';')
                            and segment[:m.start()].rstrip().endswith(('。', '！', '？', '；', ';'))
                            for m in refs)
            result.append(dict(start=start, end=stop, text=segment,
                               content=exact.body(segment),
                               citation_scope_ambiguous=ambiguous,
                               refs=[dict(start=start+m.start(), end=start+m.end(),
                                          indices=[int(x) for x in re.findall(r'\d+', m[0])]) for m in refs]))
        start = stop

    i = 0
    while i < end:
        if i in starts and i > start:
            push(i)
        ch = text[i]
        if ch in pairs:
            quote.append(pairs[ch])
        elif quote and ch == quote[-1]:
            quote.pop()
        if i in groups and not quote:
            m = groups[i]
            stop = m.end()
            # Prefix/interleaved references do not define a complete claim.
            # Example: 根据[1]介绍，甲[2]与乙[3]共同完成。
            next_char = text[stop:stop+1]
            if next_char and next_char not in '。！？；;\r\n':
                i = stop
                continue
            while stop < end and text[stop] in '。！？；;':
                stop += 1
            push(stop)
            i = stop
            continue
        if ch in '。！？；;' and not quote:
            stop = i + 1
            peek = stop
            while peek < end and text[peek].isspace():
                peek += 1
            if peek in groups:
                stop = groups[peek].end()
                while stop < end and text[stop] in '。！？；;':
                    stop += 1
            push(stop)
            i = stop
            continue
        i += 1
    push(end)
    footer = None
    if fm:
        footer = dict(start=fm.start(), text=text[fm.start():],
                      indices=[int(x) for x in re.findall(r'\[(\d+)\]', text[fm.start():])])
    return result, footer


def norm(text):
    return exact.normalized(text)[0]


def lexical_hints(unit, sources):
    """Report candidate fragments/zero overlap without inferring support."""
    needle = norm(unit['content'])
    current = {i for g in unit['refs'] for i in g['indices']}
    source_text = {s['index']: norm(s.get('evidence', '')) for s in sources}
    fragments = []
    for source in sources:
        if source['index'] in current:
            continue
        for span in source.get('raw_evidence', []):
            quote = span.get('quote', '')
            hay, positions = exact.normalized(quote)
            if not hay or not needle:
                continue
            match = difflib.SequenceMatcher(None, needle, hay, autojunk=False).find_longest_match()
            fragment = needle[match.a:match.a+match.size]
            # Numbers cannot be a trigger. Threshold is only a diagnostic.
            if match.size < 8 or re.search(r'\d', fragment):
                continue
            owners = [idx for idx, value in source_text.items() if fragment in value]
            if owners != [source['index']]:
                continue
            a = positions[match.b]
            b = positions[match.b+match.size-1] + 1
            fragments.append(dict(provided_index=source['index'], fragment=quote[a:b],
                                  field=span.get('field'), char_start=span.get('char_start', 0)+a,
                                  char_end_exclusive=span.get('char_start', 0)+b,
                                  decision='review_only', reason='substring_does_not_prove_subject_predicate_or_condition'))
    grams = {needle[i:i+3] for i in range(max(0, len(needle)-2))}
    numbers = set(NUMBER.findall(unit['content']))
    zero = []
    for source in sources:
        if source['index'] not in current:
            continue
        hay = source_text[source['index']]
        if not any(g in hay for g in grams) and not numbers.intersection(NUMBER.findall(source.get('evidence', ''))):
            zero.append(dict(provided_index=source['index'], decision='review_only',
                             reason='lexical_disjointness_does_not_prove_semantic_irrelevance'))
    return fragments, zero


def diagnose(response):
    raw = response.get('trace', {}).get('answer_before_citation_guard', '')
    sources = response.get('provided_sources', [])
    pieces, footer = units(raw)
    available = {s['index'] for s in sources}
    invalid = sorted({int(i) for i in re.findall(r'\[(\d+)\]', raw)} - available)
    rows = []
    for unit in pieces:
        proofs = exact.certificates(unit['text'], sources)
        fragments, zero = lexical_hints(unit, sources)
        current = {i for g in unit['refs'] for i in g['indices']}
        choices = {p['provided_index'] for p in proofs}
        action = None
        # Complete exact statement only. Never recover a hidden answer via a
        # supported fragment; inherited whole-answer recovery is separate.
        terminal_ref = not unit['refs'] or (len(unit['refs']) == 1 and not raw[unit['refs'][0]['end']:unit['end']].strip('。！？；; \t\r\n'))
        if not invalid and terminal_ref and not unit['citation_scope_ambiguous'] and response.get('trace', {}).get('finish_reason', 'stop') == 'stop' and len(choices) == 1 and not choices.intersection(current):
            action = dict(kind='exact_add',
                          provided_index=next(iter(choices)), proof=proofs,
                          decision='dry_run_only')
        rows.append(dict(**unit, literal_certificate=proofs,
                         support='literal_alignment_only' if proofs else 'unknown',
                         fragments=fragments, zero_overlap=zero, proposal=action))
    # No automatic scope change: a footer may be the only evidence for an
    # earlier paragraph even when another sentence has an inline reference.
    return dict(version=VERSION, ids_resolve=not invalid, invalid_indices=invalid,
                claim_support=rows, coverage='not_evaluated',
                display=dict(status=response.get('answer_status'), action='unchanged'),
                footer=dict(**footer, decision='preserve_scope') if footer else None,
                semantic_verified=False, new_generations=0)


def reconcile(response, apply_guard, *, apply_exact_units=False):
    """Keep preserved fixes; new unit edits require explicit offline acceptance."""
    original = copy.deepcopy(response)
    base = exact.reconcile(original, apply_guard)
    analysis = diagnose(base)
    if not apply_exact_units:
        return base, analysis
    raw = base.get('trace', {}).get('answer_before_citation_guard', '')
    edits = []
    actions = []
    for unit in analysis['claim_support']:
        proposal = unit['proposal']
        if not proposal:
            continue
        value = '['+str(proposal['provided_index'])+']'
        if unit['refs']:
            # A citation at paragraph end may support preceding sentences.
            # Add the exact source rather than deleting prior provenance.
            at = unit['refs'][-1]['end']
        else:
            at = unit['end']
            while at > unit['start'] and raw[at-1].isspace():
                at -= 1
            if at > unit['start'] and raw[at-1] in '。！？；;':
                at -= 1
        edits.append((at, value))
        actions.append({**proposal, 'start': unit['start'], 'end': unit['end']})
    if edits:
        edited = raw
        for at, value in sorted(edits, reverse=True):
            edited = edited[:at]+value+edited[at:]
        assert CITE.sub('', edited) == CITE.sub('', raw)
        pending = copy.deepcopy(base)
        pending['answer'] = edited
        for key in ('citation_validation', 'answer_status', 'retrieved_evidence'):
            pending.pop(key, None)
        pending['sources'] = []
        base = apply_guard(pending, repair_callback=None)
        base['trace']['clause_actions'] = actions
    return base, analysis
