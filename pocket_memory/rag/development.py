# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Live application boundary for the retained RAG protocol.

Evidence snapshots are disposable; user data and model ownership stay with the
normal application. No evaluation corpus, history directory or answer cache is
used here.
"""
import copy
import logging
import threading
import time
from contextlib import nullcontext

from .clarification import clarification_sources
from .intent import IntentResult

logger = logging.getLogger(__name__)


def public_source(source, store):
    out = copy.deepcopy(source)
    out.pop('prompt_block', None)
    kind, ident = out.get('source_type', 'note'), out.get('source_id')
    if kind == 'note' and isinstance(ident, int):
        note = store.get_note(ident)
        out.update(id=ident, source_id=ident, source_type='note',
                   parent_note_id=note.parent_note_id, note_id=ident,
                   url=f'/api/notes/{ident}', tab_name=note.tab_name)
    elif kind == 'document':
        out.update(id=ident, url=f'/api/documents/{ident}/file', source_type='document')
    out.setdefault('excerpt', out.get('evidence', ''))
    out.setdefault('reason', '本地原文证据')
    out.setdefault('location', '原文')
    out.setdefault('matched_terms', [])
    out.setdefault('evidence_locations', [out['location']])
    out.setdefault('evidence_count', 1)
    return out


class LiveRagPipeline:
    def __init__(self, formal, engine, candidate_factory=None, before_refresh=None):
        self.formal, self.store, self.native = formal, formal.store, engine
        self.embedding_service = formal.embedding_service
        self.local = threading.local()
        self._candidate = None
        self._revision = None
        self._model_identity = None
        self._factory = candidate_factory
        self.before_refresh = before_refresh
        self.last = None

    def __getattr__(self, key):
        return getattr(self.formal, key)

    def invalidate(self):
        if self._candidate is not None:
            self._candidate.close()
        self._candidate = None
        self._revision = None
        self._model_identity = None

    close = invalidate

    def _refresh(self):
        if self.before_refresh is not None:
            self.before_refresh()
        drain = getattr(self.embedding_service, 'drain', None)
        if callable(drain) and not drain(timeout=30):
            raise ValueError('资料索引仍在更新，请稍后再提问。')
        errors = getattr(self.embedding_service, 'index_errors', None)
        if callable(errors) and errors():
            raise ValueError('部分资料索引未完成，请重试整理或重新启动后再提问，避免引用旧内容。')
        revision = self.store.source_revision()
        model_identity = (str(getattr(self.native, 'model_path', '')),
                          getattr(self.native, 'n_ctx', None))
        if (self._candidate is None or revision != self._revision
                or model_identity != self._model_identity):
            self.invalidate()
            if self._factory is None:
                from .retained.context import RuntimeContext
                from .retained.candidate import Candidate
                factory = Candidate
                context = RuntimeContext(self.store, self.embedding_service, self.native)
            else:
                factory, context = self._factory, self
            # Background OCR/AI may still write while the snapshot is built.
            # Reject that request instead of combining different revisions.
            self._candidate = factory(context)
            if revision != self.store.source_revision():
                self.invalidate()
                raise ValueError('资料刚刚更新，请重新提问以使用最新内容。')
            self._revision = revision
            self._model_identity = model_identity
        return self._candidate

    def route(self, question, engine=None, **kwargs):
        self.local.response = None
        intent = kwargs.get('intent')
        if intent and intent.type == 'stats':
            return self.formal.route(question, engine, **kwargs)
        question = kwargs.get('evidence_question') or question
        refresh_started = time.perf_counter()
        candidate = self._refresh()
        refresh_seconds = time.perf_counter() - refresh_started
        if hasattr(candidate, 'nodes') and not candidate.nodes:
            return IntentResult(kind='qa', empty_answer='当前资料库没有可检索的文字，请添加笔记或导入资料后再提问。',
                                diagnostics={'schema_version': 1, 'integration': 'retained-development',
                                             'candidate': {'prepared_mode': 'empty_library'}})
        started = time.perf_counter()
        session = getattr(self.native, 'session', None)
        with session() if callable(session) else nullcontext():
            prepared = candidate.stage(question)
        sources = candidate.evidence_registry.stage(prepared)
        self.local.question, self.local.prepared = question, prepared
        self.local.sources, self.local.revision = sources, self._revision
        self.last = dict(question=question, prepared_mode=prepared['mode'],
                         preparation_seconds=time.perf_counter() - started,
                         snapshot_seconds=refresh_seconds)
        # Retain timings only; never log private questions or source text here.
        self.local.performance = {k: v for k, v in self.last.items() if k != 'question'}
        logger.info('retained preparation: %s', self.local.performance)
        return IntentResult(kind='qa', chunks=[{'candidate': True}], prompt_override=question,
                            diagnostics={'schema_version': 1, 'integration': 'retained-development',
                                         'candidate': self.last})

    def source_payloads(self, question, chunks):
        if not chunks or not chunks[0].get('candidate'):
            return self.formal.source_payloads(question, chunks)
        return [public_source(s, self.store) for s in self.local.sources]

    def contexts(self, chunks, sources=None):
        if chunks and chunks[0].get('candidate'):
            return [{'title': 'retained prepared context', 'text': '', 'candidate_prepared': True}]
        return self.formal.contexts(chunks, sources)

    def answer(self, question):
        if question != self.local.question:
            raise RuntimeError('Prepared evidence belongs to another request')
        started = time.perf_counter()
        session = getattr(self.native, 'session', None)
        with session() if callable(session) else nullcontext():
            response = self._candidate.ask(question)
        if self.local.revision != self.store.source_revision():
            response = {'answer': '资料在回答期间发生了更新，请重新提问以使用最新内容。', 'sources': []}
            self.invalidate()
        response['clarification_sources'] = clarification_sources(question, response)
        performance = dict(getattr(self.local, 'performance', {}),
                           answer_seconds=time.perf_counter() - started)
        response.setdefault('trace', {})['performance'] = performance
        logger.info('retained answer timing: %s', performance)
        self.local.response = response
        return response['answer']

    def clarification_source_payloads(self):
        response = getattr(self.local, 'response', None) or {}
        references = []
        for source in response.get('clarification_sources', []):
            public = public_source(source, self.store)
            if public.get('source_type') == 'note' and public.get('parent_note_id') is not None:
                public['url'] = f'/api/notes/{public["parent_note_id"]}?tab_id={public["source_id"]}'
            references.append(public)
        return references

    def enforce_answer_contract(self, question, answer, sources):
        response = getattr(self.local, 'response', None)
        if response is None:
            return self.formal.enforce_answer_contract(question, answer, sources)
        if answer != response['answer']:
            raise RuntimeError('The retained answer was modified outside its contract')
        return answer, [public_source(s, self.store) for s in response.get('sources', [])]


class LiveCandidateEngine:
    def __init__(self, native, pipeline):
        self.native, self.pipeline = native, pipeline

    def __getattr__(self, key):
        return getattr(self.native, key)

    def context_pack_diagnostics(self, question, contexts):
        if contexts and contexts[0].get('candidate_prepared'):
            return {'protocol': 'retained exact staged messages; no repacking'}
        return self.native.context_pack_diagnostics(question, contexts)

    def answer(self, question, contexts, history=None):
        if contexts and contexts[0].get('candidate_prepared'):
            return self.pipeline.answer(question)
        return self.native.answer(question, contexts, history=history)

    def answer_stream(self, question, contexts, history=None):
        if contexts and contexts[0].get('candidate_prepared'):
            yield self.pipeline.answer(question)
        else:
            yield from self.native.answer_stream(question, contexts, history=history)

    def _clean_answer(self, text):
        response = getattr(self.pipeline.local, 'response', None)
        return text if response is not None else self.native._clean_answer(text)
