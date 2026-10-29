# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Retrieve small anchors, supply original complete units under the same budget.

Uses the pinned LlamaIndex RRF + FlashRank retrieval. There are no case names,
business constants, expected answers, numeric corrections or generated plans.
"""
import copy,hashlib,re
from .context_contract import Original

class OriginalUnits:
    def __init__(self,owner):self.owner=owner
    def block(self,prior,ranges,kind):
        s=copy.deepcopy(prior);sid=s['source_id'];body=self.owner.notes[sid]['content']
        # These describe the old bundle, not the newly restored original unit.
        for key in ('span','parents','anchors','chunk_ids','chunk_id','chunk_order','original_locations','article_start','citation_granularity'):
            s.pop(key,None)
        raw=[dict(field='content',char_start=a,char_end_exclusive=b,quote=body[a:b]) for a,b in ranges]
        evidence='\n'.join(x['quote'] for x in raw)
        eid='original-unit:'+hashlib.sha256(repr((s['source_key'],ranges)).encode()).hexdigest()[:24]
        s.update(index=1,evidence=evidence,excerpt=evidence,raw_evidence=raw,source_field='content',
            evidence_id=eid,node_id=eid,source_verification='unique_original_span',coverage_complete=False,
            location='保存正文 · '+','.join(f'{a}–{b}' for a,b in ranges),unit_policy=kind)
        s['prompt_block']=f'[1] {s["title"]} · {s["location"]}\n'+evidence
        return s
    def fit(self,question,sources):
        msgs=self.owner.contract.messages(question,sources)
        return msgs if self.owner.contract.count(msgs)+512+128<=3072 else None
    def prepare(self,question,prior_hits,prepared):
        old,messages,trace=prepared
        # Restrict this precision adapter to one actual note. Cross-source and
        # table/schema paths retain their separately verified source contract.
        if not old or len({s['source_key'] for s in old})!=1:return None
        if any(s.get('source_type')!='note' or s.get('kind')!='content' for s in old):return None
        sid=old[0]['source_id'];body=self.owner.notes[sid]['content'];original=Original(body)
        if len(body)<2400:return None
        hits=trace['retrieved']
        if not hits:return None
        # Parent-document retrieval: score the short anchor, then recover its
        # explicitly delimited parent, not just the first 12 ranked fragments.
        anchor=original.locate(hits[0]['text'])
        if original.articles and anchor:
            a=original.article(anchor[0]);b=next((x for x in original.articles if x>a),len(body))
            parent=body[a:b]
            numbered=re.findall(r'(?<![\w.])([1-9]\d?)[、.](?!\d)',parent)
            covered='\n'.join(s['evidence'] for s in old if s.get('article_start')==a)
            # A list is kept as one original unit. No fabricated step endings,
            # stitching between separate articles, or duplicate alternatives.
            if len(numbered)>=3 and ''.join(parent.split()) not in ''.join(covered.split()):
                s=self.block(old[0],[(a,b)],'complete_numbered_parent')
                new_messages=self.fit(question,[s])
                if new_messages is not None:
                    return [s],new_messages,dict(policy=s['unit_policy'],parent_range=[a,b],
                        prior_trace=trace,restored_character_count=b-a,components=['LlamaIndex RRF','FlashRank anchor ranking','original parent recovery'])
        # Numeric/date fact questions benefit from original-sentence precision.
        # Do not apply to comparisons, multi-question synthesis or lists.
        direct=bool(re.search(r'多少|多久|几(?:年|天|月|个)|何时|哪一年|多长|多大',question))
        compound=bool(re.search(r'分别|各自|相比|比较|区别|以及|和.*(?:多少|多久)|[；;]',question))
        years=set(re.findall(r'(?:19|20)\d{2}年',question))
        # Compression needs a literal temporal constraint. An undated question
        # can refer to different stages/editions; rank1 is not disambiguation.
        if not direct or compound or len(years)!=1:return None
        pool={}
        for s in old:
            for sp in s.get('raw_evidence',[]):
                a,b=sp['char_start'],sp['char_end_exclusive']
                for m in re.finditer(r'[^。！？；\r\n]+[。！？；]?',body[a:b]):
                    lo,hi=a+m.start(),a+m.end();sentence=body[lo:hi]
                    if len(sentence.strip())<15:continue
                    parents=[(x,y) for x,y in original.parents(lo) if y<=lo]
                    text='\n'.join(body[x:y] for x,y in parents)+ '\n'+sentence
                    if not any(year in text for year in years):continue
                    pool[(lo,hi)]=dict(id=f'{lo}:{hi}',text=text,ranges=parents+[(lo,hi)])
        if not pool:return None
        ranked=self.owner.context.reranker.rerank(question,list(pool.values()))
        # Reranking estimates relevance; rank 1 is not a semantic proof that
        # the other metrics are irrelevant. Retain several complete original
        # sentences in this already bounded time scope. No inferred numbers,
        # per-question conditions, or generated summaries enter the evidence.
        selected=ranked[:3]
        ranges=sorted(set(tuple(r) for item in selected for r in item['ranges']))
        merged=[]
        for lo,hi in ranges:
            if merged and lo<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],hi)
            else:merged.append([lo,hi])
        s=self.block(old[0],merged,'ranked_original_sentences')
        new_messages=self.fit(question,[s])
        if new_messages is None:return None
        return [s],new_messages,dict(policy=s['unit_policy'],prior_trace=trace,
            selected_ranges=merged,selected_sentence_ids=[x['id'] for x in selected],
            sentence_rankings=[dict(id=x['id'],score=x['score']) for x in ranked],
            components=['FlashRank complete-sentence ranking','top-3 original sentence evidence','original heading retention'])
