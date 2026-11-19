# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Source-grounded, completely consumed scope requests. No model or entity list.

Only explicit table row labels and unambiguous labelled-field predicates use
this path. Everything else keeps the retained selector. Original facts and
source spans are immutable; selection is performed before chapter compilation.
"""
from __future__ import annotations
import copy,re
from . import report_compiled as c, report_scope_fields as fields, report_adaptive as a

PROTOCOL='exact-row-and-field-scope-v1'
_MISSING={'未提供','未填写','未填','未录入','未设置','空白','为空','空','无记录','未记录','待填写'}

class Unclear(ValueError):pass

def _clean(s):
    return re.sub(r'\s+','',s).strip('“”「」《》\"\' ')

def _clauses(focus):
    return [s.strip() for s in re.split(r'[，,。；;\n]+',focus) if s.strip()]

def _mode(text):
    """Return clause polarity and body, without deleting unexplained words."""
    text=re.sub(r'^(?:请|麻烦|帮我|给我)','',text)
    m=re.match(r'^(?:不要|不收录|不收|不选|排除|去掉|剔除)(.+)$',text)
    if m:return False,m[1]
    m=re.fullmatch(r'(.+?)(?:一律|全部|都)?(?:不收录|不收|不要|不选|排除)',text)
    if m:return False,m[1]
    text=re.sub(r'^(?:只|仅)?(?:要|保留|选取|选择|收录|选出|整理|汇编)','',text)
    return True,text

def _list(text,vocabulary=None):
    text=re.sub(r'(?:这)?[一二两三四五六七八九十\d]+(?:个)?(?:项|行|条|列)$','',text)
    text=re.sub(r'(?:的)?(?:事项|记录|条目|行)$','',text)
    if vocabulary is None:return [_clean(s) for s in re.split(r'或者|或是|以及|和|与|、|或',text) if _clean(s)]
    # Match complete source labels before operators: a row named
    # “测试和交付” is one value, not an AND expression.
    remaining=text;values=[]
    while remaining:
        remaining=remaining.lstrip('“”「」《》\"\' ')
        hits=[v for v in vocabulary if remaining.startswith(v)]
        if not hits:return []
        value=max(hits,key=len);values.append(value);remaining=remaining[len(value):].lstrip('“”「」《》\"\' ')
        if remaining:
            m=re.match(r'^(?:或者|或是|以及|和|与|、|或)',remaining)
            if not m:return []
            remaining=remaining[m.end():]
            if not remaining:return []
    return values

def table_cells(block):
    line=block['text'].splitlines()[0]
    return [v.strip() for v in line.split('\t')] if block.get('table_delimiter')=='\t' else c._cells(line)

def tsv_inventory(units,register):
    """Recognize complete rectangular pasted tables without rewriting source.

    Mixed prose, merged/empty cells and uncertain OCR are not inferred into a
    table. Every retained cell still belongs to an exact original line span.
    """
    parsed={}
    for u in units:
        lines=list(re.finditer(r'[^\n\r]+',u['body']))
        if 'ocr' in u['field'].lower() or len(lines)<3:continue
        cells=[m[0].split('\t') for m in lines]
        if len(cells[0])<2 or len(cells[0])>20 or any(len(row)!=len(cells[0]) or any(not v.strip() for v in row) for row in cells):continue
        if len(set(cells[0]))!=len(cells[0]) or len({row[0] for row in cells[1:]})!=len(cells)-1:continue
        parsed[u['unit_id']]=lines
    if not parsed:return None
    original=a.source_blocks(units);blocks=[]
    for u in units:
        if u['unit_id'] in parsed:
            hid=f'S{len(blocks)+1}'
            for i,m in enumerate(parsed[u['unit_id']]):
                blocks.append(dict(id=f'S{len(blocks)+1}',unit_id=u['unit_id'],start=m.start(),end=m.end(),text=m[0],title=u['title'],tab=u['tab'],field=u['field'],kind='table_header' if i==0 else 'table_row',required_ids=[] if i==0 else [hid],table_delimiter='\t'))
        else:
            part=[b for b in original if b['unit_id']==u['unit_id']];mapping={b['id']:f'S{len(blocks)+i}' for i,b in enumerate(part,1)}
            for b in part:
                b=copy.deepcopy(b);b['id']=mapping[b['id']];b['required_ids']=[mapping[x] for x in b.get('required_ids',[])];blocks.append(b)
    facts=a.direct_source_facts(blocks,units,register)
    contract=dict(c.source_contract(units),table_format_protocol='exact-tsv-lines-v1',inventory_sha256=c._hash(blocks))
    return blocks,facts,contract,dict(key=c._hash(contract),hit=False,invalidated=None)

def table_projection(focus,units,blocks,facts,groups):
    """Prove a row-only request, then split rows into independent selection units.

    The untouched inventory and remaining prose stay accounted for. This does
    not enable arbitrary partial deletion in validate_outline.
    """
    by_block={b['id']:b for b in blocks};by_unit={u['unit_id']:u for u in units}
    tables={}
    for f in facts:
        pieces=[by_block[s] for s in f['source_ids']]
        if len(pieces)!=2 or pieces[0].get('kind')!='table_header' or pieces[1].get('kind')!='table_row':continue
        h,row=pieces
        if row.get('required_ids')!=[h['id']] or row['unit_id']!=h['unit_id']:continue
        cols=table_cells(h);cells=table_cells(row)
        if len(cols)!=len(cells) or not cells[0] or len(set(cols))!=len(cols):continue
        parent=[g for g in groups if f['id'] in g['fact_ids']]
        if len(parent)!=1:continue
        tables.setdefault(h['id'],dict(header=h,columns=cols,rows=[],parent=parent[0]))['rows'].append((f,row,cells))
    if not tables:return None
    # An explicit source title restricts candidate tables, including same-named
    # tabs. No best-score choice between unrelated records is allowed.
    clauses=_clauses(focus);scoped=None
    if clauses:
        for u in units:
            title=_clean(u['title'])
            if len(title)<3:continue
            for suffix in ('里','中','的表格里','的表格中'):
                prefix=title+suffix
                if _clean(clauses[0])==prefix:
                    identities={(x['kind'],x['source_id']) for x in units if _clean(x['title'])==title}
                    if len(identities)!=1:raise Unclear('多份资料使用相同标题，请缩小到明确的资料范围后再选行')
                    scoped={x['unit_id'] for x in units if x['kind']==u['kind'] and x['source_id']==u['source_id']}
                    clauses=clauses[1:];break
            if scoped is not None:break
    if scoped is not None:tables={k:t for k,t in tables.items() if t['header']['unit_id'] in scoped}
    if not tables:return None
    labels={_clean(row[2][0]) for t in tables.values() for row in t['rows']}
    columns={_clean(x) for t in tables.values() for x in t['columns']}
    positive=[];negative=[];output_columns=None;needs_model=False;bounded_labels=True
    for clause in clauses:
        text=_clean(clause)
        if text in ('表格里','表格中','按原表顺序','保留表头','保留所有列','保留全部列','保留原文和引用','保留原文与引用'):continue
        if text in ('满足任一项就保留','包含任一项就保留'):continue
        if re.fullmatch(r'(?:其余|其他)(?:的)?(?:项目|行|条目|内容|项)(?:都)?(?:不要|不收|不选)',text):continue
        m=re.fullmatch(r'(?:请)?把这份(.+?)(?:整理|汇编)成(?:简短)?(?:文稿|报告|汇编)',text)
        if m and len(units)==1 and m[1] in _clean(units[0]['body']):continue
        m=re.fullmatch(r'(?:请)?列出[^，。；:：]*(?:中|里)的[一二两三四五六七八九十\d]+项[^，。；:：]*[：:](.+)',text)
        if m:
            # The prefix may contain substantive constraints. Use the list
            # only to discover row granularity; keep the FULL query for the
            # retained model selector instead of discarding those constraints.
            text='只要'+m[1];needs_model=True
        include,body=_mode(text)
        for column in sorted(columns,key=len,reverse=True):
            m=re.match(re.escape(column)+r'(?:名称)?(?:为|是|等于)(.+)',body)
            if m:
                if any(_clean(t['columns'][0])!=column for t in tables.values()):needs_model=True;bounded_labels=False
                body=m[1];break
        if re.search(r'(?:[一二两三四五六七八九十\d]+)?列$',body):
            names=_list(body,columns)
            if not include or not names or not set(names)<=columns:return None
            output_columns=names;continue
        names=_list(body,labels)
        if not names or not set(names)<=labels:
            needs_model=True;continue
        (positive if include else negative).extend(names)
    if not positive:return None
    selected_tables={hid:t for hid,t in tables.items() if any(_clean(x[2][0]) in positive for x in t['rows'])}
    # Same row label in different tables is not a unique record unless the
    # user explicitly selected the document; do not silently pick one.
    if scoped is None and len(selected_tables)!=1:return None
    if output_columns and any([_clean(x) for x in t['columns']]!=output_columns for t in selected_tables.values()):
        return None  # column projection is a different contract
    for t in selected_tables.values():
        h=t['header'];u=by_unit[h['unit_id']]
        # Footnotes/shared conditions are not silently discarded. Unsupported
        # conditional tables keep the existing path rather than guessing.
        tail=max(r['end'] for _,r,_ in t['rows']);near=u['body'][tail:tail+180].strip().split('\n')[0]
        before=u['body'][:h['start']].rstrip().split('\n')[-1]
        if re.match(r'^(?:注[：:]|备注[：:]|前提[：:]|条件[：:]|仅当|适用条件)',near) or re.search(r'仅适用|只适用|仅当|适用条件|前提[：:]|条件[：:]',before):
            raise Unclear('表格存在共同前提或脚注，当前不能安全只摘指定行；请连同条件一起明确范围')
        if any(re.search(r'同上|同前|见上(?:条|行|文)|参见|如下',cell) for _,_,cells in t['rows'] for cell in cells):
            raise Unclear('表格行含跨行指代，不能丢弃其依赖直接摘录')
        if len({_clean(x[2][0]) for x in t['rows']})!=len(t['rows']):return None
    projected=[];chosen=[];receipts=[]
    for g in groups:
        owned=[t for t in selected_tables.values() if t['parent']['id']==g['id']]
        if not owned:projected.append(copy.deepcopy(g));continue
        row_fids={f['id'] for t in owned for f,_,_ in t['rows']}
        remainder=[fid for fid in g['fact_ids'] if fid not in row_fids]
        if remainder:
            rest=copy.deepcopy(g);rest['fact_ids']=remainder;rest['scope_parent_group_id']=g['id'];projected.append(rest)
        for t in owned:
            for i,(f,row,cells) in enumerate(t['rows'],1):
                gid=f'{g["id"]}R{row["id"]}'
                item=copy.deepcopy(g);item.update(id=gid,title=cells[0],fact_ids=[f['id']],source_ids=f['source_ids'],chars=len(f['text']),scope_parent_group_id=g['id'],scope_row=True)
                projected.append(item)
                if _clean(cells[0]) in positive and _clean(cells[0]) not in negative:chosen.append(gid)
                receipts.append(dict(group_id=gid,parent_group_id=g['id'],fact_id=f['id'],header=t['header']['id'],row=row['id'],unit_id=row['unit_id'],start=row['start'],end=row['end'],selected=gid in chosen))
    dependencies={dep for g in projected if g['id'] in chosen for dep in g.get('required_group_ids',[])}
    if dependencies:return None  # do not silently select explicitly excluded prerequisites
    if needs_model and bounded_labels:
        # A literal enumeration is an upper bound, not a complete semantic
        # decision. Extra qualifiers remain in the full model input. The model
        # may reject listed rows, but cannot re-introduce unrequested rows.
        for g in projected:g['scope_eligible']=g['id'] in chosen
    return dict(protocol=PROTOCOL,kind='table_rows',focus=focus,groups=projected,selected=chosen,rows=receipts,original_groups_sha256=c._hash(groups),positive=positive,negative=negative,needs_model=needs_model)

def field_plan(focus,records,catalog):
    """Resolve explicit field predicates and local anaphora before any model.

    Generic 状态/结果/结论 inherits a unique preceding explicit state field.
    Ambiguous initial references abstain, never bind another similarly named
    column. Only exact existing values or explicit missing-value markers match.
    """
    aliases={}
    for key in catalog:
        names=[key]
        if key.endswith(('状态','结论','结果')):
            root=key[:-2];names += [root+x for x in ('状态','结论','结果','情况')]
        for name in names:aliases.setdefault(name,set()).add(key)
    previous=None;conditions=[];receipts=[]
    for raw in _clauses(focus):
        text=_clean(raw)
        if text in ('保留原文','保留完整原文','保留原文和引用','保留原文与引用'):continue
        positive,body=_mode(text)
        body=re.sub(r'(?:的)?(?:事项|记录|条目)$','',body).removesuffix('的')
        key=None;predicate=None
        for alias in sorted(aliases,key=len,reverse=True):
            if body.startswith(alias) and len(aliases[alias])==1:
                key=next(iter(aliases[alias]));predicate=body[len(alias):];break
        if key is None:
            m=re.match(r'^(?:该|这个|上述|其)?(?:状态|结论|结果)(.+)$',body)
            if m and previous:key=previous;predicate=m[1]
            elif m and len([k for k in catalog if k.endswith(('状态','结论','结果'))])>1:
                raise Unclear('资料包含多个状态字段，请明确审批、执行或其他状态；未擅自选择对象')
        if key is None:return None
        if predicate in ('不作限制','不限','不限制','不做限制'):
            receipts.append(dict(quote=raw,field=key,predicate=predicate,include=None))
            previous=key if key.endswith(('状态','结果','结论')) else previous
            continue
        predicate=re.sub(r'^(?:仍然|仍|还|目前|当前)','',predicate)
        missing=re.fullmatch(r'(?:尚未|还未|未|没有|未曾)(?:填写|填|提供|录入|记录|设置|明确)|(?:为)?(?:空白|空|未提供|未填写|未记录)',predicate)
        known=re.fullmatch(r'(?:已经|已)?(?:明确|填写|填好|提供|录入|记录|设置)',predicate)
        if missing or known:
            if '明确' in predicate and any(_clean(v) in ('未知','待确认','待明确','未明确','不明') for v in catalog[key]):return None
            values=[v for v in catalog[key] if _clean(v) in _MISSING]
            tree=dict(op='in',field=key,values=values)
            if known:tree=dict(op='not',child=tree)
        else:
            predicate=re.sub(r'^(?:为|是|等于)','',predicate)
            values=[v for v in catalog[key] if fields.norm(v)==fields.norm(predicate)]
            if len(values)!=1:return None
            tree=dict(op='in',field=key,values=values)
        if not positive:tree=dict(op='not',child=tree)
        conditions.append(tree);receipts.append(dict(quote=raw,field=key,predicate=predicate,include=positive))
        previous=key if key.endswith(('状态','结果','结论')) else previous
    if not conditions:return None
    expr=dict(op='all',children=conditions)
    decisions={gid:fields.decide(expr,row) for gid,row in records.items()}
    if any(v is None for v in decisions.values()):return None
    return dict(protocol=PROTOCOL,kind='field_predicates',focus=focus,conditions=conditions,clauses=receipts,selected=[gid for gid,v in decisions.items() if v])
