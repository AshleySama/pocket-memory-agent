# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Evidence boundaries only: intact TSV or saved OCR lines corroborated by body.

Collapsed strings alone cannot certify a field boundary. No field-name map or
question fixture is used. An OCR fragment never establishes whole-note absence.
"""
import csv
import re

TYPES=('String','Number','Array','Object','Boolean','Integer','Float','Double')
IDENT=re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
STORAGE=re.compile(r'^(?:varchar\(\d+\)|(?:bigint|int|decimal)\([\d,]+\)|text|时间戳|万分制)$')

def compact(text):
    return re.sub(r'\s+','',text)

def parse_schema(text,ocr=''):
    text=str(text or '');ocr=str(ocr or '')
    rows=[];offset=0
    for line in text.splitlines(keepends=True):
        raw=line.rstrip('\r\n');cells=next(csv.reader([raw],delimiter='\t'))
        if len(cells)==4 and IDENT.fullmatch(cells[0]) and cells[1] in TYPES:
            rows.append(dict(name=cells[0],type=cells[1],storage=cells[2],description=cells[3],
                char_start=offset,char_end_exclusive=offset+len(raw),raw=raw,field='content',format='tsv'))
        offset+=len(line)
    if rows:
        if len(rows)<3 or len(rows)!=sum(bool(s.strip()) for s in text.splitlines()):return None
        return dict(format='tsv',rows=rows,complete=True,raw=text,field='content',
            coverage='完整保存正文；完整性仅指这份笔记，不代表外部接口全部字段。')
    if not ocr or not re.search(r'字段类型',ocr):return None
    lines=[];offset=0
    for line in ocr.splitlines(keepends=True):
        lines.append((line.strip(),offset,offset+len(line.rstrip('\r\n'))));offset+=len(line)
    starts=[i for i in range(len(lines)-1) if IDENT.fullmatch(lines[i][0]) and lines[i+1][0] in TYPES]
    if len(starts)<3:return None
    rows=[];body=compact(text)
    # Corroboration is ordered and unique; conflicting or repeated OCR data is
    # not silently preferred. Stop the schema view instead of guessing columns.
    previous=-1
    for pos,i in enumerate(starts):
        end=starts[pos+1] if pos+1<len(starts) else len(lines)
        values=[s[0] for s in lines[i+2:end] if s[0]]
        storage=values.pop(0) if values and STORAGE.fullmatch(values[0]) else ''
        description=''.join(values)
        if not description and lines[i+1][0] not in ('Object','Array'):return None
        begin=lines[i][1];finish=lines[end-1][2]
        raw=ocr[begin:finish];needle=compact(raw)
        if body.count(needle)!=1:return None
        found=body.index(needle)
        if found<=previous:return None
        previous=found
        rows.append(dict(name=lines[i][0],type=lines[i+1][0],storage=storage,description=description,
            char_start=begin,char_end_exclusive=finish,raw=raw,field='ocr_text',format='ocr_lines_corroborated'))
    names=[r['name'] for r in rows]
    if len(names)!=len(set(names)):return None
    return dict(format='ocr_lines_corroborated',rows=rows,complete=False,raw=ocr,field='ocr_text',
        coverage='仅已保存OCR片段，并与正文逐行核对一致；不是整份笔记的完整字段清单，片段未列不等于字段不存在。')

def make_block(note_id,title,question,schema,index,budget):
    # Whole bounded tables avoid selecting two rows then claiming full absence.
    rows=schema['rows']
    heading=f'[{index}] {title} · 字段原文与范围\n'
    table='字段名 | 返回类型 | 字段说明 | 备注\n'+'\n'.join(' | '.join(r[k] for k in ('name','type','storage','description')) for r in rows)
    evidence=f'范围：{schema["coverage"]}\n共 {len(rows)} 条字段记录。\n原文：\n{schema["raw"]}\n'
    if schema['field']=='ocr_text':
        # OCR retains line breaks; a labelled view makes column boundaries clear.
        evidence+='\n核对后的分列视图（不是逐字引文）：\n'+table
    if len(heading+evidence)+2>budget:return None # no partial table masquerading as complete
    return dict(index=index,source_key=f'note:{note_id}',source_type='note',source_id=note_id,
        chunk_id=None,title=title,location=f'{schema["field"]} 全部保存范围 0—{len(schema["raw"])} 字符',
        kind='schema_view',chunk_order=0,node_id=f'note:{note_id}:schema',evidence=evidence,
        excerpt=evidence,prompt_block=heading+evidence,truncated=False,score=1,
        structured_rows=rows,schema_format=schema['format'],schema_all_fields=[r['name'] for r in rows],
        coverage_complete=schema['complete'],coverage_notice=schema['coverage'],source_field=schema['field'],
        raw_evidence=[dict(quote=schema['raw'],char_start=0,char_end_exclusive=len(schema['raw']),field=schema['field'])],
        evidence_notice='Only TSV/OCR boundaries are used; collapsed-only fields remain unverified.')

def validate_field_references(answer,supplied,cited):
    schema=[s for s in supplied if s.get('structured_rows')]
    if not schema:return []
    fields={name for s in schema for name in s['schema_all_fields']}
    words=set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*',answer));errors=[]
    for name in fields:
        for kind in TYPES:
            if name+kind in words and name+kind not in fields:errors.append('field_type_glued:'+name+kind)
    supported={r['name'] for s in schema if s['index'] in cited for r in s['structured_rows']}
    for name in sorted((words&fields)-supported):errors.append('field_not_in_cited_rows:'+name)
    # Limited deterministic scope guard, not a semantic absence classifier.
    if any(not s['coverage_complete'] for s in schema):
        negative=bool(re.search(r'未(?:提及|列出|提供|包含|给出)|没(?:有|写)|不存在|无法确定|不足',answer))
        scoped=bool(re.search(r'OCR|片段|截图|图片|局部|部分',answer,re.I))
        if negative and not scoped:errors.append('absence_scope_not_limited_to_fragment')
    return errors
