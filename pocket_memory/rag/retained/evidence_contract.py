# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Source-preserving representation and one citation registry. No gold/routing."""
import copy,re,hashlib,json
from .citation_guard import apply_guard

def cells(line):
    return [c.strip().replace('\\|','|') for c in re.split(r'(?<!\\)\|',line.strip().strip('|'))]

def table_record(source):
    lines=[line for line in source['evidence'].splitlines() if line.strip()]
    if source.get('kind')!='table_row' or len(lines)!=2:return None
    if not all(line.lstrip().startswith('|') and line.rstrip().endswith('|') for line in lines):return None
    hs,vs=map(cells,lines)
    if len(hs)<2 or len(hs)!=len(vs) or any(not h for h in hs) or len(set(hs))!=len(hs):return None
    return list(zip(hs,vs))

def assemble(sources):
    out=[];audit=[]
    for old in sources:
        s=copy.deepcopy(old);record=table_record(s)
        # A source's title/kind are existing structural metadata, not inferred
        # business meaning. Never label a passage as the answer to a gold slot.
        kind='表格原始列值' if record is not None else ('OCR原文' if s.get('source_field')=='ocr_text' or s.get('kind')=='ocr' else '正文原文')
        rendered='\n'.join(h+'：'+v for h,v in record) if record is not None else s['evidence']
        heading=f'[{s["index"]}] {s["title"]} · {s.get("location", "保存来源")}\n{kind}：\n'
        s['prompt_block']=heading+rendered
        assert s['evidence']==old['evidence'] and s.get('raw_evidence')==old.get('raw_evidence')
        identity=json.dumps([s['source_key'],s.get('source_field'),s.get('raw_evidence'),s.get('evidence_id'),s['evidence']],ensure_ascii=False,sort_keys=True)
        audit.append(dict(index=s['index'],source_key=s['source_key'],source_identity_sha256=hashlib.sha256(identity.encode()).hexdigest(),
            representation='original_named_columns' if record is not None else 'original_prose_or_ocr',parsed_cells=record))
        out.append(s)
    return out,audit

def normalize_footer(answer):
    pattern=r'(?m)^(\s*(?:支持来源|支持资料编号|引用来源|来源编号)\s*[:：]\s*)([\d\s\[\]【】,，、.。；;]+)\s*$'
    def replace(m):return m[1]+''.join(f'[{n}]' for n in dict.fromkeys(re.findall(r'\d+',m[2])))
    return re.sub(pattern,replace,answer)

def guard(response):
    response=copy.deepcopy(response)
    raw=response['trace'].get('answer_before_citation_guard',response['answer'])
    clean=normalize_footer(raw)
    # Already validated responses keep their original refusal/table status.
    # Re-running a generic guard used to erase that API meaning.
    if clean==raw and ('citation_validation' in response or
        (response.get('answer_status') and not response.get('provided_sources') and not response.get('sources'))):
        return response
    prior_status=response.get('answer_status')
    if clean!=raw:response['trace']['footer_normalization']=dict(before=raw,after=clean)
    response['answer']=clean
    result=apply_guard(response,repair_callback=None)
    if prior_status and prior_status not in ('answer','no_citation','citation_check_failed') and result['citation_validation']['passed']:
        result['answer_status']=prior_status
    return result

def locations(sources,notes,ocr):
    """Fill only missing, uniquely recoverable original positions; never guess."""
    from .context_contract import Original
    result=copy.deepcopy(sources)
    for s in result:
        if s.get('raw_evidence') or s.get('source_type')!='note':continue
        sid=s['source_id'];field='ocr_text' if s.get('kind')=='ocr' else 'content'
        body=ocr.get(sid,'') if field=='ocr_text' else notes[sid]['content']
        original=Original(body);raw=[]
        for line in (s.get('evidence') or '').splitlines():
            if not line.strip():continue
            found=original.locate(line)
            if found is None:raw=[];break
            a,b=found;raw.append(dict(field=field,char_start=a,char_end_exclusive=b,quote=body[a:b]))
        if raw:
            s.update(raw_evidence=raw,source_field=field,source_verification='unique_original_span',
                location='保存原文 · '+','.join(f'{x["char_start"]}–{x["char_end_exclusive"]}' for x in raw))
        else:s['location_verification']='unresolved_original_span'
    return result
