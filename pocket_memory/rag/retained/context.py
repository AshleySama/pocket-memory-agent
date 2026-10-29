# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Instance dependencies for retained QA; importing this module loads no model."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocket_memory.embedding import resource_path


@dataclass
class RuntimeContext:
    store: Any
    embedding_service: Any
    engine: Any
    cache_dir: Path | None = None
    reranker_model_dir: Path | None = None
    reranker_manifest: Path | None = None
    _reranker: Any = field(default=None, init=False, repr=False)

    def __post_init__(self):
        data_dir = Path(self.store.data_dir).resolve()
        self.cache_dir = Path(self.cache_dir or data_dir / 'retained-cache').resolve()
        if not self.cache_dir.is_relative_to(data_dir):
            raise ValueError('Retained caches must belong to this store data directory')
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.reranker_model_dir = Path(self.reranker_model_dir or resource_path('models/reranker-models/ms-marco-MultiBERT-L-12')).resolve()
        self.reranker_manifest = Path(self.reranker_manifest or resource_path('models/reranker-manifest.json')).resolve()

    @property
    def reranker(self):
        if self._reranker is None:
            from .rerank_adapter import Reranker
            self._reranker = Reranker(self.reranker_model_dir, self.reranker_manifest, self.cache_dir)
        return self._reranker

    def close(self):
        """Release candidate-owned state without touching shared host services."""
        if self._reranker is not None:
            self._reranker.close()
            self._reranker = None

