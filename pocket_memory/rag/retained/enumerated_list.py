# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Project a complete, explicitly numbered label family from a saved note scope.

Labels come from the source (``label1: ...``, ``label2: ...``), never a domain
dictionary. Unsupported questions and uncertain scopes keep the existing route.
This is original-text projection, not semantic summarization or missingness.
"""
import copy
import hashlib
import re

from .field_projection import resolve_scope, strip_scopes
from .question_plan import DEPENDENCY, number, paragraphs
from .source_scope import natural_source_question, norm

VERSION = 'numbered-original-labels-v2-full-context'
MAX_CONTEXT_CHARS = 1200
NUM = r'(?:[0-9]+|[一二三四五六七八九十两]+)'
MARKER = re.compile(r'(?m)^[ \t]*(?:[-*][ \t]+)?(?P<label>[\u3400-\u9fff]{1,16}?)(?P<number>'+NUM+r')[：:][ \t]*')


def digest(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def scoped_question(question, notes):
    canonical, _ = natural_source_question(question, notes)
    scope = resolve_scope(canonical, notes)
    if (not scope.explicit or scope.unresolved or scope.strict or not scope.pairs
            or len(scope.pairs) > 8 or any(fields != {'content'} for fields in scope.pairs.values())):
        return None
    if sum(len(notes[i]['content']) for i in scope.pairs) > 24000:
        return None
    return canonical, scope


def asks_label(question, label, notes):
    """Consume the whole request; only source and enumeration scaffolding vary."""
    q = norm(strip_scopes(question, notes))
    # Source-introduction syntax, not predicates, conditions or hidden facets.
    pre = (r'(?:(?:请问|请|帮我)?(?:只根据|只依据|根据|依据|只看|只读|参考)?)'
           r'[，,:：]*(?:(?:项目|笔记|报告|资料|正文)?(?:里面|中|里|内)?'
           r'(?:提到|提及|记录|列出|描述)?的?)')
    item = re.escape(norm(label))
    forms = (r'(?:有|存在)?哪些'+item,
             item+r'(?:有|包括|包含)?(?:哪些|哪几项|哪几条|哪几个|是什么|有哪些|是什么内容)',
             r'(?:请)?(?:列出|列举)的?(?:全部|所有)?'+item)
    return bool(re.fullmatch(pre + r'(?:'+'|'.join(forms)+r')[？?。！!]*', q))


def original_span(body, start, end, note):
    return dict(field='content', source_id=note['id'], parent_note_id=note.get('parent_note_id'),
                tab_name=note.get('tab_name'), char_start=start, char_end_exclusive=end,
                quote=body[start:end], source_revision=digest(body))


def outside_records(body, records):
    """Preserve every nonempty complement of the record spans, not guessed roles."""
    cursor = 0
    for start, end in [(r['start'], r['end']) for r in records] + [(len(body), len(body))]:
        a, b = cursor, start
        while a < b and body[a].isspace(): a += 1
        while b > a and body[b-1].isspace(): b -= 1
        if a < b: yield a, b, body[a:b]
        cursor = end


def source_record(note, index, primary, raws):
    body = note['content']
    return dict(index=index, source_type='note', source_id=note['id'], source_key=f'note:{note["id"]}',
        title=note['title'], parent_note_id=note.get('parent_note_id'), tab_name=note.get('tab_name'),
        kind='content', source_field='content', source_revision=digest(body),
        raw_evidence=raws, evidence='\n'.join(v['quote'] for v in raws),
        coverage_complete=False, truncated=False, location='保存正文',
        display_evidence=primary['quote'], display_spans=[primary], display_evidence_count=1,
        display_evidence_kind='literal_sentence_focus', display_context='\n'.join(v['quote'] for v in raws))


def label_records(body, label, note):
    """Return complete records and a primary-value span for navigation only."""
    markers = [m for m in MARKER.finditer(body) if m['label'] == label]
    # A malformed member must not silently disappear from a complete list.
    all_starts = list(re.finditer(r'(?m)^[ \t]*(?:[-*][ \t]+)?'+re.escape(label)+NUM, body))
    if not markers or [m.start() for m in markers] != [m.start() for m in all_starts]:
        return None
    records = []
    for m in markers:
        end = body.find('\n\n', m.end())
        end = len(body) if end < 0 else end
        next_marker = next((n.start() for n in MARKER.finditer(body, m.end()) if n.start() < end), end)
        end = min(end, next_marker)
        while end > m.end() and body[end-1].isspace(): end -= 1
        start = m.start()
        while start < m.end() and body[start].isspace(): start += 1
        record = body[start:end]
        # An explicit adjacent label is a different field. Unlabelled sentences,
        # qualifications and negations remain part of this field's value.
        value_start = m.end()
        boundary = re.search(r'[。；;\n]\s*(?P<field>[\u3400-\u9fffA-Za-z_]{1,20})[：:]', body[value_start:end])
        display_end = value_start + boundary.start() + 1 if boundary else end
        while display_end > start and body[display_end-1].isspace(): display_end -= 1
        if not body[value_start:display_end].strip(' \t\r\n。；;'):
            return None
        records.append(dict(source_id=note['id'], ordinal=number(m['number']), label=label,
            start=start, end=end, display_start=start, display_end=display_end,
            display_quote=body[start:display_end], source_revision=digest(body),
            parent_note_id=note.get('parent_note_id'), tab_name=note.get('tab_name')))
    return records


def validate_list(question, plan, notes, ocr=None):
    """Consume a proposed plan and prove identities, ordering and original spans.

    It does not call the producer's record parser or certify arbitrary semantics.
    The request/scope grammar is shared; this remains a structural cross-check.
    """
    resolved = scoped_question(question, notes)
    if resolved is None: return ['unresolved_scope']
    canonical, scope = resolved
    if any((ocr or {}).get(sid, '').strip() for sid in scope.pairs): return ['ocr_scope_unproved']
    label = plan.get('label', '')
    if not label or not asks_label(canonical, label, notes): return ['unparsed_request']
    if sorted(scope.pairs) != plan.get('scope_note_ids'): return ['scope_mismatch']
    records = plan.get('records', [])
    if not 2 <= len(records) <= 16: return ['not_bounded_list']
    expected = []
    for sid in sorted(scope.pairs):
        body = notes[sid]['content']
        if digest(body) != plan.get('scope_revisions', {}).get(str(sid)): return ['stale_scope']
        for m in re.finditer(r'(?m)^[ \t]*(?:[-*][ \t]+)?'+re.escape(label)+r'('+NUM+r')[：:]', body):
            a = m.start()
            while a < m.end() and body[a].isspace(): a += 1
            expected.append((sid, a, number(m[1])))
    if [(r['source_id'], r['start'], r['ordinal']) for r in records] != expected:
        return ['missing_or_reordered_member']
    if [r['ordinal'] for r in records] != list(range(1, len(records)+1)):
        return ['noncontiguous_or_conflicting_members']
    for r in records:
        sid = r['source_id']; note = notes.get(sid)
        if not note or not scope.permits(sid, 'content'): return ['source_not_in_scope']
        body = note['content']
        a, b, c, d = r['start'], r['end'], r['display_start'], r['display_end']
        if not (0 <= a == c < d <= b <= len(body)): return ['invalid_span']
        if r['display_quote'] != body[c:d] or r['source_revision'] != digest(body): return ['original_mismatch']
        if r['parent_note_id'] != note.get('parent_note_id') or r['tab_name'] != note.get('tab_name'): return ['member_identity_mismatch']
        if not re.match(re.escape(label)+NUM+r'[：:]', body[a:b]): return ['wrong_label']
        header = re.match(re.escape(label)+NUM+r'[：:][ \t]*', body[a:])
        stop = body.find('\n\n', a+header.end())
        stop = len(body) if stop < 0 else stop
        next_header = next((m.start() for m in MARKER.finditer(body,a+header.end()) if m.start()<stop),stop)
        stop = min(stop,next_header)
        while stop > a+header.end() and body[stop-1].isspace(): stop -= 1
        if b != stop: return ['whole_record_truncated']
        # The displayed primary field cannot omit text before the next explicit
        # label. Tail-field interpretation stays in the original paragraph.
        colon = re.search(r'[：:]', body[a:b]); tail = body[a+colon.end():b]
        next_field = re.search(r'[。；;\n]\s*[\u3400-\u9fffA-Za-z_]{1,20}[：:]', tail)
        required_end = a+colon.end()+next_field.start()+1 if next_field else b
        if d != required_end: return ['primary_field_truncated']
    expected_dependencies = []
    for sid in sorted(scope.pairs):
        body = notes[sid]['content']
        # Independently cover every original character, then inspect the gaps.
        # Do not call the producer's complement or record parsing functions.
        coverage = bytearray(len(body))
        for r in records:
            if r['source_id'] == sid:
                coverage[r['start']:r['end']] = b'\x01' * (r['end']-r['start'])
        for gap in re.finditer(b'\x00+', bytes(coverage)):
            a,b = gap.span()
            while a < b and body[a].isspace(): a += 1
            while b > a and body[b-1].isspace(): b -= 1
            if a == b: continue
            text = body[a:b]
            for _,_,paragraph in paragraphs(text):
                if label in paragraph and not DEPENDENCY.match(paragraph):
                    return ['unparsed_label_context']
            expected_dependencies.append((sid,a,b,text))
    if sum(b-a for _,a,b,_ in expected_dependencies) > MAX_CONTEXT_CHARS:
        return ['external_context_over_budget']
    if [(r['source_id'],r['char_start'],r['char_end_exclusive'],r['quote']) for r in plan.get('dependencies', [])] != expected_dependencies:
        return ['missing_or_changed_dependency']
    for r in plan.get('dependencies', []):
        sid = r['source_id']; note = notes.get(sid, {}); body = note.get('content', '')
        if not scope.permits(sid, 'content') or body[r['char_start']:r['char_end_exclusive']] != r['quote']:
            return ['dependency_mismatch']
        if (r.get('field') != 'content' or r.get('source_revision') != digest(body)
                or r.get('parent_note_id') != note.get('parent_note_id')
                or r.get('tab_name') != note.get('tab_name')):
            return ['dependency_identity_mismatch']
    return []


class EnumeratedList:
    def __init__(self, notes, ocr=None):
        self.notes = notes
        self.ocr = ocr or {}
        self.audit = {}

    def prepare(self, question):
        self.audit = dict(version=VERSION, active=False, reason='scope_or_request_not_supported')
        resolved = scoped_question(question, self.notes)
        if resolved is None: return None
        canonical, scope = resolved
        if any(self.ocr.get(sid, '').strip() for sid in scope.pairs):
            # This parser proves saved prose records only. OCR may contain
            # additional members or conditions and cannot be silently ignored.
            self.audit['reason'] = 'ocr_scope_requires_existing_route'; return None
        labels = {m['label'] for sid in scope.pairs for m in MARKER.finditer(self.notes[sid]['content'])}
        requested = [label for label in labels if asks_label(canonical, label, self.notes)]
        if len(requested) != 1: return None
        label = requested[0]; records = []; dependencies = []
        for sid in sorted(scope.pairs):
            note = self.notes[sid]; body = note['content']
            if re.search(r'<(?:table|tr|p|div)\b|\|[^\n]+\|', body, re.I):
                self.audit['reason'] = 'non_plain_original'; return None
            if label in body:
                local = label_records(body, label, note)
                if local is None:
                    self.audit['reason'] = 'unparsed_member_in_scope'; return None
                records.extend(local)
            else: local = []
            # A qualifier can use arbitrary wording. Preserve ALL remaining
            # saved prose, including a tab containing context but no member.
            for a,b,text in outside_records(body, local):
                for _,_,paragraph in paragraphs(text):
                    if label in paragraph and not DEPENDENCY.match(paragraph):
                        self.audit['reason'] = 'unparsed_label_context'; return None
                dependencies.append(original_span(body,a,b,note))
        if sum(d['char_end_exclusive']-d['char_start'] for d in dependencies) > MAX_CONTEXT_CHARS:
            self.audit['reason'] = 'external_context_over_budget'; return None
        if [r['ordinal'] for r in records] != list(range(1, len(records)+1)) or not 2 <= len(records) <= 16:
            self.audit['reason'] = 'incomplete_or_conflicting_sequence'; return None
        plan = dict(label=label, records=records, dependencies=dependencies,
            scope_note_ids=sorted(scope.pairs),
            scope_revisions={str(sid):digest(self.notes[sid]['content']) for sid in scope.pairs})
        errors = validate_list(question, plan, self.notes, self.ocr)
        if errors:
            self.audit.update(reason='projection_guard_failed', errors=errors); return None
        sources = []; lines = []
        for r in records:
            note = self.notes[r['source_id']]; body = note['content']; i = len(sources)+1
            primary = original_span(body,r['display_start'],r['display_end'],note)
            whole = original_span(body,r['start'],r['end'],note)
            # Each reference owns exactly the record it supports. External
            # context receives a separate reference and navigation target below.
            source = source_record(note, i, primary, [whole])
            # Do not classify ancillary labels as semantically independent:
            # a condition may follow any colon label. The COMPLETE record is
            # the answer; primary spans are only a navigation aid.
            sources.append(source); lines.append(f'{whole["quote"]} [{i}]')
        # Context is visibly separated from the requested list, never omitted.
        if dependencies: lines.append('原文说明：')
        for dependency in dependencies:
            note = self.notes[dependency['source_id']]
            source = source_record(note, len(sources)+1, copy.deepcopy(dependency),
                                   [copy.deepcopy(dependency)])
            sources.append(source)
            lines.append(dependency['quote']+f' [{source["index"]}]')
        self.audit = dict(version=VERSION, active=True, plan=plan,
            proof='complete saved scope + contiguous data-labelled records + all original record and external prose',
            guard='source and structural verification, not arbitrary semantic entailment')
        response = dict(answer='\n\n'.join(lines), answer_status='answered', sources=sources,
            provided_sources=copy.deepcopy(sources), citation_validation=dict(passed=not errors, errors=errors),
            trace=dict(enumerated_list=copy.deepcopy(self.audit), generation_calls=0, messages=[],
                       rendering='verbatim-numbered-original-fields'))
        return dict(mode='single_free_text', sources=sources, direct_record_response=response)
