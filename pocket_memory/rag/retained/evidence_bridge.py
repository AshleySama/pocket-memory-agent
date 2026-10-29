# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Canonical original evidence and bounded multi-clause retrieval; no gold input."""
import copy
import hashlib
import re
import sqlite3
from pathlib import Path
from lxml import html as lxml_html
from llama_index.core.schema import TextNode,NodeWithScore
from llama_index.core import VectorStoreIndex
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.core.indices.vector_store.retrievers import VectorIndexRetriever
from llama_index.retrievers.bm25 import BM25Retriever
from .context_contract import compact

def display(text):
    if not re.search(r'</?(?:p|div|span|strong|em|b|i|br)\b',text,re.I):return text
    try:return lxml_html.fragment_fromstring(text,create_parent='div').text_content()
    except (ValueError,lxml_html.etree.ParserError):return text

def facets(question):
    # Syntax only: no domain lexicon, case IDs or known answers.
    text=re.sub(r'[《【][^》】]+[》】]',' ',question)
    parts=[p.strip() for p in re.split(r'[。？?；;，,：:]',text) if len(p.strip())>=6]
    extra=[]
    for part in parts:
        pieces=re.split(r'以及|和|与|、',part)
        if len(pieces)>1 and all(len(x.strip())>=4 for x in pieces):extra.extend(x.strip() for x in pieces)
    return list(dict.fromkeys(p for p in extra+parts if not re.match(r'不要|不采用|不使用|排除',p)))[:6]

def span(field,body,a,b):return dict(field=field,char_start=a,char_end_exclusive=b,quote=body[a:b])

def html_tables(body):
    """lxml parses cells; only simple unmerged tables have exact row mapping."""
    out=[]
    for t in re.finditer(r'<table\b[^>]*>[\s\S]*?</table\s*>',body,re.I):
        try:root=lxml_html.fromstring(t.group())
        except (ValueError,lxml_html.etree.ParserError):continue
        if root.xpath('.//table') or root.xpath('.//*[@rowspan and @rowspan!="1"]|.//*[@colspan and @colspan!="1"]'):continue
        matches=list(re.finditer(r'<tr\b[^>]*>[\s\S]*?</tr\s*>',t.group(),re.I))
        rows=root.xpath('.//tr')
        if len(matches)!=len(rows) or len(rows)<2:continue
        cells=[[''.join(c.itertext()).strip() for c in r.xpath('./th|./td')] for r in rows]
        headers=cells[0]
        if len(headers)<2 or any(not h for h in headers) or len(set(headers))!=len(headers):continue
        if any(len(x)!=len(headers) for x in cells[1:]):continue
        hs=span('content',body,t.start()+matches[0].start(),t.start()+matches[0].end())
        for vals,m in zip(cells[1:],matches[1:]):
            rs=span('content',body,t.start()+m.start(),t.start()+m.end())
            out.append(dict(headers=headers,values=vals,raw_evidence=[hs,rs],table_range=(t.start(),t.end())))
    return out

class Bridge:
    def __init__(self,owner,database):
        self.owner=owner;self.cache={};self.retrievers={}
        with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as con:
            self.ocr={i:t or '' for i,t in con.execute('SELECT id,ocr_text FROM notes')}

    def units(self,key):
        if key in self.cache:return self.cache[key]
        units=[copy.deepcopy(n) for n in self.owner.units(key)]
        if not key.startswith('note:'):
            # Existing document fragments keep chunk provenance, never invent
            # whole-file or note offsets for them.
            for n in units:n.metadata['source_verification']='saved_document_chunk'
            self.cache[key]=units;return units
        sid=int(key.split(':')[1]);note=self.owner.notes[sid];body=note['content'];ocr=self.ocr.get(sid,'')
        tables=html_tables(body)
        table_ranges={tuple(t['table_range']) for t in tables}
        units=[n for n in units if not any(a<=n.metadata.get('raw_evidence',[{}])[0].get('char_start',-1)<b for a,b in table_ranges)]
        for t in tables:
            def cell(v):return v.replace('|','\\|').replace('\n',' ')
            text='；'.join(f'{h}：{v}' for h,v in zip(t['headers'],t['values']))
            evidence='| '+' | '.join(map(cell,t['headers']))+' |\n| '+' | '.join(map(cell,t['values']))+' |'
            node=self.owner.node(key,note,text,evidence,t['raw_evidence'],'table_row',t['raw_evidence'][-1]['char_start'])
            node.metadata.update(parsed_cells=dict(zip(t['headers'],t['values'])),source_format='html_table')
            units.append(node)
        has_table=any(n.metadata.get('kind')=='table_row' for n in units)
        for n in units:
            m=n.metadata;m['source_field']='content';m['source_verification']='saved_original_spans'
            # Prefer the complete matching paragraph, even in a short note.
            # Expanding every anchor to the whole note erased its relevance and
            # mixed independent background, targets and conditions together.
            if m.get('kind')=='content' and m.get('paragraph_range'):
                a,b=m['paragraph_range']
                if len(display(body[a:b]))<=600:
                    m['window']=body[a:b];m['raw_evidence']=[span('content',body,a,b)]
            elif m.get('kind')=='content' and not has_table and len(body)<=600:
                m['window']=body;m['raw_evidence']=[span('content',body,0,len(body))]
            # Display only removes HTML presentation markup. Original bytes and
            # offsets remain separately attached, never rewritten in the source.
            if m.get('kind')=='content':m['window']=display(m['window'])
            m['parent_note_id']=note.get('parent_note_id');m['tab_name']=note.get('tab_name')
        # OCR is a separate original field. Keep it available, never call it
        # equivalent to saved prose merely because both come from one note.
        for old in self.owner.nodes:
            m=old.metadata
            if m['source_key']!=key or m.get('kind')!='ocr':continue
            needle=compact(old.text);clean=compact(ocr);a=clean.find(needle)
            if not needle or a<0 or clean.find(needle,a+1)>=0:continue
            pos=[i for i,c in enumerate(ocr) if not c.isspace()];start,end=pos[a],pos[a+len(needle)-1]+1
            # Exact duplicate only; source modality preference is not equivalence.
            if needle in compact(body):continue
            node=copy.deepcopy(old)
            node.metadata.update(window=ocr[start:end],raw_evidence=[span('ocr_text',ocr,start,end)],source_field='ocr_text',
                source_verification='saved_original_spans',parent_note_id=note.get('parent_note_id'),tab_name=note.get('tab_name'),
                supplement_only=has_table)
            units.append(node)
        self.cache[key]=units;return units

    def retrieve(self,keys,query,kind=None):
        sig=(tuple(sorted(keys)),kind)
        if sig not in self.retrievers:
            nodes=[n for k in sig[0] for n in self.units(k) if kind is None or n.metadata.get('kind')==kind]
            if not nodes:return []
            index=VectorStoreIndex(nodes,embed_model=self.owner.embed,show_progress=False)
            dense=VectorIndexRetriever(index=index,similarity_top_k=min(18,len(nodes)))
            sparse=BM25Retriever(nodes=nodes,similarity_top_k=min(18,len(nodes)),token_pattern=r'[a-zA-Z0-9_]+|[\u4e00-\u9fff]',skip_stemming=True,verbose=False)
            self.retrievers[sig]=QueryFusionRetriever([dense,sparse],llm=self.owner.llm,mode='reciprocal_rerank',similarity_top_k=min(18,len(nodes)),num_queries=1,use_async=False)
        return self.retrievers[sig].retrieve(query)

    def block(self,node):
        m=copy.deepcopy(node.metadata);raw=m.get('raw_evidence',[]);text=m.pop('window',node.text)
        for s in raw:
            body=self.owner.notes[m['source_id']]['content'] if s['field']=='content' else self.ocr[m['source_id']]
            assert body[s['char_start']:s['char_end_exclusive']]==s['quote'],'Unverified original span'
        kind=m.get('kind');identity=(m['source_key'],m.get('source_field'),[(s['field'],s['char_start'],s['char_end_exclusive']) for s in raw],node.node_id if not raw else None)
        eid='bridge:'+hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
        if raw:
            for k in ('chunk_id','chunk_order'):m.pop(k,None)
        location=(('OCR原字段' if m.get('source_field')=='ocr_text' else '保存正文')+' · '+
            ','.join(str(s['char_start'])+'–'+str(s['char_end_exclusive']) for s in raw)) if raw else m.get('location','保存文档切片')
        m.update(evidence=text,excerpt=text,evidence_id=eid,node_id=eid,coverage_complete=False,truncated=False,
            location=location,
            original_retrieval_node=node.node_id)
        return m

    def prepare(self,question,prior):
        allowed,scope=self.owner.scope(question);allowed_ids={n.node_id for n in allowed};keys={n.metadata['source_key'] for n in allowed}
        if not allowed:return prior
        queries=list(dict.fromkeys([question]+facets(question)))
        scoped={k for s in scope for k in s.get('matched_sources',[])}
        # Reuse the existing first-stage index for every clause, then canonical
        # units from its candidate sources. No answer-aware source selection.
        key_pool=set(scoped);first_stage=[]
        for q in queries:
            hits,_=self.owner.legacy.retrieve(q)
            hits=[h for h in hits if h.node.node_id in allowed_ids]
            key_pool.update(h.node.metadata['source_key'] for h in hits[:6]);first_stage.extend(h.node.node_id for h in hits[:6])
        keys=keys.intersection(key_pool)
        groups=[];rankings=[];pool={}
        for q in queries:
            hits=self.retrieve(keys,q)
            # Retrieve rows in their own channel before any mixed-granularity
            # cutoff. Otherwise a retrieved OCR table could survive while all
            # verified rows from that same table disappear behind long prose.
            row_hits=self.retrieve(keys,q,'table_row')
            ids={h.node.node_id for h in hits}
            hits=hits+[h for h in row_hits if h.node.node_id not in ids]
            rankings.append(dict(query=q,hits=[dict(node_id=h.node.node_id,source_key=h.node.metadata['source_key'],kind=h.node.metadata.get('kind'),score=h.score) for h in hits]))
            primary=[h for h in hits if not h.node.metadata.get('supplement_only')]
            secondary=[h for h in hits if h.node.metadata.get('supplement_only')]
            # Reserve both prose and row evidence when each is retrieved in the
            # leading candidates; do not let OCR duplicates take their slots.
            reserve=[]
            for kind in ('content','table_row','ocr','document'):
                hit=next((h for h in primary[:6] if h.node.metadata.get('kind')==kind),None)
                if hit:reserve.append(hit)
            order=reserve+[h for h in primary if h not in reserve]+secondary
            groups.append(order)
            for h in hits:
                block=self.block(h.node)
                pool.setdefault(block['evidence_id'],(h,block))
        # All clauses broaden recall, but generic fragments cannot independently
        # force unrelated documents into the answer. Rank complete canonical
        # units against the full question, then preserve explicit source scope.
        scores=self.owner.context.reranker.rerank(question,[dict(id=k,text=v[1]['title']+'\n'+v[1]['evidence']) for k,v in pool.items()])
        ranked=[pool[s['id']][0] for s in scores if not pool[s['id']][0].node.metadata.get('supplement_only')]
        supplemental=[pool[s['id']][0] for s in scores if pool[s['id']][0].node.metadata.get('supplement_only')]
        # A paragraph-trained cross encoder can rank every short table row below
        # verbose prose. Keep a separate row channel, fused across clauses, rather
        # than placing unlike granularities behind one global top-k cutoff.
        row_votes={};row_nodes={}
        old_rows=[]
        for source in prior.get('sources',[]):
            if source.get('kind')!='table_row':continue
            for node in self.units(source['source_key']):
                if node.metadata.get('kind')!='table_row':continue
                raw=node.metadata.get('raw_evidence',[])
                if raw and all(s in source.get('raw_evidence',[]) for s in raw):
                    old_rows.append(NodeWithScore(node=node,score=source.get('score')));break
        # Previously selected exact rows are preservation anchors, not gold.
        # New retrieval may supplement them but may not silently replace them.
        main_rows=[h for h in groups[0] if h.node.metadata.get('kind')=='table_row'] if groups else []
        table_keys={h.node.metadata['source_key'] for h in main_rows[:2]+old_rows}|scoped
        for group in groups:
            table_hits=[h for h in group if h.node.metadata.get('kind')=='table_row' and h.node.metadata['source_key'] in table_keys]
            for rank,h in enumerate(table_hits[:2]):
                key=self.block(h.node)['evidence_id'];row_nodes[key]=h
                row_votes[key]=row_votes.get(key,0)+1/(60+rank)
        row_order=list(old_rows)
        # Do not pad an already resolved row set with unrelated records merely
        # to fill a top-k quota. New row retrieval is for unresolved table scope.
        if not row_order:
            for k in sorted(row_votes,key=lambda k:-row_votes[k]):row_order.append(row_nodes[k])
            row_order=row_order[:4]
        prose=[h for h in ranked if h.node.metadata.get('kind')!='table_row'
            and len(self.block(h.node)['evidence'])>=40]
        reserve=[]
        for key in sorted(scoped):
            choices=[h for h in row_order+prose if h.node.metadata['source_key']==key]
            if choices:reserve.append(choices[0])
        prose_order=[];prose_seen=set()
        for h in prose:
            key=h.node.metadata['source_key']
            if key not in prose_seen:
                # The reranker chooses source relevance; the original full-query
                # anchor chooses its parent. Do not replace a directly matching
                # paragraph with a verbose introduction from the same document.
                anchor=next((a for a in groups[0] if a.node.metadata['source_key']==key
                    and a.node.metadata.get('kind')==h.node.metadata.get('kind')
                    and len(self.block(a.node)['evidence'])>=40),h)
                prose_order.append(anchor);prose_seen.add(key)
                # Keep a second independently retrieved parent when full-query
                # anchors and reranking disagree. Never collapse an entire
                # document to one introduction or expand it into all paragraphs.
                eid=self.block(anchor.node)['evidence_id']
                alternate=next((a for a in [h]+groups[0] if a.node.metadata['source_key']==key
                    and a.node.metadata.get('kind')==h.node.metadata.get('kind')
                    and self.block(a.node)['evidence_id']!=eid
                    and len(self.block(a.node)['evidence'])>=40),None)
                if alternate:prose_order.append(alternate)
            if len(prose_seen)==max(2,len(scoped)):break
        # Scope reservation must use the same parent selection, not reintroduce
        # the rejected introductory paragraph as a second source fragment.
        reserve=[next((h for h in prose_order if h.node.metadata['source_key']==r.node.metadata['source_key']),r)
            if r.node.metadata.get('kind')!='table_row' else r for r in reserve]
        ordered=reserve+prose_order+row_order
        # Unmapped OCR remains eligible via its own channel; do not mix an OCR
        # table fragment with selected verified rows of that same source.
        row_sources={h.node.metadata['source_key'] for h in row_order}
        ordered += [h for h in supplemental if h.node.metadata['source_key'] not in row_sources][:1]
        packed=[];seen=set();omitted=[]
        for h in ordered:
            s=self.block(h.node)
            if s['evidence_id'] in seen:continue
            seen.add(s['evidence_id'])
            if any(s['source_key']==old['source_key'] and s.get('source_field')==old.get('source_field') and compact(s['evidence']) in compact(old['evidence']) for old in packed):continue
            s['index']=len(packed)+1;s['prompt_block']=f'[{s["index"]}] {s["title"]} · {s["location"]}\n'+s['evidence']
            used=self.owner.contract.count(self.owner.contract.messages(question,packed+[s]))
            # Both retained answer protocols get adequate output space; entire
            # canonical units are omitted explicitly, never sliced mid-row.
            if used+768+128>3072:
                omitted.append(dict(evidence_id=s['evidence_id'],source_key=s['source_key'],reason='whole_unit_token_budget'));continue
            packed.append(s)
        self.owner.cache.commit()
        trace=dict(version='canonical-prose-row-ocr-v2',queries=queries,first_stage_nodes=list(dict.fromkeys(first_stage)),
            rankings=rankings,source_keys=sorted(keys),selected=[dict(evidence_id=s['evidence_id'],source_key=s['source_key'],kind=s.get('kind'),field=s.get('source_field'),raw_evidence=s.get('raw_evidence',[])) for s in packed],
            omitted=omitted,coverage_claim='retrieval allocation only, not semantic completeness',
            canonical_reranking=scores,
            channel_allocation=dict(table_row_ids=[h.node.node_id for h in row_order],prose_ids=[h.node.node_id for h in prose_order],
                retained_prior_row_ids=[h.node.node_id for h in old_rows]),
            components=['LlamaIndex QueryFusionRetriever RRF','VectorIndexRetriever','BM25Retriever','FlashRank','original-span parent retrieval','lxml HTML table parser'])
        result=dict(prior,sources=packed,evidence_bridge=trace,scope=scope)
        if prior['route']=='standard_retrieval':result.update(retrieved=rankings,omitted=omitted,queries=queries,components=trace['components'])
        return result
