# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Isolated evidence layer; model, retrieval, prompt and citation guard unchanged."""
from pathlib import Path
import sqlite3
from .evidence_layer import parse_schema,make_block,validate_field_references

from . import restored_candidate as prior
from . import pilot
from .citation_guard import apply_guard,indices

class Candidate(prior.Candidate):
    def __init__(self, context):
        super().__init__(context)
        with sqlite3.connect(context.store.db_path.resolve().as_uri()+'?mode=ro',uri=True) as con:
            ocr=dict(con.execute('SELECT id,ocr_text FROM notes'))
        self.schemas={i:s for i,n in self.notes.items() if (s:=parse_schema(n['content'],ocr.get(i,'')))}
        self.question='';self.schema_trace={}

    def pack(self,retrieved):
        ids=[]
        for hit in retrieved:
            meta=hit.node.metadata;i=meta.get('source_id')
            if meta.get('source_type')=='note' and i in self.schemas and i not in ids:ids.append(i)
        if not ids:
            self.schema_trace={'active':False,'reason':'non-schema path unchanged'}
            return super().pack(retrieved)
        packed=[];budget=1500
        for i in ids:
            block=make_block(i,self.notes[i]['title'],self.question,self.schemas[i],len(packed)+1,budget)
            if block:packed.append(block);budget-=len(block['prompt_block'])+2
        if not packed:
            self.schema_trace={'active':False,'reason':'no view fits; preserve original path'}
            return super().pack(retrieved)
        # Schema views already include original selected spans. Remaining original
        # fragments can supplement them; no document-wide equivalence is claimed.
        baseline,_=super().pack(retrieved);omitted=[]
        for block in baseline:
            raw=block['evidence']
            duplicate=any(raw in p['evidence'] for p in packed)
            if duplicate:
                omitted.append(dict(node_id=block['node_id'],reason='exact_text_already_in_evidence'));continue
            heading=f'[{len(packed)+1}] {block["title"]} · 补充原文片段\n'
            if len(heading+raw)+2<=budget:
                block=dict(block,index=len(packed)+1,prompt_block=heading+raw)
                schema=self.schemas.get(block.get('source_id')) if block.get('source_type')=='note' else None
                if schema:
                    supported=[r for r in schema['rows'] if r['raw'] in raw]
                    if supported:
                        block.update(structured_rows=supported,schema_all_fields=[r['name'] for r in schema['rows']],
                            coverage_complete=False,coverage_notice='补充原文片段，非完整笔记。',
                            raw_evidence=[dict(field='content',quote=r['raw'],char_start=r['char_start'],char_end_exclusive=r['char_end_exclusive']) for r in supported])
                packed.append(block);budget-=len(block['prompt_block'])+2
            else:omitted.append(dict(node_id=block['node_id'],reason='remaining_budget_insufficient_for_whole_fragment',chars=len(raw)))
        self.schema_trace={'active':True,'budget':1500,'remaining':budget,
            'selected_rows':{str(p['source_id']):[r['name'] for r in p.get('structured_rows',[])] for p in packed if p.get('structured_rows')},
            'scope':'source records with original spans; supplements only within budget; omissions logged, no whole-note OCR substitution'}
        return packed,omitted

    def ask(self,question):
        self.question=question
        raw=pilot.Candidate.ask(self,question)
        errors=validate_field_references(raw['answer'],raw['provided_sources'],indices(raw['answer']))
        response=apply_guard(raw,repair_callback=None)
        response['trace']['schema_context']=self.schema_trace
        response['trace']['field_reference_check']={'passed':not errors,'errors':errors,
            'scope':'prior field membership/glue and limited absence-scope guard unchanged; not semantic entailment'}
        if errors:
            safe='这次回答的字段与引用未通过核对，暂不展示未经核对的结论。可先查看原文字段记录。'
            if 'absence_scope_not_limited_to_fragment' in errors:
                safe='当前可核对的表格或 OCR 只覆盖部分资料，无法据此确认所问字段；片段未列出不代表整份资料不存在该字段。'
            response.update(answer=safe,
                sources=[],answer_status='field_reference_check_failed',retrieved_evidence=response['provided_sources'],
                citation_validation={'passed':False,'errors':errors,'scope':'exact field membership only'})
        return response
