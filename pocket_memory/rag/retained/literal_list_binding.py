# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Recover mislabeled complete copied lists, using only supplied originals.

Not a semantic scorer: an ambiguous, paraphrased, shortened, reordered or
unsupported list stays on the existing path. Recovery renders the entire
original section, including its conditions, rather than silently certifying
only the model's selection of facts.
"""
import copy
import hashlib
import re
import unicodedata

from .citation_guard import CITE, apply_guard, indices

VERSION = 'complete-original-list-binding-v1'
HEADING = re.compile(r'(?m)^(#{1,6})[ \t]+([^\r\n]+)')
ITEM = re.compile(r'^\s*(\d{1,2})[.)、][ \t]+(.+)$')
INLINE = re.compile(r'</?(?:strong|em|b|i)>', re.I)
INTRO = re.compile(r'^(?P<label>.+?)(?:的)?(?:做法|步骤|流程|'
                   r'制作教程|操作步骤|操作流程)(?:如下)?[：:]$')
REQUEST = re.compile(r'(?:的)?(?:怎么做|如何制作|制作教程|制作步骤|做法|'
                     r'有哪些步骤|的步骤是什么|操作步骤|操作流程|流程是什么)[?？。!！]*$')


def layout(text):
    """Only known presentation syntax; keep polarity, units and punctuation."""
    text = INLINE.sub('', text)
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def lines(text, answer=False):
    result = []
    for raw in text.splitlines():
        value = raw.strip()
        if value.startswith('> '):
            value = value[2:]  # Markdown block quote, before list parsing.
        if answer:
            value = CITE.sub('', value).strip()
        if value:
            result.append(value)
    return result


def list_group(text):
    """Exactly one ordered group, starting at 1, with whole physical members."""
    values = lines(text)
    found = [(i, ITEM.fullmatch(value)) for i, value in enumerate(values)]
    found = [(i, m) for i, m in found if m]
    if not 2 <= len(found) <= 24:
        return None
    if [int(m[1]) for _, m in found] != list(range(1, len(found)+1)):
        return None
    start, end = found[0][0], found[-1][0]
    if end-start+1 != len(found):
        return None
    return dict(items=[layout(m[2]) for _, m in found], before=values[:start],
                after=values[end+1:])


def original_sections(body):
    """Keep siblings separate; no inferred heading or flattened cross-tab list."""
    heads = list(HEADING.finditer(body))
    if not heads or body[:heads[0].start()].strip():
        return []  # Unknown document-wide preconditions must not disappear.
    result = []
    for i, head in enumerate(heads):
        label = re.sub(r'^\d+[.)、]\s*', '', head[2]).strip()
        end = next((m.start() for m in heads[i+1:] if len(m[1]) <= len(head[1])), len(body))
        # Only empty ancestor introductions are supported. Otherwise there may
        # be an inherited exception that does not appear in this section.
        parents = []
        for previous in heads[:i]:
            while parents and len(parents[-1][1]) >= len(previous[1]):
                parents.pop()
            parents.append(previous)
        parents = [p for p in parents if len(p[1]) < len(head[1])]
        # Ancestor TITLES may themselves state applicability (e.g. test only).
        # This bounded recovery does not silently strip those constraints.
        if parents:
            continue
        result.append(dict(label=label, start=head.start(), body_start=head.end(), end=end,
                           heading=head[0], text=body[head.end():end].strip()))
    return result


def supplied_original(source, notes):
    if source.get('source_type', 'note') != 'note' or source.get('truncated'):
        return None
    note = notes.get(source.get('source_id'))
    if not note:
        return None
    body = note.get('content', '')
    spans = source.get('raw_evidence', [])
    if not spans or any(s.get('field') != 'content' for s in spans):
        return None
    covered = bytearray(len(body))
    for span in spans:
        a, b = span.get('char_start'), span.get('char_end_exclusive')
        if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < b <= len(body)
                and span.get('quote') == body[a:b] and span['quote'] in source.get('evidence', '')):
            return None
        covered[a:b] = b'\x01' * (b-a)
    return body, covered


def prove(question, response, notes):
    trace = response.get('trace', {})
    raw = trace.get('answer_before_citation_guard', '')
    if (trace.get('finish_reason', 'stop') != 'stop' or len(CITE.findall(raw)) != 1
            or len(indices(raw)) != 1):
        return []
    cite = next(CITE.finditer(raw), None)
    if cite is None or raw[cite.end():].strip('。 \r\n'):
        return []
    values = lines(raw, answer=True)
    if not values:
        return []
    intro = INTRO.fullmatch(values[0])
    if not intro:
        return []
    label = intro['label']
    q = question.strip()
    if not q.startswith(label) or not REQUEST.fullmatch(q[len(label):]):
        return []
    generated = list_group('\n'.join(values[1:]))
    if generated is None:
        return []
    choices = []
    for source in response.get('provided_sources', []):
        original = supplied_original(source, notes)
        if original is None:
            continue
        body, covered = original
        sections = [s for s in original_sections(body)
                    if s['label'] == label or re.fullmatch(re.escape(label)+r'[（(][^()（）]+[)）]', s['label'])]
        if len(sections) != 1:
            continue
        section = sections[0]
        if CITE.search(section['text']):
            continue  # Original footnotes are not model citation labels.
        a, b = section['start'], section['end']
        if any(not covered[i] and not body[i].isspace() for i in range(a, b)):
            continue
        group = list_group(section['text'])
        if group is None or group['items'] != generated['items']:
            continue
        # Every extra model line must be a complete line in this same section,
        # never a fragment assembled from separate subjects or other sources.
        originals = {layout(line) for line in lines(section['text'])}
        if any(layout(line) not in originals for line in generated['before'] + generated['after']):
            continue
        choices.append(dict(source_index=source['index'], source_id=source['source_id'],
                            field='content', char_start=a, char_end_exclusive=b,
                            source_revision=hashlib.sha256(body.encode()).hexdigest(),
                            section=section, member_count=len(group['items'])))
    return choices


def reconcile(question, response, notes):
    choices = prove(question, response, notes)
    if len(choices) != 1:
        return response
    proof = choices[0]
    raw = response['trace']['answer_before_citation_guard']
    # Correctly assigned responses remain byte-for-byte unchanged.
    if indices(raw) == [proof['source_index']]:
        return response
    result = copy.deepcopy(response)
    section = proof['section']
    # Recovered answer includes the COMPLETE same-section source: ingredients,
    # applicability, exceptions and tail notes, not just matching list items.
    # Bind each complete original paragraph/item, rather than placing a single
    # citation after the last line (which leaves the other steps unlocated).
    rendered = []
    for line in section['text'].splitlines():
        rendered.append(line + f" [{proof['source_index']}]"
                        if line.strip() and not HEADING.fullmatch(line) else line)
    result['answer'] = section['label'] + '\n\n' + '\n'.join(rendered)
    result['sources'] = []
    result = apply_guard(result, repair_callback=None)
    result['trace']['literal_list_binding'] = dict(version=VERSION,
        original_raw_answer=raw, old_indices=indices(raw), proof=proof,
        rendering='complete_supplied_original_section', new_generations=0,
        semantic_scope='original-section provenance, not general semantic or question coverage verification')
    return result
