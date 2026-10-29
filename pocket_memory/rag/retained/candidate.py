# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Accepted bound-facts entry point with explicit host-owned dependencies.

The wrapper preserves the original stage/ask binding, source-scope adapter,
registry finalization and deterministic direct-record branch. The caller owns
request serialization and must ensure its shared text engine is available.
Automatic completion retry remains disabled.
"""
from .clause_candidate import Candidate as RetainedCandidate
from .facts import BoundFacts
from .registry import EvidenceRegistry
from .source_scope import ScopedRetrieval


class Candidate(RetainedCandidate):
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

    def stage(self, question):
        self.bound_prepared = None
        direct = self.bound_facts.prepare(question)
        if direct:
            self._staged = None
            self.bound_prepared = (question, direct)
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
