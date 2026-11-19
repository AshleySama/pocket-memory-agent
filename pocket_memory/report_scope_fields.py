# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Conservative filters over explicit record fields, with exact provenance.

No business entity or expected answer vocabulary. Free prose and conflicting
fields stay with the semantic judge. A field in one record never binds another.
"""
import re

def extract(groups, blocks):
    by_id={b['id']:b for b in blocks}; records={}
    for group in groups:
        fields={};overlong=False
        texts=[by_id[s]['text'] for s in group['source_ids']]
        heads=[h.strip() for t in texts for h in re.findall(r'^\s*#{1,6}\s+(.+)$',t,re.M)]
        if len(set(heads))>1:
            records[group['id']]={};continue
        for sid in group['source_ids']:
            b=by_id[sid]
            for m in re.finditer(r'(?:^|[\n；;])\s*([^\s：:；;。\n|]{1,14})[：:]\s*([^；;。\n|]+)',b['text']):
                key,value=m.group(1),m.group(2).strip()
                # Only labelled short records, never prose introductions.
                if key.startswith(('#','-','*')):continue
                if len(value)>60:overlong=True;continue
                start=b['start']+m.start(2); end=start+len(value)
                fields.setdefault(key,[]).append(dict(value=value,source_id=sid,
                    unit_id=b['unit_id'],start=start,end=end,quote=value))
        # Repeated record labels indicate separate records or a state history.
        # Never combine one record's status with another record's owner.
        records[group['id']]=fields if not overlong and all(len(v)==1 for v in fields.values()) else {}
    catalog={}
    for fields in records.values():
        for key,entries in fields.items():
            catalog.setdefault(key,set()).update(e['value'] for e in entries)
    return records,{k:sorted(v) for k,v in catalog.items()}

def norm(text):
    return re.sub(r'\s+','',text).replace('已经','已').replace('尚未','未')

def bind(text,catalog):
    """Return a predicate tree; unresolved leaves are explicit, never dropped."""
    text=text.strip()
    # Bare scalar values only bind declared identity/state fields. A matching
    # remark or arbitrary prose key is never evidence of the item's identity.
    scalar=text[:-1] if text.endswith('的') else text
    matches=[(k, v) for k,vals in catalog.items() if k.endswith('状态') or k in ('负责人','项目','版本') for v in vals if norm(v)==norm(scalar)]
    if len(matches)==1:return dict(op='in',field=matches[0][0],values=[matches[0][1]])
    if text in ('事项','记录'):
        keys=[k for k in catalog if k in ('事项类型','类型','类别')]
        if len(keys)==1:return dict(op='exists',field=keys[0])
    # Compact project/version identifiers retain both dimensions, with values
    # discovered from the current records rather than a project-name lexicon.
    for project in catalog.get('项目',[]):
        for version in catalog.get('版本',[]):
            if text==project+version+'版':
                return dict(op='all',children=[dict(op='in',field='项目',values=[project]),dict(op='in',field='版本',values=[version])])
    for key,values in catalog.items():
        for value in values:
            if text in (key+value,key+'为'+value,key+'等于'+value,key+'是'+value):
                return dict(op='in',field=key,values=[value])
    # Conditions joined with 的 can have independent, source-backed bindings.
    complete=[dict(op='in',field=k,values=[text]) for k,v in catalog.items() if k in ('类型','类别','事项类型') and text in v]
    if len(complete)==1:return complete[0]
    if '的' in text:
        left,right=text.rsplit('的',1)
        if left and right:
            parts=[bind(left,catalog),bind(right,catalog)]
            if any(p['op']!='unknown' for p in parts):return dict(op='all',children=parts)
    # Explicit calendar predicates: preserve both ends and comparison direction.
    normalized=re.sub(r'(\d{4})年(\d{1,2})月(\d{1,2})日',lambda m:f'{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02}',text).removesuffix('的')
    date_match=re.fullmatch(r'(.+?)(早于|不早于|晚于|不晚于|为|在)?(\d{4}-\d{2}-\d{2})(?:(?:至|到|~|～)(\d{4}-\d{2}-\d{2})(?:[（(]含两端[）)])?)?(?:执行)?',normalized)
    if date_match:
        prefix,op,lo,hi=date_match.groups();prefix=re.sub(r'(日期|时间)$','',prefix)
        keys=[k for k,vals in catalog.items() if k.endswith(('日期','时间')) and prefix and k.startswith(prefix) and all(re.fullmatch(r'\d{4}-\d{2}-\d{2}',v) for v in vals)]
        if len(keys)==1:
            import datetime
            datetime.date.fromisoformat(lo)
            if hi:datetime.date.fromisoformat(hi)
            if hi and lo<=hi:return dict(op='range',field=keys[0],lo=lo,hi=hi)
            if not hi:return dict(op='compare',field=keys[0],value=lo,compare=op or '为')
    for key in sorted(catalog,key=len,reverse=True):
        # An abbreviated field name is allowed only for the generic 状态 suffix.
        aliases=[key]+([key[:-2]] if key.endswith('状态') else [])
        for alias in aliases:
            if not text.startswith(alias):continue
            rest=text[len(alias):]
            if rest.startswith('不等于'):
                child=bind(alias+'等于'+rest[3:],catalog)
                if child['op']!='unknown':return dict(op='not',child=child)
            rest=re.sub(r'^(?:必须|明确|确实|均)?(?:为|是|在|等于)?','',rest)
            dates=re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:(?:至|到|~|～)(\d{4}-\d{2}-\d{2})(?:[（(]含两端[）)])?)?',rest)
            if dates:
                lo,hi=dates.group(1),dates.group(2) or dates.group(1)
                if lo<=hi:return dict(op='range',field=key,lo=lo,hi=hi)
            values=[v for v in catalog[key] if norm(v)==norm(rest)]
            if len(values)==1:return dict(op='in',field=key,values=values)
            # An absent explicitly requested value is a valid empty filter, but
            # only for literal equality syntax (not an unrecognised sentence).
            if re.match(r'^(?:明确)?(?:为|等于)',text[len(alias):]) and re.fullmatch(r'[\w\u4e00-\u9fff-]{1,20}',rest):
                return dict(op='in',field=key,values=[rest])
    # Category is a declared subject field, not an arbitrary "unique word" in
    # remarks. Only an exact category label binds without a semantic judgment.
    candidates=[]
    for key,values in catalog.items():
        if key in ('类型','类别','事项类型'):
            matched=[v for v in values if text==v]
            if matched:candidates.append(dict(op='in',field=key,values=matched))
    if len(candidates)==1:return candidates[0]
    # Inverse "张三负责" and "A项目" require the declared field label.
    for key,values in catalog.items():
        labels=[key]+([key[:-1]] if key.endswith('人') else [])
        for label in labels:
            if scalar.endswith(label):
                value=scalar[:-len(label)]
                if value in values:return dict(op='in',field=key,values=[value])
        for value in values:
            prefix=value+key
            if text.startswith(prefix) and len(text)>len(prefix):
                return dict(op='all',children=[dict(op='in',field=key,values=[value]),bind(text[len(prefix):],catalog)])
    return dict(op='unknown',text=text)

def decide(tree,fields):
    op=tree['op']
    if op=='unknown':return None
    if op=='not':
        val=decide(tree['child'],fields);return None if val is None else not val
    if op=='all':
        vals=[decide(t,fields) for t in tree['children']]
        return False if False in vals else (None if None in vals else True)
    entries=fields.get(tree['field'],[]);values={e['value'] for e in entries}
    if len(values)!=1:return None  # missing or conflicting data is not a fact
    value=next(iter(values))
    if op=='exists':return True
    if op=='in':return value in tree['values']
    if op=='range':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):return None
        return tree['lo']<=value<=tree['hi']
    if op=='compare':
        import datetime
        try:datetime.date.fromisoformat(value)
        except ValueError:return None
        target=tree['value'];cmp=tree['compare']
        return {'早于':value<target,'不早于':value>=target,'晚于':value>target,'不晚于':value<=target,'为':value==target,'在':value==target}[cmp]
    raise ValueError('Unknown field filter')

def judgments(plan,groups,blocks,semantic=None):
    records,catalog=extract(groups,blocks)
    bindings={p['id']:bind(p['text'],catalog) for p in plan['atoms']}
    def resolve(tree):
        if tree['op']=='unknown' and tree['text'] in (semantic or {}):return semantic[tree['text']]
        if 'children' in tree:return dict(tree,children=[resolve(t) for t in tree['children']])
        if 'child' in tree:return dict(tree,child=resolve(tree['child']))
        return tree
    bindings={pid:resolve(tree) for pid,tree in bindings.items()}
    def keys(tree):
        if 'field' in tree:return {tree['field']}
        if tree['op']=='unknown':return set()
        return set().union(*(keys(t) for t in tree.get('children',[])),*( [keys(tree['child'])] if 'child' in tree else []))
    def expr_keys(expr):
        if expr['op']=='atom':return keys(bindings[expr['id']])
        return set().union(*(expr_keys(t) for t in expr.get('children',[])),*([expr_keys(expr['child'])] if 'child' in expr else []))
    def typed(expr):
        out=dict(expr)
        if 'children' in expr:
            out['children']=[typed(t) for t in expr['children']]
            columns=[expr_keys(t) for t in out['children']]
            # An implicit attribute list on different known columns is a
            # conjunction. Explicit OR and every exclusion list are untouched.
            if expr.get('implicit_list') and all(columns) and sum(map(len,columns))==len(set().union(*columns)):
                out['op']='all';out['basis']='distinct_explicit_fields'
        if 'child' in expr:out['child']=typed(expr['child'])
        return out
    values={gid:{pid:decide(tree,fields) for pid,tree in bindings.items()} for gid,fields in records.items()}
    return dict(bindings=bindings,records=records,values=values,expression=typed(plan['expression']) if 'expression' in plan else None)

def binding_request(plan,groups,blocks):
    """Resolve short unbound predicates once against the discovered schema.

    Empty means abstain; arbitrary values, operators and rewritten questions
    cannot be produced. This is semantic mapping, not verified truth.
    """
    from . import report_adaptive as a
    _,catalog=extract(groups,blocks);pending=[]
    def walk(tree):
        if tree['op']=='unknown' and len(tree['text'])<=14 and tree['text'] not in pending:pending.append(tree['text'])
        for t in tree.get('children',[]):walk(t)
        if 'child' in tree:walk(tree['child'])
    for p in plan['atoms']:walk(bind(p['text'],catalog))
    options={f'F{i}V{j}':[key,value] for i,(key,vals) in enumerate(catalog.items(),1) for j,value in enumerate(vals,1)}
    if not pending or not options:return None
    labels={f'B{i}':text for i,text in enumerate(pending,1)}
    schema=a._obj({pid:dict(type='array',items=dict(type='string',enum=list(options)),maxItems=8) for pid in labels})
    messages=[dict(role='system',content='/no_think\n把用户的短条件映射到原文已有字段取值。每项只可表示一个字段上的允许值集合。只在含义完全一致时选代码；复合条件、关系不明、值不存在时返回空数组。选择主体自身字段，附带/关联/备注字段不能替代主体。不要根据字词相似扩大范围。不输出解释。'),
        dict(role='user',content='原始短条件：\n'+json_dump(labels)+'\n原文字段取值代码：\n'+json_dump(options))]
    return dict(messages=messages,schema=schema,labels=labels,options=options,max_output_tokens=48+24*len(labels))

def json_dump(obj):
    import json
    return json.dumps(obj,ensure_ascii=False)

def parse_binding(response,request):
    from . import report_adaptive as a
    obj=a._parse(response)
    if not isinstance(obj,dict) or set(obj)!=set(request['labels']):raise ValueError('字段映射条件不完整')
    result={}
    for pid,codes in obj.items():
        if not isinstance(codes,list) or len(set(codes))!=len(codes) or any(c not in request['options'] for c in codes):raise ValueError('字段映射包含未知取值')
        if not codes:continue
        pairs=[request['options'][c] for c in codes]
        if len({k for k,v in pairs})!=1:raise ValueError('一个条件不能被映射到多个不同字段')
        result[request['labels'][pid]]=dict(op='in',field=pairs[0][0],values=[v for k,v in pairs])
    return result
