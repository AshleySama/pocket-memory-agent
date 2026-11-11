# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Offline upstream FlashRank; a scorer, never another answer generator."""
import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path

import onnxruntime as ort
from tokenizers import BertWordPieceTokenizer
from flashrank import Ranker, RerankRequest

from ..performance import metric, span, timed

MODEL = 'ms-marco-MultiBERT-L-12'
TOKENIZER_PROTOCOL = 'original-vocab-bert-wordpiece-v1'


class _LocalRanker(Ranker):
    """Initialize only the attributes used by FlashRank 0.2.10's rerank.

    Keep the upstream tokenization/inference/scoring method unchanged. Its
    initializer would create an extra session and load the unrelated tokenizer
    shipped in tokenizer.json, both of which the retained adapter replaced.
    """

    def __init__(self, model_dir, tokenizer):
        self.logger = logging.getLogger(__name__)
        self.model_dir = Path(model_dir)
        self.cache_dir = self.model_dir.parent
        self.llm_model = None
        self.tokenizer = tokenizer
        model = self.model_dir / 'flashrank-MultiBERT-L12_Q.onnx'
        assert model.is_file(), 'Local reranker absent; no inference-time downloads'
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model), sess_options=options, providers=['CPUExecutionProvider'])
        metric('rerank.session_creations')


class Reranker:
    """Runtime-owned scorer and writable cache; no cross-store mutable handles."""

    def __init__(self, model_dir, manifest, cache_dir):
        self.model_dir = Path(model_dir)
        self.manifest = Path(manifest)
        self.cache_dir = Path(cache_dir)
        self._ranker = None
        self._tokenizer = None
        self._cache = None
        self._lock = threading.RLock()

    def get_tokenizer(self):
        """Allow evidence token counting without loading the ONNX scorer."""
        with self._lock:
            if self._tokenizer is None:
                with span('rerank.tokenizer_load'):
                    # tokenizer.json contains an unrelated 30,522-word English
                    # tokenizer. Rebuild from the original 105,879-word vocab.
                    # Preserve physical LF lines: two vocabulary entries contain
                    # Unicode line separators, which str.splitlines would split.
                    config = json.loads((self.model_dir / 'config.json').read_text('utf-8'))
                    tc = json.loads((self.model_dir / 'tokenizer_config.json').read_text('utf-8'))
                    with (self.model_dir / 'vocab.txt').open(encoding='utf-8') as f:
                        vocab = {token.rstrip('\n'): i for i, token in enumerate(f)}
                    assert len(vocab) == config['vocab_size'] and max(vocab.values()) + 1 == len(vocab)
                    tokenizer = BertWordPieceTokenizer(
                        vocab, lowercase=tc['do_lower_case'],
                        strip_accents=tc.get('strip_accents'),
                        handle_chinese_chars=tc['tokenize_chinese_chars'])._tokenizer
                    assert tokenizer.get_vocab() == vocab, 'Tokenizer/model vocabulary mismatch'
                    tokenizer.enable_truncation(max_length=min(512, tc['model_max_length']))
                    tokenizer.enable_padding(pad_id=config['pad_token_id'], pad_token=tc['pad_token'])
                    self._tokenizer = tokenizer
            return self._tokenizer

    def get_ranker(self):
        with self._lock:
            if self._ranker is None:
                with span('rerank.model_load'):
                    self._ranker = _LocalRanker(self.model_dir, self.get_tokenizer())
            return self._ranker

    def _get_cache(self):
        if self._cache is None:
            with span('rerank.cache_open'):
                cache = sqlite3.connect(self.cache_dir / 'reranker-cache.sqlite', check_same_thread=False)
                try:
                    cache.execute('CREATE TABLE IF NOT EXISTS scores (key TEXT PRIMARY KEY, score REAL NOT NULL)')
                except Exception:
                    cache.close()
                    raise
                self._cache = cache
        return self._cache

    @timed('rerank.total')
    def rerank(self, question, passages):
        with self._lock:
            metric('rerank.calls')
            metric('rerank.passages', len(passages))
            if not passages:
                return []
            cache = self._get_cache()
            manifest = self.manifest.read_text('utf-8')
            pending = []
            scores = {}
            keys = {}
            hits = misses = 0
            with span('rerank.sql'):
                for passage in passages:
                    # Preserve the persisted key protocol and the original ID
                    # mapping, including the original handling of repeated IDs.
                    key = hashlib.sha256((TOKENIZER_PROTOCOL + '\n' + manifest + '\n' + question + '\n' + passage['text']).encode()).hexdigest()
                    keys[passage['id']] = key
                    row = cache.execute('SELECT score FROM scores WHERE key=?', (key,)).fetchone()
                    if row is not None:
                        scores[passage['id']] = row[0]
                        hits += 1
                    else:
                        pending.append(dict(passage))
                        misses += 1
            metric('rerank.cache_hits', hits)
            metric('rerank.cache_misses', misses)
            if pending:
                ranker = self.get_ranker()
                # The real ONNX scorer is batch-shape sensitive. Keep every
                # original pending passage, order and batch boundary, including
                # duplicate texts. Do not reuse a score across pending IDs.
                for start in range(0, len(pending), 8):
                    batch = pending[start:start + 8]
                    metric('rerank.inferred_pairs', len(batch))
                    with span('rerank.inference'):
                        result = ranker.rerank(RerankRequest(
                            query=question, passages=batch))
                    with span('rerank.sql'):
                        for passage in result:
                            value = float(passage['score'])
                            scores[passage['id']] = value
                            cache.execute('INSERT OR REPLACE INTO scores VALUES (?,?)',
                                          (keys[passage['id']], value))
            with span('rerank.sql'):
                cache.commit()
            # Sorting the original sequence retains input order for equal scores.
            return sorted([dict(passage, score=scores[passage['id']]) for passage in passages],
                          key=lambda passage: passage['score'], reverse=True)

    def close(self):
        with self._lock:
            if self._cache is not None:
                self._cache.close()
                self._cache = None
            self._ranker = None
            self._tokenizer = None
