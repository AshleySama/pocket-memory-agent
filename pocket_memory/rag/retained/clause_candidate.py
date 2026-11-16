# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Review candidate: v4 generation and preserved exact fixes are unchanged.

Unit proposals remain shadow-only until their efficacy/safety gate passes.
"""
from . import coverage_candidate as v4
from .clause_attribution import reconcile
from .citation_guard import apply_guard
from .literal_list_binding import reconcile as bind_literal_list
pilot = v4.pilot
VERSION = 'rag-clause-citation-v2-shadow'


class Candidate(v4.Candidate):
    def ask(self, question):
        response = bind_literal_list(question, super().ask(question), self.notes)
        result, analysis = reconcile(response, apply_guard)
        result['trace']['clause_attribution'] = analysis
        return result
