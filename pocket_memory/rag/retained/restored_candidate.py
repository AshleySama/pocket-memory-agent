# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Restore the prior isolated answer protocol; never changes production imports.

Retrieval and packing reuse the accepted pre-chain pilot (BGE/BM25/RRF).
One answer completion only. Existing deterministic citation guard is reused;
its optional second model call is disabled for this frozen eight-call batch.
"""
from . import pilot
from .citation_guard import apply_guard


class Candidate(pilot.Candidate):
    def __init__(self, context):
        super().__init__(context, round_number=2)

    def ask(self, question):
        return apply_guard(super().ask(question), repair_callback=None)
