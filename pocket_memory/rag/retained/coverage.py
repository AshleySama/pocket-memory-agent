# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Whole verified table units and scoped short-note coverage, without gold facts."""
import re
from .table_contract import parse_tables

def coverage_pack(question,retrieved,notes,schemas,scope,budget=1500):
    # Unscoped synthesis remains on the established path: both experimental
    # alternatives failed complete semantic review and are not promoted.
    if not scope:return None
    ids={h.node.metadata['source_key'] for h in retrieved}
    if len(ids)<2 or (len(question)<55 and len(scope)<2):return None
    if any(h.node.metadata['source_type']=='note' and h.node.metadata['source_id'] in schemas for h in retrieved):return None
    representatives={}
    for h in retrieved:representatives.setdefault(h.node.metadata['source_key'],h)
    scoped={key for s in scope for key in s['matched_sources']}
    full=[]
    # If all explicitly allowed short notes fit, retain their full content.
    if scoped and scoped<=set(representatives):
        for key in representatives:
            if key not in scoped:continue
            hit=representatives[key];meta=hit.node.metadata
            if meta['source_type']!='note':full=[];break
            text=notes[meta['source_id']]['content']
            if not text:full=[];break
            full.append((hit,text,[dict(field='content',char_start=0,char_end_exclusive=len(text),quote=text)],True))
        if sum(len(t)+len(h.node.metadata['title'])+45 for h,t,_,_ in full)>budget:full=[]
    units=full;table_done=set()
    if not full:
        units=[]
        for hit in retrieved:
            meta=hit.node.metadata;key=meta['source_key']
            if key in table_done:continue
            tables=parse_tables(notes[meta['source_id']]['content']) if meta['source_type']=='note' else []
            if tables:
                # Never infer split cells from OCR. Original header and complete
                # rows constitute a table unit; do not clip halfway through it.
                body=notes[meta['source_id']]['content']
                for table in tables:
                    start=table['header_start'];end=table['rows'][-1]['end'];text=body[start:end]
                    units.append((hit,text,[dict(field='content',char_start=start,char_end_exclusive=end,quote=text)],False))
                # Preserve non-table prose from the same saved source too.
                cursor=0
                for a,b in [(t['header_start'],t['rows'][-1]['end']) for t in tables]+[(len(body),len(body))]:
                    raw=body[cursor:a];text=raw.strip();start=cursor+len(raw)-len(raw.lstrip())
                    if text:units.append((hit,text,[dict(field='content',char_start=start,char_end_exclusive=start+len(text),quote=text)],False))
                    cursor=b
                table_done.add(key)
            else:units.append((hit,hit.node.text,[],False))
    packed=[];omitted=[];seen=set();remaining=budget
    for hit,text,spans,complete in units:
        if text in seen:continue
        seen.add(text);meta=hit.node.metadata
        heading=f'[{len(packed)+1}] {meta["title"]} · '+('完整保存正文' if complete else meta['location'])+'\n'
        cost=len(heading+text)+2
        if cost>remaining:
            omitted.append(dict(node_id=hit.node.node_id,reason='whole_evidence_unit_exceeds_remaining_budget',chars=len(text)));continue
        actual_meta=dict(meta)
        if spans:
            actual_meta.update(kind='content',location=f'保存正文 · 字符 {spans[0]["char_start"]}—{spans[-1]["char_end_exclusive"]}')
            actual_meta.pop('chunk_id',None)
        packed.append(dict(index=len(packed)+1,**actual_meta,node_id=(f'content:{meta["source_key"]}:{spans[0]["char_start"]}:{spans[-1]["char_end_exclusive"]}' if spans else hit.node.node_id),retrieval_anchor_node_id=hit.node.node_id,evidence=text,excerpt=text,score=hit.score,
            truncated=False,prompt_block=heading+text,raw_evidence=spans,coverage_complete=complete,
            coverage_notice='完整保存正文；不包括独立OCR。' if complete else '完整证据单元；不代表整份资料。'))
        remaining-=cost
    # Avoid silently narrowing a composite answer to only one source.
    if len({p['source_key'] for p in packed})<2:return None
    return packed,omitted,dict(active=True,whole_scoped_notes=bool(full),budget=budget,remaining=remaining,
        sources=[p['source_key'] for p in packed],policy='whole source units; no partial table/OCR substitution')


class CoverageGeneration:
    """Apply the composite-answer budget only after the coverage pack activates."""
    def __init__(self,base,candidate):self.base=base;self.candidate=candidate
    def __getattr__(self,name):return getattr(self.base,name)
    def create_chat_completion(self,**kw):
        if self.candidate.coverage_trace.get('active'):
            # A general attribution/coverage instruction, independent of domains,
            # particular questions or expected answers. No repair/retry call.
            kw['messages'][0]['content']+=('复合问题请按问题的各部分分别作答。先保留各资料自身陈述，再说明跨来源的匹配或比较；'
                '不得用一份资料的相近描述替代另一份资料的原始目标、条件或标准。'
                '每一项只写必要事实和对应引用，避免重复题目、展开未问属性或末尾重复总结。')
            used=sum(len(self.base.tokenize(m['content'].encode('utf-8'),add_bos=False)) for m in kw['messages'])
            kw['max_tokens']=min(512,max(220,3072-used-128))
            self.candidate.coverage_trace.update(prompt_content_tokens=used,output_token_limit=kw['max_tokens'])
        return self.base.create_chat_completion(**kw)
