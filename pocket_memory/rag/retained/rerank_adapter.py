# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Offline upstream FlashRank; a scorer, never another answer generator."""
import hashlib,json,sqlite3,threading
from pathlib import Path
import onnxruntime as ort
from tokenizers import BertWordPieceTokenizer
from flashrank import Ranker,RerankRequest
MODEL='ms-marco-MultiBERT-L-12'
TOKENIZER_PROTOCOL='original-vocab-bert-wordpiece-v1'

class Reranker:
    """Runtime-owned scorer and writable cache; no cross-store mutable handles."""
    def __init__(self, model_dir, manifest, cache_dir):
        self.model_dir=Path(model_dir)
        self.manifest=Path(manifest)
        self.cache_dir=Path(cache_dir)
        self._ranker=None
        self._cache=None
        self._lock=threading.RLock()

    def get_ranker(self):
        if self._ranker is None:
            model=self.model_dir/'flashrank-MultiBERT-L12_Q.onnx'
            assert model.is_file(),'Local reranker absent; no inference-time downloads'
            self._ranker=Ranker(model_name=MODEL,cache_dir=str(self.model_dir.parent),max_length=512,log_level='ERROR')
            # The upstream inference/tokenization is retained; limit CPU work to the
            # same four threads as the answer model instead of all host cores.
            self._ranker.session=None
            options=ort.SessionOptions();options.intra_op_num_threads=4;options.inter_op_num_threads=1
            self._ranker.session=ort.InferenceSession(str(model),sess_options=options,providers=['CPUExecutionProvider'])
            # The upstream zip's tokenizer.json is an unrelated 30,522-word English
            # tokenizer; this model and its vocab.txt use 105,879 words. Setting a
            # Python .vocab attribute does not change Rust tokenizers' actual IDs.
            # Reconstruct from the original vocabulary, preserving physical LF lines
            # (two valid vocabulary tokens themselves contain Unicode line separators).
            config=json.loads((model.parent/'config.json').read_text('utf-8'))
            tc=json.loads((model.parent/'tokenizer_config.json').read_text('utf-8'))
            with (model.parent/'vocab.txt').open(encoding='utf-8') as f:
                vocab={token.rstrip('\n'):i for i,token in enumerate(f)}
            assert len(vocab)==config['vocab_size'] and max(vocab.values())+1==len(vocab)
            tokenizer=BertWordPieceTokenizer(vocab,lowercase=tc['do_lower_case'],
                strip_accents=tc.get('strip_accents'),handle_chinese_chars=tc['tokenize_chinese_chars'])._tokenizer
            assert tokenizer.get_vocab()==vocab, 'Tokenizer/model vocabulary mismatch'
            tokenizer.enable_truncation(max_length=min(512,tc['model_max_length']))
            tokenizer.enable_padding(pad_id=config['pad_token_id'],pad_token=tc['pad_token'])
            self._ranker.tokenizer=tokenizer
            self._cache=sqlite3.connect(self.cache_dir/'reranker-cache.sqlite',check_same_thread=False)
            self._cache.execute('CREATE TABLE IF NOT EXISTS scores (key TEXT PRIMARY KEY, score REAL NOT NULL)')
        return self._ranker

    def rerank(self,question,passages):
        with self._lock:
            ranker=self.get_ranker()
            manifest=self.manifest.read_text('utf-8')
            pending=[];scores={};keys={}
            for p in passages:
                key=hashlib.sha256((TOKENIZER_PROTOCOL+'\n'+manifest+'\n'+question+'\n'+p['text']).encode()).hexdigest()
                keys[p['id']]=key;row=self._cache.execute('SELECT score FROM scores WHERE key=?',(key,)).fetchone()
                if row:scores[p['id']]=row[0]
                else:pending.append(dict(p))
            for start in range(0,len(pending),8):
                result=ranker.rerank(RerankRequest(query=question,passages=pending[start:start+8]))
                for p in result:
                    value=float(p['score']);scores[p['id']]=value
                    self._cache.execute('INSERT OR REPLACE INTO scores VALUES (?,?)',(keys[p['id']],value))
            self._cache.commit()
            return sorted([dict(p,score=scores[p['id']]) for p in passages],key=lambda p:p['score'],reverse=True)

    def close(self):
        with self._lock:
            if self._cache is not None:
                self._cache.close()
                self._cache=None
            self._ranker=None
