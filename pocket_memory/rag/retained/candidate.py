# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Accepted bound-facts entry point with explicit host-owned dependencies.

The wrapper preserves the original stage/ask binding, source-scope adapter,
registry finalization and deterministic direct-record branch. The caller owns
request serialization and must ensure its shared text engine is available.
Automatic completion retry remains disabled.
"""
from pocket_memory.rag.performance import timed, span, metric
from .clause_candidate import Candidate as RetainedCandidate
from .facts import BoundFacts
from .registry import EvidenceRegistry
from .source_scope import ScopedRetrieval
from .request_cache import retrieval_request
from .question_plan import QuestionPlan
from .field_projection import FieldProjection
from .enumerated_list import EnumeratedList
from .section_projection import SectionProjection


class Candidate(RetainedCandidate):
    @timed('snapshot.construct')
    def __init__(self, context):
        super().__init__(context)
        self._staged = None
        self.bound_prepared = None
        self._closed = False
        scope = ScopedRetrieval(self.stable, self.stable.scope)
        self.stable.scope = scope
        self.stable.legacy.scope = scope
        self.app_scope = scope
        self.evidence_registry = EvidenceRegistry(self.notes, self.bridge.ocr)
        self.bound_facts = BoundFacts(self.notes)
        self.question_plan = QuestionPlan(self.notes, self.bridge.ocr, self.app_scope)
        self.field_projection = FieldProjection(self.notes, self.bridge.ocr)
        self.enumerated_list = EnumeratedList(self.notes, self.bridge.ocr)
        self.section_projection = SectionProjection(self.notes, self.bridge.ocr)
        self.stable.legacy.coverage_ocr_originals = self.bridge.ocr

    @timed('evidence.direct_plan')
    def prepare_direct(self, question):
        self.bound_prepared = None
        self._staged = None
        from .field_projection import request_fields, resolve_scope, strip_scopes
        from .table_contract import find_entities
        from ..clarification import _missing_subject_fields, _NAME_HEADERS, _compact
        # Ambiguity belongs to the actual saved schema, not a leading noun in
        # the question. Apply the same proof in all-data and selected scopes.
        field_scope = resolve_scope(question, self.notes)
        ambiguous = []
        if not field_scope.unresolved and not field_scope.strict:
            for sid, fields in field_scope.pairs.items():
                if 'content' not in fields: continue
                for table in self.field_projection.tables[sid]:
                    header = table['headers'][table['key']]
                    bare_fields = (request_fields(question, table, [], self.notes) is not None or
                        ((_compact(header) in _NAME_HEADERS or _compact(header).endswith('名称')) and
                         bool(_missing_subject_fields(strip_scopes(question, self.notes), header,
                                                      dict.fromkeys(table['headers'])))))
                    if len(table['rows']) > 1 and bare_fields and not find_entities(question, table)[0]:
                        ambiguous.append(self.notes[sid]['title'])
            if ambiguous:
                names = '、'.join('《'+name+'》' for name in dict.fromkeys(ambiguous))
                response = dict(answer=f'资料{names}包含多个对象，请明确对象的完整名称后再查询这些字段。',
                                sources=[], provided_sources=[], answer_status='subject_unconfirmed',
                                trace={'generation_calls': 0, 'messages': []})
                direct = dict(mode='single_free_text', sources=[], direct_record_response=response)
                self.bound_prepared = (question, direct)
                return direct
        direct = self.field_projection.prepare(question)
        if direct is None:
            direct = self.bound_facts.prepare(question)
        if direct is None:
            direct = self.question_plan.prepare(question)
        if direct is None:
            direct = self.enumerated_list.prepare(question)
        if direct is None:
            direct = self.section_projection.prepare(question)
        if direct:
            self.bound_prepared = (question, direct)
        return direct

    @timed('evidence.stage')
    def stage(self, question):
        with retrieval_request():
            direct = self.prepare_direct(question)
            if direct:
                return direct
            self._staged = None
            prepared = super().prepare(question)
            self._staged = (question, prepared)
            return prepared

    def prepare(self, question):
        if self._staged is not None:
            staged_question, prepared = self._staged
            self._staged = None
            assert question == staged_question, 'Prepared evidence belongs to another request'
            return prepared
        return super().prepare(question)

    def ask(self, question):
        current = self.bound_prepared
        self.bound_prepared = None
        if current is not None:
            assert current[0] == question, 'Record belongs to another request'
            return self.evidence_registry.finish(current[1]['direct_record_response'])
        return self.evidence_registry.finish(super().ask(question))

    def close(self):
        """Close writable caches; shared model and embedding services stay owned by the host."""
        if self._closed:
            return
        self._closed = True
        self._staged = None
        self.bound_prepared = None
        self.stable.cache.close()
        self.context.close()
