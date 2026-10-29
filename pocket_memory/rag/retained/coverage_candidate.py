# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""One explicit compatibility boundary and one free-text completion path.

The pinned v19 module is the preserved capability boundary. The verified bridge
is used only as a source assembly component, never as another answer engine.
No question planner, generated source selector or answer resynthesis is used.
"""
import time
from . import pilot
from .prepared_pipeline import PreparedStable
from .preserved_units import OriginalUnits
from .coverage_contract import CoverageContract
from .evidence_contract import assemble, guard, locations
from . import evidence_bridge as canonical
VERSION='rag-sentence-coverage-v4'

class Candidate:
    def __init__(self, context):
        self.context=context
        self.stable=PreparedStable(context)
        self.bridge=canonical.Bridge(self.stable,context.store.db_path)
        self.original_units=OriginalUnits(self.stable)
        self.coverage=CoverageContract(self.stable)
    def __getattr__(self,name):return getattr(self.stable,name)
    @property
    def engine(self):return self.stable.engine
    @engine.setter
    def engine(self,value):self.stable.engine=value
    @property
    def llm(self):return self.stable.llm
    @llm.setter
    def llm(self,value):self.stable.llm=value
    def prepare(self,question):
        p=self.stable.prepare(question)
        families={self.stable.notes.get(h.node.metadata.get('source_id'),{}).get('parent_note_id') or h.node.metadata['source_key']
            for h in p.get('retrieved',[]) if hasattr(h,'node')}
        ordinary=p.get('ordinary_context_contract',False)
        if ordinary:
            ordinary_prepared=self.stable.contract.prepare(question,p['retrieved'])
            revised=self.original_units.prepare(question,p['retrieved'],ordinary_prepared)
            if revised is not None:
                sources,messages,trace=revised
                tokens=self.stable.contract.count(messages)
                return dict(mode='single_free_text',sources=sources,messages=messages,baseline_route=p['route'],
                    evidence_bridge=trace,assembly=[],input_tokens=tokens,context_fits=tokens+512+128<=3072)
        affected=p['route']=='standard_retrieval' or (ordinary and len(families)>1 and len(canonical.facets(question))>1)
        if not affected and ordinary:
            replacement=self.coverage.prepare(question,ordinary_prepared)
            if replacement is not None:
                ordinary_prepared=replacement
        if not affected:
            p['sources']=locations(p['sources'],self.notes,self.bridge.ocr)
            return dict(mode='preserved_v19',baseline_preparation=p,ordinary_prepared=ordinary_prepared if ordinary else None)
        bridged=self.bridge.prepare(question,p)
        sources,audit=assemble(bridged['sources'])
        messages=self.stable.contract.messages(question,sources)
        tokens=self.stable.contract.count(messages)
        return dict(mode='single_free_text',sources=sources,messages=messages,
            baseline_route=p['route'],evidence_bridge=bridged.get('evidence_bridge'),
            assembly=audit,input_tokens=tokens,context_fits=tokens+512+128<=3072)
    def ask(self,question):
        start=time.perf_counter();p=self.prepare(question)
        if p['mode']=='preserved_v19':
            # No runtime monkeypatch of scope, retrieval or the old prepare.
            response=guard(self.stable.execute_prepared(question,p['baseline_preparation'],p.get('ordinary_prepared')))
            for key in ('provided_sources','sources'):
                response[key]=locations(response.get(key,[]),self.notes,self.bridge.ocr)
            response['trace']['closeout']=dict(mode='preserved_v19',version=VERSION,seconds=time.perf_counter()-start)
            return response
        if not p['context_fits']:
            return dict(answer='本次相关原文超出可完整处理的范围，请缩小资料范围后再问。',sources=[],provided_sources=p['sources'],
                answer_status='incomplete',citation_validation=dict(passed=True,errors=[]),trace=dict(closeout=dict(mode='blocked_budget',version=VERSION),generation_calls=0))
        generated=self.engine._llm.create_chat_completion(messages=p['messages'],max_tokens=512,temperature=0)
        ch=generated['choices'][0]
        response=guard(dict(answer=ch['message']['content'] or '',provided_sources=p['sources'],sources=[],
            trace=dict(messages=p['messages'],closeout=dict(mode='single_free_text',version=VERSION),
                assembly=p['assembly'],evidence_bridge=p['evidence_bridge'],baseline_route=p['baseline_route'],
                budget=dict(context=3072,input_tokens=p['input_tokens'],output=512,margin=128),
                generation_calls=1,finish_reason=ch['finish_reason'],usage=generated.get('usage'),seconds=time.perf_counter()-start)))
        if ch['finish_reason']!='stop':response['answer_status']='incomplete'
        return response
