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
        self.stable.legacy.coverage_ocr_originals = self.bridge.ocr

    @timed('evidence.direct_plan')
    def prepare_direct(self, question):
        self.bound_prepared = None
        self._staged = None
        direct = self.field_projection.prepare(question)
        if direct is None:
            direct = self.bound_facts.prepare(question)
        if direct is None:
            direct = self.question_plan.prepare(question)
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
