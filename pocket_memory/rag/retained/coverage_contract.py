# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Original sentence/layout evidence, before the old fixed anchor cutoff.

Reuses LlamaIndex SentenceSplitter and the local FlashRank scorer. The only
deduplication is original-coordinate overlap, not inferred fact equivalence.
No query generation, generated summaries, expected answers or domain lexicons.
"""
import copy,hashlib,re
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import NodeWithScore
from .context_contract import Original,CONTEXT,OUTPUT,MARGIN

class CoverageContract:
    def __init__(self,owner):
        self.owner=owner
        self.splitter=None
    @staticmethod
    def covered_length(bounds,ranges):
        lo,hi=bounds;merged=[]
        for a,b in sorted((max(lo,a),min(hi,b)) for a,b in ranges if a<hi and b>lo):
            if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],b)
            else:merged.append([a,b])
        return sum(b-a for a,b in merged)
    def lost_complete_lists(self,old,new):
        """Preserve a complete original numbered parent already in evidence.

        A sequential list is an atomic evidence unit. If the new selection
        breaks one that the prior context retained in full, keep that context.
        This is a source-layout invariant, independent of query or test labels.
        """
        lost=[]
        for source_id in {s['source_id'] for s in old}:
            original=Original(self.owner.notes[source_id]['content'])
            before=[(r['char_start'],r['char_end_exclusive']) for s in old if s['source_id']==source_id for r in s.get('raw_evidence',[]) if r['field']=='content']
            after=[(r['char_start'],r['char_end_exclusive']) for s in new if s['source_id']==source_id for r in s.get('raw_evidence',[]) if r['field']=='content']
            starts=original.articles or [0]
            for a,b in zip(starts,starts[1:]+[len(original.body)]):
                text=original.body[a:b];right=b-len(text)+len(text.rstrip())
                left=a+len(text)-len(text.lstrip())
                if self.covered_length((left,right),before)<right-left:continue
                numbers=[int(m.group(1)) for m in re.finditer(r'(?<![\w.])([1-9]\d?)(?:[、．.]|\s+)(?!\d)',text)]
                sequential=any(numbers[i:i+3]==[1,2,3] for i in range(max(0,len(numbers)-2)))
                if sequential and self.covered_length((left,right),after)<right-left:
                    lost.append(dict(source_id=source_id,span=[left,right]))
        return lost
    def prepare(self,question,prepared):
        old,old_messages,trace=prepared
        hits=trace.get('retrieved',[])
        if not old or any(s.get('source_type')!='note' or s.get('kind')!='content' for s in old):return None
        if any(h.get('source_type')!='note' or h.get('kind')!='content' for h in hits):return None
        # Count with the reranker's tokenizer to keep long portal/layout lines
        # searchable. The splitter also handles native Chinese sentence marks.
        if self.splitter is None:
            tokenizer=self.owner.context.reranker.get_ranker().tokenizer
            self.splitter=SentenceSplitter(chunk_size=200,chunk_overlap=40,
                tokenizer=lambda s:tokenizer.encode(s,add_special_tokens=False).ids,
                chunking_tokenizer_fn=lambda s:re.findall(r'[^。！？；]+[。！？；]?',s),
                paragraph_separator='\n\n',secondary_chunking_regex=r'[^。！？；]+[。！？；]?')
        pool={};unmapped=[]
        def retain_fragment(h):
            # A historical extracted fragment may not map uniquely to saved
            # text. Keep its existing identity rather than invent an offset,
            # or let that fragment suppress every verified unit in the query.
            node=self.owner.by_id[h['node_id']]
            unit=self.owner.contract.units([NodeWithScore(node=node,score=h['score'])])[0]
            unit.update(id=unit['node_id'],text=unit['evidence'])
            pool[(h['source_key'],h['node_id'])]=unit
        for h in hits:
            original=self.owner.contract.originals.get(h['source_id'])
            if original is None:original=Original(self.owner.notes[h['source_id']]['content'])
            restored=original.restore(h['text'])
            if restored is None:
                unmapped.append(h['node_id']);retain_fragment(h);continue
            a,b=restored['start'],restored['end'];local=Original(original.body[a:b])
            for text in self.splitter.split_text(local.body):
                span=local.locate(text)
                if span is None:
                    unmapped.append(h['node_id']);retain_fragment(h);continue
                lo,hi=a+span[0],a+span[1]
                parents=[(x,y) for x,y in original.parents(lo) if y<=lo]
                raw=[original.raw(x,y) for x,y in parents]+[original.raw(lo,hi)]
                content='\n'.join(s['quote'] for s in raw)
                identity=(h['source_key'],lo,hi)
                node_id='sentence:'+hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
                if identity in pool:
                    pool[identity]['anchors'].append(h['node_id']);continue
                item={k:v for k,v in h.items() if k not in ('text','node_id','score')}
                item.update(id=node_id,text=content,node_id=node_id,span=[lo,hi],parents=parents,
                    raw_evidence=raw,evidence=content,anchors=[h['node_id']],chunk_ids=[],
                    source_verification='unique_original_span',complete_sentence=lo in original.ends and hi in original.ends,
                    truncated=False,coverage_complete=False)
                pool[identity]=item
        if not pool:return None
        scored=self.owner.context.reranker.rerank(question,list(pool.values()))
        selected=[];packed=[];decisions=[]
        for item in scored:
            bounds=item.get('span')
            covered=0
            same=[s['span'] for s in selected if s['source_key']==item['source_key'] and s.get('span')]
            if bounds:
                lo,hi=bounds
                covered=self.covered_length(bounds,same)
            if bounds and covered>=.75*(hi-lo):
                decisions.append(dict(id=item['id'],reason='original_coordinate_overlap'));continue
            proposed=self.owner.contract.bundles(selected+[item])
            tokens=self.owner.contract.count(self.owner.contract.messages(question,proposed))
            if tokens+OUTPUT+MARGIN>CONTEXT:
                decisions.append(dict(id=item['id'],reason='whole_original_unit_budget'));continue
            selected.append(item);packed=proposed
            if len(selected)==8:break
        if not packed:return None
        lost=self.lost_complete_lists(old,packed)
        if lost:
            kept=copy.deepcopy(trace)
            kept['coverage_selection']=dict(policy='retain_prior_complete_numbered_parent',protected_parents=lost)
            return old,old_messages,kept
        messages=self.owner.contract.messages(question,packed)
        tokens=self.owner.contract.count(messages)
        assert tokens+OUTPUT+MARGIN<=CONTEXT
        result=copy.deepcopy(trace)
        result.update(messages=messages,standard_route='ranked_original_sentence_coverage',
            evidence_chars=sum(len(s['prompt_block']) for s in packed),
            budget=dict(context=CONTEXT,reserved_output=OUTPUT,margin=MARGIN,prompt_tokens=tokens),
            coverage_selection=dict(policy='all_retrieved_anchors_split_before_selection',
                prior_anchor_count=len(hits),unique_units=len(pool),selected_units=len(selected),
                rankings=[dict(id=s['id'],score=s['score'],source_key=s['source_key'],span=s.get('span'),selected=s in selected) for s in scored],
                retained_unmapped_fragments=unmapped,decisions=decisions,components=['LlamaIndex SentenceSplitter','FlashRank','original coordinate overlap removal','native chat token budget']))
        return packed,messages,result
