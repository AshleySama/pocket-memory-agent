# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Exact saved headings -> complete original sections, with no generation.

This is bounded source projection, not summarization, semantic field inference,
or a proof of factual correctness. Unknown requests keep the existing route.
"""
import copy
import re

from .enumerated_list import (MAX_CONTEXT_CHARS, digest, original_span,
                              outside_records, scoped_question, source_record)
from .field_projection import strip_scopes
from .source_scope import norm

VERSION = 'original-heading-sections-v1'
MAX_SECTION_CHARS = 2500
MAX_HEADINGS = 16
COLON = re.compile(r'([^\r\n：:]+)[：:][ \t]*')
MARKDOWN = re.compile(r'(#{1,6})[ \t]+([^\r\n]+?)[ \t]*')
CONNECTORS = ('以及', '和', '与', '及', '、')


def valid_label(label):
    label = label.strip()
    return (0 < len(label) <= 40 and bool(re.search(r'[A-Za-z\u3400-\u9fff]', label))
            and not re.search(r'[#：:。！？!?；;|<>\[\]《》]', label)
            and not re.match(r'[-*>]|\d+[.)、]', label))


def plain_body(body):
    return not re.search(r'<[!/A-Za-z]|\|[^\n]+\||```|~~~|(?m:^[ \t]*>)', body)


def exact_scope(question, notes, ocr):
    resolved = scoped_question(question, notes)
    if resolved is None: return None
    canonical, scope = resolved
    if any((ocr or {}).get(sid, '').strip() for sid in scope.pairs): return None
    # An explicit document family is one object; do not merge separate objects
    # just because their section labels look compatible.
    families = {notes[sid].get('parent_note_id') or sid for sid in scope.pairs}
    if len(families) != 1: return None
    return canonical, scope


def section_rows(note):
    """Producer scans physical lines; heading boundaries are never guessed."""
    body = note['content']
    if not plain_body(body): return None
    heads = []; offset = 0
    for line in body.splitlines(keepends=True):
        raw = line.rstrip('\r\n'); stripped = raw.strip()
        a = offset; offset += len(line)
        if not stripped: continue
        md = MARKDOWN.fullmatch(raw)
        colon = COLON.fullmatch(raw)
        if md:
            label = md[2].strip(); style = ('markdown', len(md[1]))
        elif colon:
            label = colon[1].strip(); style = ('colon', 0)
        elif stripped.startswith('#') or stripped.endswith(('：', ':')):
            return None
        else: continue
        if raw != raw.lstrip() or not valid_label(label): return None
        heads.append(dict(label=label, start=a, heading_end=a+len(raw), style=style))
    if not heads: return []
    if len(heads) > MAX_HEADINGS or len({h['style'] for h in heads}) != 1: return None
    rows = []
    for i, head in enumerate(heads):
        end = heads[i+1]['start'] if i+1 < len(heads) else len(body)
        while end > head['heading_end'] and body[end-1].isspace(): end -= 1
        body_start = head['heading_end']
        while body_start < end and body[body_start].isspace(): body_start += 1
        if body_start >= end or end-head['start'] > MAX_SECTION_CHARS: return None
        rows.append(dict(source_id=note['id'], field='content', label=head['label'],
            start=head['start'], end=end, heading_end=head['heading_end'], body_start=body_start,
            quote=body[head['start']:end], source_revision=digest(body),
            parent_note_id=note.get('parent_note_id'), tab_name=note.get('tab_name')))
    return rows


def request_labels(question, labels, notes):
    """Consume every asked field; reject ambiguous connector/label partitions."""
    names = {norm(label): label for label in labels}
    if len(names) != len(labels): return None
    q = norm(strip_scopes(question, notes)).rstrip('？?。！!')
    prefix = r'(?:(?:请问|请|帮我)?(?:只根据|只依据|根据|依据|参考|只看|只读)?[，,:：]*的?)'
    forms = [r'(?P<fields>.+?)(?:分别)?(?:是什么|有哪些|是谁|是哪些|有什么内容)',
             r'(?:有哪些|有什么)(?P<fields>.+)',
             r'(?:列出|列举)的?(?:全部|所有)?(?P<fields>.+)']
    candidates = set()

    def parse_fields(text, selected=()):
        if len(selected) >= 8: return
        for key in names:
            if not text.startswith(key) or key in selected: continue
            rest = text[len(key):]; found = selected + (key,)
            if not rest:
                candidates.add(found)
            else:
                for connector in CONNECTORS:
                    if rest.startswith(connector): parse_fields(rest[len(connector):], found)

    for form in forms:
        match = re.fullmatch(prefix + form + r'[？?。！!]*', q)
        if match: parse_fields(match['fields'])
    if len(candidates) != 1: return None
    return [names[key] for key in next(iter(candidates))]


def _guard_rows(note):
    """Consumer independently scans line intervals and reconstructs boundaries."""
    body = note['content']
    if not plain_body(body): return None
    heads = []
    for match in re.finditer(r'[^\r\n]+', body):
        line = match[0]; stripped = line.strip()
        if not stripped: continue
        md = re.fullmatch(r'(#{1,6})[ \t]+([^\r\n]+?)[ \t]*', line)
        colon = re.fullmatch(r'([^\r\n：:]+)[：:][ \t]*', line)
        if md:
            label, style = md[2].strip(), ('markdown', len(md[1]))
        elif colon:
            label, style = colon[1].strip(), ('colon', 0)
        elif stripped.startswith('#') or stripped.endswith(('：', ':')):
            return None
        else: continue
        if line != line.lstrip() or not valid_label(label): return None
        heads.append((match.start(), match.end(), label, style))
    if not heads: return []
    if len(heads) > MAX_HEADINGS or len({h[3] for h in heads}) != 1: return None
    result = []
    for index, (a, heading_end, label, _) in enumerate(heads):
        stop = heads[index+1][0] if index+1 < len(heads) else len(body)
        section_text = body[a:stop].rstrip()
        end = a + len(section_text)
        content_start = heading_end
        while content_start < end and body[content_start].isspace(): content_start += 1
        if content_start >= end or len(section_text) > MAX_SECTION_CHARS: return None
        result.append(dict(source_id=note['id'], field='content', label=label,
            start=a, end=end, heading_end=heading_end, body_start=content_start,
            quote=section_text, source_revision=digest(body),
            parent_note_id=note.get('parent_note_id'), tab_name=note.get('tab_name')))
    return result


def validate_sections(question, plan, notes, ocr=None, response=None):
    """Reconstruct scope, fields, complete text coverage and output identities.

    Scope/request grammar is shared with the producer. This is an independent
    consumer of the proposed spans, not independent semantic entailment.
    """
    if not isinstance(plan, dict): return ['invalid_plan']
    resolved = exact_scope(question, notes, ocr)
    if resolved is None: return ['unresolved_scope']
    canonical, scope = resolved
    if plan.get('scope_note_ids') != sorted(scope.pairs): return ['scope_mismatch']
    revisions = {str(sid): digest(notes[sid]['content']) for sid in scope.pairs}
    if plan.get('scope_revisions') != revisions: return ['stale_scope']
    available = []
    for sid in sorted(scope.pairs):
        local = _guard_rows(notes[sid])
        if local is None: return ['unproved_section_format']
        available.extend(local)
    if not available or len(available) > MAX_HEADINGS: return ['unbounded_sections']
    labels = request_labels(canonical, [r['label'] for r in available], notes)
    if not labels or plan.get('labels') != labels: return ['request_not_fully_bound']
    expected = [next(r for r in available if r['label'] == label) for label in labels]
    if plan.get('sections') != expected: return ['section_identity_or_boundary_mismatch']
    contexts = []
    for sid in sorted(scope.pairs):
        body = notes[sid]['content']; covered = bytearray(len(body))
        for row in expected:
            if row['source_id'] == sid:
                covered[row['start']:row['end']] = b'\x01' * (row['end']-row['start'])
        for match in re.finditer(b'\x00+', bytes(covered)):
            a, b = match.span()
            while a < b and body[a].isspace(): a += 1
            while b > a and body[b-1].isspace(): b -= 1
            if a < b: contexts.append(original_span(body, a, b, notes[sid]))
    if sum(len(c['quote']) for c in contexts) > MAX_CONTEXT_CHARS: return ['context_over_budget']
    if plan.get('context') != contexts: return ['missing_or_changed_original_context']
    if response is not None:
        originals = [original_span(notes[r['source_id']]['content'], r['start'], r['end'], notes[r['source_id']])
                     for r in expected]
        pieces = []; sources = []
        for i, raw in enumerate(originals + contexts, 1):
            if i == len(originals)+1: pieces.append('原文说明：')
            pieces.append(raw['quote'] + f' [{i}]')
            sources.append(source_record(notes[raw['source_id']], i, raw, [raw]))
        if response.get('answer') != '\n\n'.join(pieces): return ['rendered_answer_not_original']
        if response.get('sources') != sources or response.get('provided_sources') != sources:
            return ['rendered_source_identity_mismatch']
    return []


class SectionProjection:
    def __init__(self, notes, ocr=None):
        self.notes = notes; self.ocr = ocr or {}; self.audit = {}

    def prepare(self, question):
        self.audit = dict(version=VERSION, active=False, reason='scope_or_request_not_supported')
        resolved = exact_scope(question, self.notes, self.ocr)
        if resolved is None: return None
        canonical, scope = resolved
        available = []
        for sid in sorted(scope.pairs):
            rows = section_rows(self.notes[sid])
            if rows is None:
                self.audit['reason'] = 'unproved_section_format'; return None
            available.extend(rows)
        if not available or len(available) > MAX_HEADINGS: return None
        labels = request_labels(canonical, [r['label'] for r in available], self.notes)
        if not labels: return None
        selected = [next(r for r in available if r['label'] == label) for label in labels]
        context = []
        for sid in sorted(scope.pairs):
            note = self.notes[sid]
            local = sorted((r for r in selected if r['source_id']==sid), key=lambda r: r['start'])
            for a,b,_ in outside_records(note['content'], local):
                context.append(original_span(note['content'], a, b, note))
        if sum(len(c['quote']) for c in context) > MAX_CONTEXT_CHARS:
            self.audit['reason'] = 'context_over_budget'; return None
        plan = dict(labels=labels, sections=selected, context=context,
            scope_note_ids=sorted(scope.pairs),
            scope_revisions={str(sid):digest(self.notes[sid]['content']) for sid in scope.pairs})
        sources = []; lines = []
        originals = [original_span(self.notes[r['source_id']]['content'], r['start'], r['end'], self.notes[r['source_id']])
                     for r in selected]
        for i, raw in enumerate(originals + context, 1):
            if i == len(originals)+1: lines.append('原文说明：')
            sources.append(source_record(self.notes[raw['source_id']], i, raw, [copy.deepcopy(raw)]))
            lines.append(raw['quote'] + f' [{i}]')
        response = dict(answer='\n\n'.join(lines), answer_status='answered', sources=sources,
            provided_sources=copy.deepcopy(sources),
            trace=dict(section_projection=dict(version=VERSION, active=True, plan=copy.deepcopy(plan)),
                       generation_calls=0, messages=[], rendering='complete-original-sections'))
        errors = validate_sections(question, plan, self.notes, self.ocr, response)
        if errors:
            self.audit.update(reason='projection_guard_failed', errors=errors); return None
        response['citation_validation'] = dict(passed=not errors, errors=errors)
        self.audit = dict(version=VERSION, active=True, plan=copy.deepcopy(plan),
                          guard='original identity and full-coverage verification; not semantic inference')
        response['trace']['section_projection'] = copy.deepcopy(self.audit)
        return dict(mode='single_free_text', sources=sources, direct_record_response=response)
