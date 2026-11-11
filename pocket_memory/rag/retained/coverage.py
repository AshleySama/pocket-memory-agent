# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Whole verified table units and scoped short-note coverage, without gold facts."""
from pocket_memory.rag.performance import completion
import hashlib
import re
from .table_contract import parse_tables


def _is_ocr(hit):
    meta=hit.node.metadata
    return meta.get('kind')=='ocr' or meta.get('source_field')=='ocr_text' or any(
        s.get('field')=='ocr_text' for s in meta.get('raw_evidence',[]))


def _ocr_unit(hit, originals):
    """Bind only an already-retrieved chunk to the saved OCR field.

    Match the same unique whitespace-preserving coordinates used by Bridge.
    An explicitly bad coordinate is an error, never repaired by another match.
    This verifies the saved OCR text, not its accuracy against the original image.
    """
    meta=hit.node.metadata
    body=originals.get(meta.get('source_id')) if originals is not None else None
    if meta.get('source_type')!='note' or not isinstance(body,str):
        return None,'ocr_original_field_unavailable'
    revision=hashlib.sha256(body.encode('utf-8')).hexdigest()
    if meta.get('source_revision') not in (None,revision):
        return None,'ocr_original_revision_mismatch'
    compact=lambda text:re.sub(r'\s+','',text)
    spans=meta.get('raw_evidence')
    if spans:
        verified=[]
        for raw in spans:
            a,b,quote=raw.get('char_start'),raw.get('char_end_exclusive'),raw.get('quote')
            if not (raw.get('field')=='ocr_text' and type(a) is int and type(b) is int
                    and 0<=a<b<=len(body) and isinstance(quote,str) and body[a:b]==quote):
                return None,'ocr_original_span_mismatch'
            if raw.get('source_revision') not in (None,revision):
                return None,'ocr_original_revision_mismatch'
            verified.append(dict(field='ocr_text',char_start=a,char_end_exclusive=b,quote=quote))
        if compact(hit.node.text)!=compact(''.join(s['quote'] for s in verified)):
            return None,'ocr_chunk_span_text_mismatch'
        spans=verified
    else:
        needle=compact(hit.node.text);clean=compact(body)
        a=clean.find(needle) if needle else -1
        if a<0:return None,'ocr_chunk_not_in_saved_field'
        if clean.find(needle,a+1)>=0:return None,'ocr_chunk_location_ambiguous'
        positions=[i for i,c in enumerate(body) if not c.isspace()]
        start,end=positions[a],positions[a+len(needle)-1]+1
        spans=[dict(field='ocr_text',char_start=start,char_end_exclusive=end,quote=body[start:end])]
    spans=[dict(s,source_revision=revision) for s in spans]
    return (hit,'\n'.join(s['quote'] for s in spans),spans,False),None


def coverage_pack(question,retrieved,notes,schemas,scope,budget=1500,*,ocr_originals=None,audit=None):
    # Unscoped synthesis remains on the established path: both experimental
    # alternatives failed complete semantic review and are not promoted.
    if not scope:return None
    ids={h.node.metadata['source_key'] for h in retrieved}
    if len(ids)<2 or (len(question)<55 and len(scope)<2):return None
    if any(h.node.metadata['source_type']=='note' and h.node.metadata['source_id'] in schemas for h in retrieved):return None
    representatives={}
    for h in retrieved:representatives.setdefault(h.node.metadata['source_key'],h)
    scoped={key for s in scope for key in s['matched_sources']}
    ocr_units=[];ocr_omitted=[]
    for hit in retrieved:
        if not _is_ocr(hit):continue
        unit,why=_ocr_unit(hit,ocr_originals)
        if why:
            ocr_omitted.append(dict(node_id=hit.node.node_id,source_key=hit.node.metadata['source_key'],
                                    field='ocr_text',reason=why))
        else:ocr_units.append(unit)
    has_ocr=bool(ocr_units or ocr_omitted)
    if audit is not None and has_ocr:
        audit.update(retrieved=len(ocr_units)+len(ocr_omitted),verified=len(ocr_units),
                     original_field='ocr_text',omitted=ocr_omitted)
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
        # The same source's OCR is not replaced by its saved body. Test the
        # combined reservation before taking the full-body shortcut.
        if sum(len(t)+len(h.node.metadata['title'])+45 for h,t,_,_ in full+ocr_units)>budget:full=[]
    units=full;table_done=set()
    if full:
        units=full+ocr_units
    else:
        units=[]
        ocr_by_node={id(unit[0]):unit for unit in ocr_units}
        for hit in retrieved:
            meta=hit.node.metadata;key=meta['source_key']
            # Dispatch by original field BEFORE table_done/body parsing. OCR
            # cannot become table rows from this note's unrelated saved body.
            if _is_ocr(hit):
                if id(hit) in ocr_by_node:units.append(ocr_by_node[id(hit)])
                continue
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
    packed=[];omitted=list(ocr_omitted);seen=set();remaining=budget
    for hit,text,spans,complete in units:
        meta=hit.node.metadata
        is_ocr=bool(spans) and all(s['field']=='ocr_text' for s in spans)
        identity=(meta['source_key'],'ocr_text',tuple((s['char_start'],s['char_end_exclusive'],s['source_revision']) for s in spans)) if is_ocr else text
        if identity in seen:continue
        seen.add(identity)
        location=('OCR原字段 · 字符 '+','.join(f'{s["char_start"]}—{s["char_end_exclusive"]}' for s in spans)) if is_ocr else meta['location']
        heading=f'[{len(packed)+1}] {meta["title"]} · '+('完整保存正文' if complete else location)+'\n'
        cost=len(heading+text)+2
        if cost>remaining:
            omission=dict(node_id=hit.node.node_id,reason='whole_evidence_unit_exceeds_remaining_budget',chars=len(text))
            if is_ocr:omission.update(source_key=meta['source_key'],field='ocr_text')
            omitted.append(omission);continue
        actual_meta=dict(meta)
        if is_ocr:
            for name in ('index','node_id','evidence','excerpt','score','truncated','prompt_block','raw_evidence','coverage_complete','coverage_notice','retrieval_anchor_node_id'):
                actual_meta.pop(name,None)
            note=notes.get(meta['source_id'],{})
            actual_meta.update(kind='ocr',source_field='ocr_text',location=location,
                source_verification='saved_original_spans',source_revision=spans[0]['source_revision'],
                parent_note_id=note.get('parent_note_id'),tab_name=note.get('tab_name'))
            actual_meta.pop('chunk_id',None)
            node_id='ocr:'+hashlib.sha256(repr(identity).encode('utf-8')).hexdigest()[:24]
        elif spans:
            # A retrieved OCR chunk may be the representative for a short
            # note's body. Do not carry its OCR identity onto that body block.
            if _is_ocr(hit):
                for name in ('index','node_id','evidence','excerpt','score','truncated','prompt_block','raw_evidence','coverage_complete','coverage_notice','retrieval_anchor_node_id','source_revision'):
                    actual_meta.pop(name,None)
                actual_meta.update(source_field='content',source_verification='saved_original_spans')
            actual_meta.update(kind='content',location=f'保存正文 · 字符 {spans[0]["char_start"]}—{spans[-1]["char_end_exclusive"]}')
            actual_meta.pop('chunk_id',None)
        if not is_ocr:node_id=f'content:{meta["source_key"]}:{spans[0]["char_start"]}:{spans[-1]["char_end_exclusive"]}' if spans else hit.node.node_id
        packed.append(dict(index=len(packed)+1,**actual_meta,node_id=node_id,retrieval_anchor_node_id=hit.node.node_id,evidence=text,excerpt=text,score=hit.score,
            truncated=False,prompt_block=heading+text,raw_evidence=spans,coverage_complete=complete,
            coverage_notice=('已核对保存OCR字段；不代表原图识别正确或整份资料完整。' if is_ocr else
                             '完整保存正文；不包括独立OCR。' if complete else '完整证据单元；不代表整份资料。')))
        remaining-=cost
    if audit is not None and has_ocr:
        audit.update(packed=sum(s.get('source_field')=='ocr_text' for s in packed),
                     omitted=[o for o in omitted if o.get('field')=='ocr_text' or o['node_id'] in {u[0].node.node_id for u in ocr_units}])
    # Avoid silently narrowing a composite answer to only one source.
    if len({p['source_key'] for p in packed})<2:return None
    trace=dict(active=True,whole_scoped_notes=bool(full),budget=budget,remaining=remaining,
        sources=[p['source_key'] for p in packed],policy='whole source units; no partial table/OCR substitution')
    if has_ocr:trace['ocr_adapter']=dict(audit or {},verification='exact_saved_ocr_only')
    return packed,omitted,trace


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
