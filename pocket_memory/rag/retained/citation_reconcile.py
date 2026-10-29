# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Conservative exact-excerpt citation reconciliation; no model or corpus lookup.

This is deliberately narrower than semantic attribution. Punctuation/layout
normalization is allowed, but paraphrase, anaphora, numeric conversion and
cross-span word coverage are not. Unknown responses remain byte-for-byte v4.
"""
import copy, re, unicodedata

VERSION='exact-excerpt-citation-v1'
CITE=re.compile(r'\[\d+\]')
REFS=re.compile(r'\[\d+\](?:[ \t]*\[\d+\])*')
FOOTER=re.compile(r'^\s*(?:支持资料编号|支持来源|引用来源|来源编号)\s*[:：]')
GLOSSARY=re.compile(r'^\s*[A-Za-z][A-Za-z -]{2,}\s*[（(][^()（）]+[）)]\s*[。.]?\s*$')

def normalized(text):
    """Retain a character map to the original quote, including decimals/signs."""
    out=[]; positions=[]
    excluded=set()
    for m in CITE.finditer(text): excluded.update(range(m.start(),m.end()))
    for i,ch in enumerate(text):
        if i in excluded:continue
        for c in unicodedata.normalize('NFKC',ch):
            keep=c.isalnum() or c in '%/+-=_' or (c=='.' and i>0 and i+1<len(text) and text[i-1].isdigit() and text[i+1].isdigit())
            if keep:out.append(c);positions.append(i)
    return ''.join(out),positions

def body(text):
    text=CITE.sub('',text)
    return re.sub(r'^\s*(?:[-*•]\s+|\d+[.)、]\s+)','',text).strip()

def safe_boundary(quote,start,end,claim,glossary):
    left=quote[:start].rstrip();right=quote[end:]
    line_start=quote.rfind('\n',0,start)+1
    if re.match(r'\s*(?:标题\s*[:：]|#{1,6}\s+)',quote[line_start:]):return False
    if glossary:
        # A bilingual dictionary record, not a claim about an actor or amount.
        tail=re.split(r'[。！？;；\n]',left)[-1]
        if re.search(r'不|未|并非|不是|\b(?:not|never|wrong)\b',tail,re.I) or right.lstrip().startswith(('?','？')):return False
        return (not left or not left[-1].isascii() or not left[-1].isalnum()) and (not right or not right[0].isascii() or not right[0].isalnum())
    left_ok=not left or left[-1] in '。！？;；\n'
    # OCR/PDF exports can repeat a short heading directly before the sentence.
    # Accept only a literal duplicate after a section marker, not coreference.
    if not left_ok:
        for k in range(4,min(17,len(claim))):
            prefix=claim[:k]
            if left.endswith(prefix):
                before=left[:-k].rstrip()
                if not before or before[-1] in '。！？;；\n' or re.search(r'\d+(?:\.\d+){2,}\.\s*$',before):
                    left_ok=True;break
    # A question is not an assertion. A comma can introduce an unconsumed
    # condition/exception, so only closed statements or explicit page breaks
    # qualify. A bare numeric footnote is not itself an end-of-statement proof.
    right_ok=bool(re.match(r'\s*(?:[。！]|\d+\s*\n)',right))
    return left_ok and right_ok

def certificates(text,sources):
    clean=body(text);needle,_=normalized(clean);glossary=bool(GLOSSARY.fullmatch(clean))
    if len(needle)<12 and not glossary:return []
    found=[]
    for source in sources:
        if source.get('truncated') or source.get('kind') in ('title','heading','table_row'):continue
        for span in source.get('raw_evidence',[]):
            quote=span.get('quote','')
            if not quote or span.get('char_end_exclusive',0)-span.get('char_start',0)!=len(quote):continue
            hay,mapping=normalized(quote);at=hay.find(needle)
            while at>=0:
                start=mapping[at];end=mapping[at+len(needle)-1]+1
                if safe_boundary(quote,start,end,clean,glossary):
                    found.append(dict(provided_index=source['index'],source_id=source.get('source_id'),node_id=source.get('node_id'),
                        field=span.get('field'),char_start=span['char_start']+start,char_end_exclusive=span['char_start']+end,
                        quote=quote[start:end],method='exact_bilingual_record' if glossary else 'exact_continuous_statement'))
                    break
                at=hay.find(needle,at+1)
    return found

def reconcile(response,apply_guard):
    original=copy.deepcopy(response);trace=response.get('trace',{})
    raw=trace.get('answer_before_citation_guard')
    if not raw or trace.get('finish_reason','stop')!='stop':return original
    sources=response.get('provided_sources',[]);available={s['index'] for s in sources}
    invalid=set(int(m[0][1:-1]) for m in CITE.finditer(raw))-available
    edits=[];diagnosis=[]
    if invalid:
        # Entire original answer must be one directly supported statement.
        # No one-source fallback, partial list recovery, or topic-based guess.
        if '\n' in raw.strip() or len(REFS.findall(raw))!=1:return original
        proof=certificates(raw,sources)
        if len({p['provided_index'] for p in proof})!=1:return original
        m=next(REFS.finditer(raw));idx=proof[0]['provided_index']
        edits=[(m.start(),m.end(),'['+str(idx)+']'+CITE.sub('',m[0]))];diagnosis=[dict(action='recover_entire_literal_answer',proof=proof)]
    else:
        # Legal citations are only rebound for a complete, independently
        # matchable statement/record. Other sentences and all footers survive.
        offset=0
        for line in raw.splitlines(keepends=True):
            if FOOTER.match(line):offset+=len(line);continue
            groups=list(REFS.finditer(line))
            if len(groups)==1:
                m=groups[0];claim=line[:m.start()]+line[m.end():]
                proof=certificates(claim,sources)
                old={int(v) for v in re.findall(r'\d+',m[0])}
                choices={p['provided_index'] for p in proof}
                if len(choices)==1 and not old&choices:
                    idx=next(iter(choices));edits.append((offset+m.start(),offset+m.end(),'['+str(idx)+']'+CITE.sub('',m[0])))
                    diagnosis.append(dict(action='rebind_complete_literal_unit',old_provided_indices=sorted(old),proof=proof))
            offset+=len(line)
    if not edits:return original
    edited=raw
    for a,b,value in reversed(edits):edited=edited[:a]+value+edited[b:]
    # Reference whitespace belongs to the citation token; no factual rewrite.
    assert CITE.sub('',edited)==CITE.sub('',raw)
    pending=copy.deepcopy(response);pending['answer']=edited
    for key in ('citation_validation','answer_status','retrieved_evidence'):pending.pop(key,None)
    pending['sources']=[]
    result=apply_guard(pending,repair_callback=None)
    assert result['citation_validation']['passed']
    result['trace']['citation_reconcile']=dict(version=VERSION,original_raw_answer=raw,original_final_answer=response['answer'],
        original_guard=copy.deepcopy(trace.get('citation_guard')),actions=diagnosis,semantic_verdict='not_a_general_semantic_validator',
        model_calls=0,restored_original_body=bool(invalid))
    return result
