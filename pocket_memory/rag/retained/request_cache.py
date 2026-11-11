# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Reuse identical physical retrieval only during one evidence stage.

Callers still resolve scope and execute every retrieval/aggregation channel.
No candidate, rank or RRF vote is removed. Cache only detached result snapshots:
upstream fusion and downstream evidence expansion both mutate returned objects.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy

from pocket_memory.rag.performance import metric


class _RequestCache:
    def __init__(self):
        self.entries = {}
        self.active = True


_current = ContextVar('pocket_memory_retained_retrieval_cache', default=None)


@contextmanager
def retrieval_request():
    """Each stage gets a fresh cache, including a nested stage or a failed one."""
    state = _RequestCache()
    token = _current.set(state)
    try:
        yield
    finally:
        # A copied Context may outlive this block. It must not keep this stage's
        # source content or continue using this stage's cache after it exits.
        state.active = False
        state.entries.clear()
        _current.reset(token)


def cached_retrieval(owner, channel, query, signature, retrieve):
    """Run a physical retrieval with an exact, caller-supplied snapshot key.

    Only literal string queries are cached. The owner is retained with the
    entry so its id cannot be recycled during the request. Exceptions from the
    retriever are propagated unchanged and never cached. Outside stage(), this
    helper is a transparent call-through.
    """
    state = _current.get()
    if state is None or not state.active or type(query) is not str:
        return retrieve()
    key = (id(owner), channel, query, signature)
    try:
        hash(key)
    except TypeError:
        # Do not change the existing retriever's accepted-input/exception
        # behavior when a caller cannot provide a complete hashable key.
        return retrieve()
    if key in state.entries:
        metric('retrieve.request_cache_hits')
        return copy.deepcopy(state.entries[key][1])
    metric('retrieve.request_cache_misses')
    result = retrieve()
    stored = copy.deepcopy(result)
    state.entries[key] = (owner, stored)
    return copy.deepcopy(stored)
