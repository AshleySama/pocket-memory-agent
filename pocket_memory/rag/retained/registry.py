# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Request-local citation identity, separate from answer semantic validation.

Inspired by WeKnora CitationSources (MIT): expanded context is not a new
original source. This is an independent Python implementation, not a port.
Never selects evidence, rewrites answers, changes prompts or guesses offsets.
"""
import copy
import hashlib
import json
import re

VERSION = 'original-source-registry-v1'
ADDED = {'citation_source_id', 'citation_members', 'citation_provenance'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def identity(source):
    # Display numbering and retrieval scores are deliberately not identities.
    return digest({k: source.get(k) for k in (
        'source_type', 'source_id', 'kind', 'node_id', 'chunk_id', 'evidence')})


def actual_sources(prepared):
    if prepared['mode'] == 'single_free_text':
        return prepared['sources']
    ordinary = prepared.get('ordinary_prepared')
    if ordinary is not None:
        return ordinary[0]
    baseline = prepared['baseline_preparation']
    if baseline['route'] == 'exact_table_projection':
        return baseline['response'].get('provided_sources', [])
    return baseline.get('sources', [])


def unchanged_source(source):
    return {k: v for k, v in source.items() if k not in ADDED}


class EvidenceRegistry:
    def __init__(self, notes, ocr):
        self.notes, self.ocr = notes, ocr
        self.prepared = None
        self.preview_audit = None

    def body(self, source, field):
        if source.get('source_type', 'note') != 'note':
            return None
        sid = source.get('source_id')
        if field == 'content':
            return self.notes.get(sid, {}).get('content')
        if field == 'ocr_text':
            return self.ocr.get(sid)
        return None

    def members(self, source):
        sid = source.get('source_id')
        note = self.notes.get(sid, {}) if source.get('source_type', 'note') == 'note' else {}
        common = dict(source_type=source.get('source_type', 'note'), source_id=sid,
                      parent_note_id=note.get('parent_note_id'), tab_name=note.get('tab_name'),
                      title=source.get('title'), kind=source.get('kind'))
        spans, unresolved = [], []
        for raw in source.get('raw_evidence', []):
            field = raw.get('field')
            body = self.body(source, field)
            if body is None:
                unresolved.append(dict(reason='original_field_unavailable', field=field))
                continue
            a, b, quote = raw.get('char_start'), raw.get('char_end_exclusive'), raw.get('quote')
            if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < b <= len(body)
                    and isinstance(quote, str) and body[a:b] == quote):
                # Do not turn a bad historical coordinate into verified provenance.
                unresolved.append(dict(reason='original_span_mismatch', field=field,
                                       char_start=a, char_end_exclusive=b))
                continue
            span = dict(field=field, char_start=a, char_end_exclusive=b, quote=quote,
                        source_revision=hashlib.sha256(body.encode()).hexdigest())
            spans.append(span)
        atomic = (source.get('kind') in ('table_row', 'schema_view')
                  or bool(source.get('table_headers') or source.get('structured_rows')))
        # Column headers / row / OCR corroboration remain one indivisible source.
        groups = [spans] if atomic and spans else [[s] for s in spans]
        result = []
        for group in groups:
            member = dict(common, spans=group,
                          verification='partial_original_spans' if atomic and unresolved else 'exact_saved_original',
                          role=('partial_structured_evidence' if unresolved else 'atomic_structured_evidence') if atomic else 'member_of_cited_context',
                          independent_claim_support='not_inferred')
            member['id'] = 'original:' + digest(member)[:24]
            result.append(member)
        if not result or unresolved:
            # Keep the existing source usable; do not falsely claim coordinates
            # and do not reject a correct answer because provenance is incomplete.
            member = dict(common, chunk_id=source.get('chunk_id'), node_id=source.get('node_id'),
                          text=source.get('evidence', ''), verification='supplied_fragment_only',
                          role='unresolved_original_location', independent_claim_support='not_inferred')
            member['id'] = 'fragment:' + digest(member)[:24]
            result.append(member)
        return result, unresolved

    def decorate(self, source):
        out = copy.deepcopy(source)
        members, unresolved = self.members(source)
        out.update(citation_source_id='context:' + identity(source)[:24], citation_members=members,
                   citation_provenance=dict(version=VERSION, issues=unresolved,
                       scope='all members of this supplied block; not a claim-to-member inference',
                       semantic_support='not_validated', coverage_complete=source.get('coverage_complete', False)))
        return out

    def stage(self, prepared):
        self.prepared = copy.deepcopy(actual_sources(prepared))
        old = prepared.get('sources') or prepared.get('baseline_preparation', {}).get('sources', [])
        self.preview_audit = dict(
            corrected=[identity(s) for s in old] != [identity(s) for s in self.prepared],
            old_count=len(old), actual_count=len(self.prepared),
            ordinary_prepared_used=prepared.get('ordinary_prepared') is not None)
        return [self.decorate(s) for s in self.prepared]

    def finish(self, response):
        result = copy.deepcopy(response)
        provided = response.get('provided_sources', [])
        ids = [s['index'] for s in provided]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate supplied citation label')
        mapping = {}
        for source in provided:
            mapping.setdefault(identity(source), []).append(source['index'])
        final_map = []
        for source in response.get('sources', []):
            original = mapping.get(identity(source))
            if not original:
                raise ValueError('final source is not one of the actual supplied sources')
            final_map.append(dict(display_index=source['index'], supplied_indices=original,
                                  citation_source_id='context:' + identity(source)[:24]))
        # For normal generation trace.messages is the exact message sent; for
        # deterministic table projections / structured plans it is absent.
        messages = response.get('trace', {}).get('messages')
        trace = response.get('trace', {})
        structured_plan = bool(trace.get('response_format') and trace.get('rendering'))
        prompt_errors = []
        prompt_unverified = []
        if messages and not structured_plan:
            users = [m['content'] for m in messages if m['role'] == 'user']
            # Exact retained prompt envelope only. All numbered headers bound
            # blocks, including unregistered ones; the question is never evidence.
            supported = len(users) == 1 and users[0].startswith('参考资料：\n') and '\n\n问题：' in users[0]
            context = users[0][len('参考资料：\n'):].split('\n\n问题：', 1)[0] if supported else ''
            markers = list(re.finditer(r'(?m)^\[(\d+)\] ([^\n]* · [^\n]*)\n', context))
            blocks = {}
            for i, marker in enumerate(markers):
                end = markers[i+1].start() if i+1 < len(markers) else len(context)
                blocks.setdefault(int(marker.group(1)), []).append((marker.group(0), context[marker.end():end]))
            for source in provided:
                matches = blocks.get(source['index'], [])
                if not supported or len(matches) != 1:
                    prompt_unverified.append(source['index'])
                    continue
                header, body = matches[0]
                if source.get('prompt_block'):
                    valid = (header + body).startswith(source['prompt_block'])
                else:
                    valid = header.startswith(f'[{source["index"]}] {source["title"]} · ') and source['evidence'] in body
                if not valid:
                    prompt_errors.append(source['index'])
        # This parser is not an answer guard. Ambiguous literal formatting stays
        # unverified; it must not turn an old correct response into an HTTP error.
        result['provided_sources'] = [self.decorate(s) for s in provided]
        result['sources'] = [self.decorate(s) for s in response.get('sources', [])]
        issues = [dict(index=s['index'], **e) for s in result['provided_sources'] for e in s['citation_provenance']['issues']]
        result.setdefault('trace', {})['evidence_registry'] = dict(
            version=VERSION, preview=self.preview_audit, final_to_supplied=final_map,
            supplied_to_identity={str(s['index']): s['citation_source_id'] for s in result['provided_sources']},
            original_location_issues=issues, original_location_verified_members=sum(
                m['verification'] == 'exact_saved_original' for s in result['provided_sources'] for m in s['citation_members']),
            unresolved_members=sum(m['verification'] != 'exact_saved_original' for s in result['provided_sources'] for m in s['citation_members']),
            prompt_checked=bool(messages) and not structured_plan and not prompt_errors and not prompt_unverified,
            prompt_mismatch_indices=prompt_errors, prompt_unverified_indices=prompt_unverified,
            deterministic_or_structured_path=not bool(messages) or structured_plan,
            answer_changed=False, citation_selection_changed=False, semantic_support='not_validated')
        return result
