# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Prepared rows for the retained candidate's owned SimpleVectorStore snapshot.

Keep upstream float64 row-wise cosine arithmetic and heap selection unchanged.
The optimization only removes repeated list-to-array conversion and norms, and
reuses an identical dense query inside one evidence stage. Other query modes,
filters and unrecognized options retain the upstream implementation.

The retained index owns its data. Mutations go through add/delete/delete_nodes/
clear; directly editing a stored embedding list is not a supported snapshot edit.
"""
import heapq

import numpy as np
from llama_index.core.bridge.pydantic import PrivateAttr
from llama_index.core.vector_stores.simple import SimpleVectorStore
from llama_index.core.vector_stores.types import (
    VectorStoreQuery, VectorStoreQueryMode, VectorStoreQueryResult,
)

from pocket_memory.rag.performance import metric, span, timed
from .request_cache import cached_retrieval


_QUERY_FIELDS = frozenset((
    'query_embedding', 'similarity_top_k', 'doc_ids', 'node_ids', 'query_str',
    'output_fields', 'embedding_field', 'mode', 'alpha', 'filters',
    'mmr_threshold', 'sparse_top_k', 'hybrid_top_k',
))
_UNSUPPORTED_FIELDS = (
    'doc_ids', 'output_fields', 'embedding_field', 'alpha', 'filters',
    'mmr_threshold', 'sparse_top_k', 'hybrid_top_k',
)


class PreparedVectorStore(SimpleVectorStore):
    """An instance-local adapter; upstream/global vector stores are untouched."""

    _generation: int = PrivateAttr(default=0)
    _prepared_generation: int = PrivateAttr(default=-1)
    _prepared_data: object = PrivateAttr(default=None)
    _prepared_embeddings: object = PrivateAttr(default=None)
    _matrix: object = PrivateAttr(default=None)
    _norms: object = PrivateAttr(default=None)
    _node_ids: tuple = PrivateAttr(default=())

    def _invalidate(self):
        self._generation += 1
        self._prepared_generation = -1
        self._prepared_data = None
        self._prepared_embeddings = None
        self._matrix = None
        self._norms = None
        self._node_ids = ()

    def add(self, nodes, **add_kwargs):
        self._invalidate()
        return super().add(nodes, **add_kwargs)

    def delete(self, ref_doc_id, **delete_kwargs):
        self._invalidate()
        return super().delete(ref_doc_id, **delete_kwargs)

    def delete_nodes(self, node_ids=None, filters=None, **delete_kwargs):
        self._invalidate()
        return super().delete_nodes(node_ids=node_ids, filters=filters, **delete_kwargs)

    def clear(self):
        self._invalidate()
        return super().clear()

    def _prepare_rows(self):
        if self._prepared_generation == self._generation:
            if self._prepared_data is self.data and self._prepared_embeddings is self.data.embedding_dict:
                return self._matrix is not None
            # Also support replacing the whole data object/dictionary, such as
            # adopting a loaded snapshot. Never reuse the former dense result.
            self._invalidate()
        self._prepared_generation = self._generation
        self._prepared_data = self.data
        self._prepared_embeddings = self.data.embedding_dict
        with span('retrieve.dense_prepare_rows'):
            try:
                # Match upstream np.array(embeddings), including dtype. Do not
                # turn integer, ragged or custom embeddings into float64 data.
                matrix = np.array(list(self.data.embedding_dict.values()))
            except (TypeError, ValueError):
                return False
            if matrix.ndim != 2 or matrix.dtype != np.dtype('float64'):
                return False
            if any(np.asarray(row).dtype != matrix.dtype
                   for row in self.data.embedding_dict.values()):
                # A subset containing only integer/float32 rows would have a
                # different upstream dtype from the complete mixed matrix.
                return False
            # These are the same one-dimensional norm calls used upstream,
            # computed once per immutable row rather than once per query.
            norms = np.array([np.linalg.norm(row) for row in matrix])
            matrix.setflags(write=False)
            norms.setflags(write=False)
            self._matrix = matrix
            self._norms = norms
            self._node_ids = tuple(self.data.embedding_dict)
            metric('retrieve.dense_matrix_builds')
        return True

    @staticmethod
    def _supported(query, kwargs):
        if type(query) is not VectorStoreQuery or kwargs or set(vars(query)) != _QUERY_FIELDS:
            return False
        if query.mode != VectorStoreQueryMode.DEFAULT:
            return False
        if any(getattr(query, name) is not None for name in _UNSUPPORTED_FIELDS):
            return False
        if query.query_str is not None and type(query.query_str) is not str:
            return False
        if query.similarity_top_k is not None and type(query.similarity_top_k) is not int:
            return False
        return query.node_ids is None or (
            type(query.node_ids) is list and all(type(value) is str for value in query.node_ids))

    def _query_rows(self, vector, top_k, node_ids):
        with span('retrieve.dense_scan'):
            allowed = None if node_ids is None else set(node_ids)
            query_norm = np.linalg.norm(vector)
            similarity_heap = []
            scored = 0
            # Retain embedding_dict insertion order, not the caller's node_ids
            # order. The tuple heap tie behavior is part of the old protocol.
            for i, node_id in enumerate(self._node_ids):
                if allowed is not None and node_id not in allowed:
                    continue
                product = np.dot(vector, self._matrix[i])
                norm = query_norm * self._norms[i]
                similarity = product / norm
                heapq.heappush(similarity_heap, (similarity, node_id))
                if top_k and len(similarity_heap) > top_k:
                    heapq.heappop(similarity_heap)
                scored += 1
            result = sorted(similarity_heap, key=lambda item: item[0], reverse=True)
            metric('retrieve.dense_scored_rows', scored)
            return VectorStoreQueryResult(
                similarities=[score for score, _ in result],
                ids=[node_id for _, node_id in result])

    @timed('retrieve.dense')
    def query(self, query, **kwargs):
        if not self._supported(query, kwargs):
            return super().query(query, **kwargs)
        try:
            vector = np.array(query.query_embedding)
        except (TypeError, ValueError):
            return super().query(query, **kwargs)
        if vector.ndim != 1 or vector.dtype != np.dtype('float64'):
            return super().query(query, **kwargs)
        if not self._prepare_rows() or vector.shape[0] != self._matrix.shape[1]:
            return super().query(query, **kwargs)
        node_ids = None if query.node_ids is None else tuple(query.node_ids)
        # Every supported input field is represented, or checked against its
        # fixed default above. Bytes are immutable and preserve signed zeros;
        # a reused/mutated query list cannot accidentally hit an old result.
        signature = (self._generation, query.mode, query.similarity_top_k,
                     node_ids, query.query_str, vector.dtype.str,
                     vector.shape, vector.tobytes())
        return cached_retrieval(self, 'dense', query.query_str or '', signature,
            lambda: self._query_rows(vector, query.similarity_top_k, node_ids))
