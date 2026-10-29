# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Conservative schema views with original character spans, never inferred values.

TSV uses Python's standard CSV reader. Collapsed text is eligible only with an
explicit field/type header and unambiguous typed identifiers. No field-name map,
question fixture, note ID, model-generated parser or source mutation is used.
"""
import csv
import io
import math
import re

TYPES=('String','Number','Array','Object','Boolean','Integer','Float','Double')
TYPE_PATTERN='|'.join(TYPES)
IDENT=re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
COLLAPSED=re.compile(r'(?P<name>[A-Za-z_][A-Za-z0-9_]*?)(?P<type>'+TYPE_PATTERN+r')(?=(?:varchar\(|int\(|bigint\(|decimal\(|text(?=[\u3400-\u9fff])|[\u3400-\u9fff]|[a-z_][A-Za-z0-9_]*(?:'+TYPE_PATTERN+r')))' )
STORAGE=re.compile(r'^(?:varchar\(\d+\)|(?:bigint|int|decimal)\([\d,]+\)|text)')


def parse_schema(text):
    text=str(text or '')
    rows=[]
    # A table with intact tabs is preferable to interpreting compressed text.
    offset=0
    for line in text.splitlines(keepends=True):
        cells=next(csv.reader([line.rstrip('\r\n')],delimiter='\t'))
        if len(cells)==4 and IDENT.fullmatch(cells[0]) and cells[1] in TYPES:
            rows.append(dict(name=cells[0],type=cells[1],storage=cells[2],description=cells[3],
                char_start=offset,char_end_exclusive=offset+len(line.rstrip('\r\n')),
                raw=line.rstrip('\r\n'),format='tsv'))
        offset+=len(line)
    if len(rows)>=3:
        # Reject a partly parsed nonempty TSV table rather than pretend complete.
        nonempty=[line for line in text.splitlines() if line.strip()]
        if len(rows)!=len(nonempty):return None
        return {'format':'tsv','rows':rows,'complete':True}
    rows=[]
    header=re.search(r'字段\s*字段类型\s*字段说明\s*备注',text)
    if not header:return None
    matches=list(COLLAPSED.finditer(text,header.end()))
    if len(matches)<4 or matches[0].start()!=header.end():return None
    for i,match in enumerate(matches):
        end=matches[i+1].start() if i+1<len(matches) else len(text)
        tail=text[match.end():end]
        storage=STORAGE.match(tail)
        description=tail[storage.end():] if storage else tail
        if not description.strip() and match['name']!='_child' and match['type'] not in ('Object','Array'):return None
        # An unexplained typed identifier in a cell makes this layout unsafe.
        if re.search(r'[A-Za-z_][A-Za-z0-9_]*(?:String|Number|Array|Object|Boolean)(?=[\u3400-\u9fff])',description):return None
        rows.append(dict(name=match['name'],type=match['type'],storage=storage[0] if storage else '',
            description=description,char_start=match.start(),char_end_exclusive=end,
            raw=text[match.start():end],format='collapsed_typed_header'))
    names=[row['name'] for row in rows if not row['name'].startswith('_')]
    if len(names)!=len(set(names)):return None
    return {'format':'collapsed_typed_header','rows':rows,'complete':True}


def cjk_bigrams(value):
    return {s[i:i+2] for s in re.findall(r'[\u3400-\u9fff]+',value) for i in range(len(s)-1)}


def select_rows(question,rows,limit=6):
    compact=re.sub(r'\s+','',question)
    qgrams=cjk_bigrams(question)
    grams=[cjk_bigrams(r['description']) for r in rows]
    frequencies={g:sum(g in gs for gs in grams) for g in set().union(*grams)}
    ranked=[]
    for index,row in enumerate(rows):
        if row['name'].startswith('_'):continue # child rows need a full nested schema, not a guessed parent
        explicit=bool(re.search(r'(?<![A-Za-z0-9_])'+re.escape(row['name'])+r'(?![A-Za-z0-9_])',question))
        label=re.split(r'[:：,，;；]',row['description'])[0].strip()
        exact=bool(label and len(label)>=2 and re.sub(r'\s+','',label) in compact)
        overlap=qgrams & grams[index]
        score=30*explicit+20*exact+sum(math.log((len(rows)+1)/(frequencies[g]+1))+1 for g in overlap)
        if score>0:ranked.append((score,index,row))
    ranked.sort(key=lambda item:(-item[0],item[1]))
    chosen=ranked[:limit]
    return [dict(row,selection_score=score) for score,_,row in sorted(chosen,key=lambda x:x[1])]


def make_block(note_id,title,question,schema,index,budget):
    selected=select_rows(question,schema['rows'])
    if not selected:return None
    heading=f'[{index}] {title} · 字段结构视图（保留原文位置）\n'
    while selected:
        table='字段名 | 类型 | 存储说明 | 含义\n'+'\n'.join(' | '.join(row[k].replace('\n',' ') for k in ['name','type','storage','description']) for row in selected)
        raw='\n'.join(f'字符{row["char_start"]}—{row["char_end_exclusive"]}: {row["raw"]}' for row in selected)
        evidence='原文分列视图（仅添加分隔，不补充字段值）：\n'+table+'\n\n对应原文摘录：\n'+raw
        if len(heading+evidence)+2<=budget:break
        # Remove lowest relevance record intact, never cut through a field row.
        remove=min(range(len(selected)),key=lambda i:(selected[i]['selection_score'],-i))
        selected.pop(remove)
    if not selected:return None
    return {'index':index,'source_key':f'note:{note_id}','source_type':'note','source_id':note_id,
        'chunk_id':None,'title':title,'location':'正文字段记录（原文字符位置见引用）',
        'kind':'schema_view','chunk_order':0,'node_id':f'note:{note_id}:schema',
        'evidence':evidence,'excerpt':evidence,'prompt_block':heading+evidence,
        'truncated':False,'score':max(r['selection_score'] for r in selected),
        'structured_rows':selected,'schema_format':schema['format'],
        'schema_all_fields':[r['name'] for r in schema['rows'] if not r['name'].startswith('_')],
        'raw_evidence':[{'quote':r['raw'],'char_start':r['char_start'],'char_end_exclusive':r['char_end_exclusive']} for r in selected],
        'evidence_notice':'Formatted view is not a verbatim quotation. Original unchanged spans are included alongside each parsed row.'}


def validate_field_references(answer,supplied,cited):
    schema=[s for s in supplied if s.get('structured_rows')]
    if not schema:return []
    fields={name for s in schema for name in s['schema_all_fields']}
    words=set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*',answer))
    mentioned=words&fields
    errors=[]
    for name in fields:
        for kind in TYPES:
            if name+kind in words and name+kind not in fields:
                errors.append('field_type_glued:'+name+kind)
    # This is a field-membership guard, not a claim-entailment classifier.
    supported={r['name'] for s in schema if s['index'] in cited for r in s['structured_rows']}
    for name in sorted(mentioned-supported):errors.append('field_not_in_cited_rows:'+name)
    return errors
