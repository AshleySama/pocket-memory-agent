# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Isolated table contract, with the prior interface evidence path untouched."""
import json
import re
import unicodedata
from pathlib import Path
import time
from .table_contract import request_for, render
from .coverage import coverage_pack, CoverageGeneration

from . import evidence_candidate as prior
from . import pilot


class Candidate(prior.Candidate):
    def __init__(self, context):
        super().__init__(context)
        self._retrieval_once=None
        self.coverage_trace={}
        self.current_scope=[]

    def pack(self,retrieved):
        result=coverage_pack(self.question,retrieved,self.notes,self.schemas,self.current_scope)
        if result is not None:
            packed,omitted,self.coverage_trace=result
            self.schema_trace={'active':False,'reason':'verified whole-source composite coverage'}
            return packed,omitted
        self.coverage_trace={'active':False}
        return super().pack(retrieved)

    def retrieve(self,question):
        if self._retrieval_once is not None:
            result=self._retrieval_once; self._retrieval_once=None
            return result
        retrieved,scope=super().retrieve(question)
        # RRF may return several mentions of one subject but omit the sources
        # explicitly named in a multi-source question. Reserve one relevant
        # existing chunk per exact title, within the same verified scope.
        allowed,_=self.scope(question)
        mentioned={}
        q=unicodedata.normalize('NFKC',question).casefold()
        for node in allowed:
            meta=node.metadata
            source=(self.notes if meta['source_type']=='note' else self.documents)[meta['source_id']]
            title=unicodedata.normalize('NFKC',source['title']).strip().casefold()
            if len(title)<4:continue
            pattern=re.escape(title).replace(r'\ ',r'\s+')
            if title[0].isascii() and title[0].isalnum():pattern=r'(?<![a-z0-9_])'+pattern
            if title[-1].isascii() and title[-1].isalnum():pattern+=r'(?![a-z0-9_])'
            if re.search(pattern,q):mentioned.setdefault(title,[]).append(node)
        # A shorter title contained in another explicit title is not a second
        # independent subject (e.g. a person's name within a film title).
        mentioned={title:nodes for title,nodes in mentioned.items() if not any(title!=other and title in other for other in mentioned)}
        if not 2<=len(mentioned)<=4:return retrieved,scope
        import numpy as np
        from llama_index.core.schema import NodeWithScore
        query=np.asarray(self.embed.get_query_embedding(question),dtype=np.float32)
        anchors=[]
        for nodes in mentioned.values():
            # Duplicate titles in distinct sources are ambiguous, not aliases.
            if len({n.metadata['source_key'] for n in nodes})!=1:return retrieved,scope
            ranked=sorted(nodes,key=lambda n:float(np.dot(query,n.embedding)),reverse=True)
            anchors.append(NodeWithScore(node=ranked[0],score=float(np.dot(query,ranked[0].embedding))))
        ids={a.node.node_id for a in anchors}
        return (anchors+[h for h in retrieved if h.node.node_id not in ids])[:8],scope

    def scope(self,question):
        allowed,scope=super().scope(question)
        if any(not s.get('matched_sources') for s in scope):return allowed,scope
        names=re.findall(r'[《【]([^》】]+)[》】]',question)
        outside=re.sub(r'[《【][^》】]+[》】]','',question)
        requested=re.findall(r'(?:页签|标签页)\s*[0-9一二三四五六七八九十]+',outside)
        # Quoted titles were removed above. Preserve the grammatical link in
        # “只看《标题》的首页”, including whitespace and ordinary quote marks.
        main_only=bool(re.search(r'(?:只看|仅看|仅限|只限|只在|只根据|仅根据)[\s的“”\"\u300c\u300d]*(?:主笔记|首页|主页)',outside))
        if not names or not (requested or main_only):return allowed,scope
        parent_ids={i for i,n in self.notes.items() if any(pilot.norm(n['title'])==pilot.norm(name) for name in names)}
        if not parent_ids:return allowed,scope
        if main_only and requested:return [],scope+[{'quoted_title':'conflicting_tab_scope','matched_sources':[]}]
        if main_only:
            keys={f'note:{i}' for i in parent_ids}
            scope=[dict(s,matched_sources=[k for k in s['matched_sources'] if k in keys]) for s in scope]
            return [n for n in allowed if n.metadata['source_key'] in keys],scope
        children=[n for n in self.notes.values() if n.get('parent_note_id') in parent_ids]
        selected=set()
        # An explicit union includes the home page as well as named children.
        if re.search(r'(?:首页|主页|主笔记)',outside):selected.update(parent_ids)
        for name in requested:
            matches={n['id'] for n in children if pilot.norm(name) in {pilot.norm(n.get('tab_name') or ''),pilot.norm(n['title'])}}
            if not matches:return [],scope+[{'quoted_title':name,'matched_sources':[]}]
            selected.update(matches)
        keys={f'note:{i}' for i in selected}
        # The inherited scope may have selected just the child. Use the verified
        # parent/child identities, never arbitrary nodes outside the named family.
        scope=[dict(s,matched_sources=sorted(keys)) for s in scope]
        return [n for n in self.nodes if n.metadata['source_key'] in keys],scope

    def ask(self,question):
        start=time.perf_counter()
        retrieved,scope=self.retrieve(question)
        missing=[s['quoted_title'] for s in scope if not s.get('matched_sources')]
        if not retrieved and missing:
            return dict(answer='未找到指定的资料或页签：'+ '、'.join(missing)+'。请核对名称；当前不使用其他资料代答。',
                        sources=[],provided_sources=[],answer_status='missing_source_scope',
                        trace=dict(scope=scope,retrieved=[],route='missing_source_scope',seconds=time.perf_counter()-start,generation_calls=0))
        # Only an explicit table entity activates this route; unrelated interface
        # hits cannot override it. No matched row means the prior path is reused.
        request=request_for(question,retrieved,self.notes,scope)
        if request is None:
            self.current_scope=scope;self.coverage_trace={}
            self._retrieval_once=(retrieved,scope)
            original=self.engine._llm;self.engine._llm=CoverageGeneration(original,self)
            try:
                response=super().ask(question)
                response['trace']['coverage_context']=self.coverage_trace
                return response
            finally:self.engine._llm=original
        base_trace=dict(scope=scope,retrieved=[dict(node_id=h.node.node_id,score=h.score,**h.node.metadata,text=h.node.text) for h in retrieved],
                        route='table_column_selection_and_source_render',omitted=[],
                        citation_guard={'repair_calls':0},schema_context={'active':False,'reason':'separate general table contract'})
        if request.get('blocked'):
            return dict(answer=request['blocked'],sources=[],provided_sources=[],answer_status='table_scope_clarify',trace=base_trace)
        generated=self.engine._llm.create_chat_completion(messages=request['messages'],max_tokens=220,temperature=0,
                  response_format={'type':'json_object','schema':request['schema']})
        choice=generated['choices'][0];raw=choice['message']['content'] or ''
        try: plan=json.loads(raw)
        except (ValueError,TypeError): plan=None
        result,errors=render(plan,request)
        if result is None:
            result=dict(answer='表格查询的列选择未通过核对，暂不生成未经核对的答案。',sources=[],provided_sources=[],answer_status='table_plan_failed')
        base_trace.update(messages=request['messages'],response_format={'type':'json_object','schema':request['schema']},
                          raw_model_output=raw,plan=plan,plan_errors=errors,usage=generated.get('usage'),
                          finish_reason=choice['finish_reason'],seconds=time.perf_counter()-start,
                          answer_before_citation_guard=result['answer'],
                          rendering='Model selects requested column names; original cell values and exact references rendered deterministically. Not free-form answer generation.')
        result.update(trace=base_trace,citation_validation={'passed':bool(result['sources']) and not errors,'errors':errors,
                       'scope':'exact source cell and row identity; question coverage independently reviewed'})
        return result
