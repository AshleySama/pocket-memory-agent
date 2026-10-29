# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Source-level evidence integration; original spans accompany every view.

No question/answer table or field-name whitelist. OCR anchors correct only the
rows they cover. Intact TSV, body interpretation and uncertainty are distinct.
"""
import csv
from pathlib import Path
import re

from . import field_citation as previous
from . import field_boundary as guard
TYPES=previous.TYPES
IDENT=previous.IDENT
STORAGE=re.compile(r'^(?:varchar\(\d+\)|(?:bigint|int|decimal)\([\d,]+\)|text|时间戳|万分制)')
HEADER='字段名 | 返回类型 | 存储说明 | 含义'

def compact_map(text):
    positions=[i for i,c in enumerate(text) if not c.isspace()]
    return ''.join(text[i] for i in positions),positions

def ocr_records(ocr):
    lines=[];offset=0
    for line in ocr.splitlines(keepends=True):
        lines.append((line.strip(),offset,offset+len(line.rstrip('\r\n'))));offset+=len(line)
    starts=[i for i in range(len(lines)-1) if IDENT.fullmatch(lines[i][0]) and lines[i+1][0] in TYPES]
    result=[]
    for n,i in enumerate(starts):
        end=starts[n+1] if n+1<len(starts) else len(lines)
        raw=ocr[lines[i][1]:lines[end-1][2]]
        result.append(dict(name=lines[i][0],type=lines[i+1][0],raw=raw,
            char_start=lines[i][1],char_end_exclusive=lines[end-1][2],field='ocr_text'))
    return result

def parse_schema(body,ocr=''):
    body=str(body or '');ocr=str(ocr or '')
    rows=[];uncertain=[];offset=0;nonempty=0
    for line in body.splitlines(keepends=True):
        raw=line.rstrip('\r\n')
        if raw.strip():
            nonempty+=1;cells=next(csv.reader([raw],delimiter='\t'))
            if len(cells)==4 and IDENT.fullmatch(cells[0]) and cells[1] in TYPES:
                rows.append(dict(name=cells[0],type=cells[1],storage=cells[2],description=cells[3],
                    raw=raw,char_start=offset,char_end_exclusive=offset+len(raw),field='content',boundary='intact_tsv'))
            else:uncertain.append(dict(raw=raw,char_start=offset,char_end_exclusive=offset+len(raw),reason='not_a_four_column_record'))
        offset+=len(line)
    if len(rows)>=3:
        return dict(format='tsv',rows=rows,uncertain=uncertain,complete=len(rows)==nonempty,body=body,ocr=ocr)
    header=re.search(r'字段\s*字段类型\s*字段说明\s*备注',body)
    if not header:return None
    matches=list(previous.COLLAPSED.finditer(body,header.end()))
    if len(matches)<4:return None
    spans=[dict(start=m.start(),type_start=m.start('type'),end=m.end(),name=m['name'],type=m['type']) for m in matches]
    normalized,positions=compact_map(body)
    anchors={};conflicts=set()
    for o in ocr_records(ocr):
        needle=re.sub(r'\s+','',o['raw'])
        if needle and normalized.count(needle)==1:
            p=normalized.index(needle);start=positions[p];type_start=positions[p+len(o['name'])]
            match=next((s for s in spans if s['type_start']==type_start and s['type']==o['type']),None)
            if match:
                match.update(start=start,name=o['name']);anchors[start]=o
        else:
            # Do not force contradictory OCR onto the body; isolate that row.
            for s in spans:
                if s['name']==o['name'] or s['name'].endswith(o['name']):conflicts.add(s['start'])
    spans.sort(key=lambda s:s['start']);rows=[];uncertain=[]
    for n,s in enumerate(spans):
        end=spans[n+1]['start'] if n+1<len(spans) else len(body)
        raw=body[s['start']:end];tail=body[s['end']:end];storage=STORAGE.match(tail)
        description=tail[storage.end():] if storage else tail
        problem=None
        if s['start'] in conflicts:problem='ocr_body_conflict'
        elif s['name'].startswith('_'):problem='nested_parent_not_verified'
        elif not description.strip() and s['type'] not in ('Array','Object'):problem='missing_description_boundary'
        if problem:
            uncertain.append(dict(raw=raw,char_start=s['start'],char_end_exclusive=end,reason=problem));continue
        row=dict(name=s['name'],type=s['type'],storage=storage[0] if storage else '',description=description,
            raw=raw,char_start=s['start'],char_end_exclusive=end,field='content',
            boundary='ocr_corroborated' if s['start'] in anchors else 'body_typed_header')
        if s['start'] in anchors:row['ocr_support']=anchors[s['start']]
        rows.append(row)
    # Duplicate names are ambiguous individually; preserve all other rows.
    duplicate={r['name'] for r in rows if sum(x['name']==r['name'] for x in rows)>1}
    clean=[]
    for row in rows:
        if row['name'] in duplicate:uncertain.append(dict(raw=row['raw'],char_start=row['char_start'],char_end_exclusive=row['char_end_exclusive'],reason='duplicate_field_name'))
        else:clean.append(row)
    return dict(format='body_with_ocr_anchors',rows=clean,uncertain=uncertain,complete=False,body=body,ocr=ocr)

def raw_ranges(rows,body):
    ranges=[]
    for r in sorted(rows,key=lambda x:x['char_start']):
        start,end=r['char_start'],r['char_end_exclusive']
        if ranges and not body[ranges[-1][1]:start].strip():ranges[-1][1]=end
        else:ranges.append([start,end])
    return [dict(field='content',char_start=a,char_end_exclusive=b,quote=body[a:b]) for a,b in ranges]

def make_block(note_id,title,question,schema,index,budget):
    full=schema['complete'] and schema['format']=='tsv'
    selected=[dict(r,selection_score=1) for r in schema['rows']] if full else previous.select_rows(question,schema['rows'])
    if not selected:return None
    heading=f'[{index}] {title} · 原文与字段视图\n'
    def render(selected,full):
        spans=[dict(field='content',char_start=0,char_end_exclusive=len(schema['body']),quote=schema['body'])] if full else raw_ranges(selected,schema['body'])
        coverage='本笔记保存正文全部字段；不代表外部接口或整个知识库。' if full else '所列原文记录；未覆盖整份笔记，不能据此作整份资料缺失判断。'
        table=HEADER+'\n'+'\n'.join(' | '.join(r[k].replace('\n',' ') for k in ('name','type','storage','description')) for r in selected)
        originals='\n'.join(f'正文字符{p["char_start"]}—{p["char_end_exclusive"]}：{p["quote"]}' for p in spans)
        origin='完整Tab分列。' if schema['format']=='tsv' else '正文按已给字段/类型表头解释；有OCR支持的行另行对齐，未将正文解释冒充图片核验。'
        evidence=f'核对范围：{coverage}\n依据：{origin}\n分列视图（非逐字引文）：\n{table}\n对应原文：\n{originals}'
        return evidence,spans,coverage
    evidence,spans,coverage=render(selected,full)
    if len(heading+evidence)+2>budget and full:
        full=False;selected=previous.select_rows(question,schema['rows'])
        if not selected:return None
        evidence,spans,coverage=render(selected,full)
    while selected and len(heading+evidence)+2>budget:
        selected.pop(min(range(len(selected)),key=lambda i:(selected[i]['selection_score'],-i)))
        if selected:evidence,spans,coverage=render(selected,False)
    if not selected:return None
    return dict(index=index,source_key=f'note:{note_id}',source_type='note',source_id=note_id,chunk_id=None,
        title=title,location='保存正文；原文字符位置见引用',kind='schema_view',chunk_order=0,node_id=f'note:{note_id}:schema',
        evidence=evidence,excerpt=evidence,prompt_block=heading+evidence,truncated=False,score=max(r['selection_score'] for r in selected),
        structured_rows=selected,schema_format=schema['format'],schema_all_fields=[r['name'] for r in schema['rows']],
        raw_evidence=spans,coverage_complete=full,coverage_notice=coverage,source_field='content',
        uncertain_records=schema['uncertain'],evidence_notice='Explicitly labelled interpretation and unchanged original spans; no inferred parent for nested rows.')

validate_field_references=guard.validate_field_references
