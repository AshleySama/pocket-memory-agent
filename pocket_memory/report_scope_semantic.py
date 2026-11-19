# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Parse scope once; judge positive atomic predicates; compose truth in Python.

The model never chooses the final set or writes citations. A quoted clause is
auditable but not automatically a correct interpretation. Unknowns are explicit.
"""
from __future__ import annotations
import copy
import json
import math
import re
import time
from . import report_adaptive as a, report_compiled as c

PROTOCOL = 'scope-predicates-three-valued-v2'
SCOPE_TOKENS = 512
MARGIN = 96
MAX_ATOMS = 8
MAX_BATCHES = 24


def partitions(groups, units):
    by_unit={u['unit_id']:u for u in units}; parts={}
    for g in groups:
        keys=set()
        for uid in g['unit_ids']:
            u=by_unit[uid]
            keys.add(('document',str(u['source_id'])) if u['kind']=='document' else ('unit',uid))
        if len(keys)!=1:raise ValueError('候选组跨越独立来源，不能判断范围')
        parts.setdefault(next(iter(keys)),[]).append(g)
    return list(parts.values())


def enabled(sectioned, groups, units, focus):
    # Common prerequisites need a branch-aware scope representation. Keep that
    # accepted path intact until such a representation has been validated.
    return (sectioned is not None and bool(focus.strip())
            and not any(g.get('required_group_ids') for g in groups)
            and len(partitions(groups,units))>1)


def scope_request(focus):
    atom=a._obj(dict(field=dict(type='string',enum=['topic','state','component']),
                     term=a._string(1,30),quote=a._string(1,80)))
    schema=a._obj({**{key:dict(type='array',maxItems=MAX_ATOMS,items=atom) for key in ('any','all','exclude')},
                   'unsupported':a._string(0,80)})
    messages=[dict(role='system',content=(
        '/no_think\n将用户的内容范围转换成JSON逻辑，不阅读资料、不选内容。'
        'any是并列纳入对象，满足任意一个即可；“也要、还要、以及”扩展any，不是每项都必须有。'
        'all是每项都必须同时满足的共同条件，例如状态限制，或明确要求同时含有的成分。'
        'exclude是明确不要的内容，命中任一排除项即排除。'
        'field区分topic主题/主对象、state状态/限制、component配料/组成。主题提到的辅助成分不等于主题。'
        'term从用户原话逐字摘取最短独立概念，不含并列关系词，不添加同义词；quote摘取含关系词的连续原句作为依据。'
        'any为空表示不限制主题，不表示全部不要；all、exclude没有则为空。'
        '保留步骤、注意事项、条件、完整原文等成稿要求不属于筛选条件，不写入any、all或exclude。'
        '所有判断都是正向属性，排除关系只写在exclude中，不在term中再取反。'
        '分支限定、含糊的否定或不能确定的范围，不强行压平，unsupported简述原因；普通平铺条件填空字符串。'
        '仅输出any、all、exclude、unsupported。')),
        dict(role='user',content=focus)]
    return messages,schema


def parsed(response):
    def unique(pairs):
        obj={}
        for k,v in pairs:
            if k in obj:raise ValueError('范围结果出现重复JSON键')
            obj[k]=v
        return obj
    a._parse(response)
    return json.loads(response['text'],object_pairs_hook=unique)


def parse_scope(response, focus):
    obj=parsed(response)
    if not isinstance(obj,dict) or set(obj)!={'any','all','exclude','unsupported'}:
        raise ValueError('内容范围规则不完整')
    if not isinstance(obj['unsupported'],str) or len(obj['unsupported'])>80:raise ValueError('范围状态无效')
    if obj['unsupported'].strip():raise ValueError('范围需要确认：'+obj['unsupported'])
    atoms=[]; keys={}; logic={key:[] for key in ('any','all','exclude')}
    for op in logic:
        rows=obj[op]
        if not isinstance(rows,list) or len(rows)>MAX_ATOMS:raise ValueError('范围条件数量无效')
        for x in rows:
            if (not isinstance(x,dict) or set(x)!={'field','term','quote'}
                or x['field'] not in ('topic','state','component')
                or not isinstance(x['term'],str) or not 1<=len(x['term'].strip())<=30
                or not isinstance(x['quote'],str) or not 1<=len(x['quote'])<=80
                or x['quote'] not in focus or x['term'] not in x['quote']):
                raise ValueError('范围条件缺少用户原话依据')
            if op=='all' and x['field']=='topic' and re.search(r'(也要|还要|也包括|还包括)',x['quote']) and not re.search(r'(同时|必须|并且)',x['quote']):
                raise ValueError('并列纳入被误当成同时满足，未按错误规则选材')
            key=(x['field'],x['term'])
            if key not in keys:
                keys[key]=f'P{len(atoms)+1}';atoms.append(dict(id=keys[key],field=x['field'],term=x['term']))
            pid=keys[key]
            if pid not in logic[op]:logic[op].append(pid)
    if len(atoms)>MAX_ATOMS:raise ValueError('范围条件过多，未丢弃条件')
    if set(logic['all'])&set(logic['exclude']):raise ValueError('同时满足与排除条件冲突，需要确认')
    if not atoms and focus.strip():
        raise ValueError('没有识别出限制范围，未擅自汇编全部资料')
    return dict(atoms=atoms,logic=logic,raw_rules=obj)


def tri_any(values, empty=False):
    if not values:return empty
    if True in values:return True
    return None if None in values else False


def tri_all(values):
    if False in values:return False
    return None if None in values else True


def evaluate(plan, values):
    inc=tri_any([values[p] for p in plan['logic']['any']],empty=True)
    req=tri_all([values[p] for p in plan['logic']['all']])
    exc=tri_any([values[p] for p in plan['logic']['exclude']])
    return tri_all([inc,req,None if exc is None else not exc])


def match_budget(groups,atoms):return 48+32*len(groups)*len(atoms)


def match_request(plan,groups,units,blocks):
    by_block={b['id']:b for b in blocks}; previews=[];rows=[]
    for g in groups:
        text=[]
        for sid in g['source_ids']:
            b=by_block[sid]
            previews.append(dict(group_id=g['id'],source_id=sid,unit_id=b['unit_id'],start=b['start'],end=b['end'],quote=b['text'],position='full'))
            text.append(f'原文编号[{sid}]\n'+b['text'])
        rows.append(g['id']+'｜主题：'+g['title']+'\n'+'\n'.join(text))
    atoms=plan['atoms']
    # Inclusion/exclusion operators and the whole user sentence intentionally do
    # not enter this independent attribute judge. Only Python applies them.
    rules='\n'.join(f'{p["id"]}｜{p["field"]}｜{p["term"]}' for p in atoms)
    messages=[dict(role='system',content=(
        '/no_think\n依据原文判断每个主题组是否具有每个独立属性，不决定是否选入汇编。'
        'topic判断主题/主对象，辅助配料不是主题；state判断该主题的实际状态，不能把未来或否定表述当成当前肯定状态；'
        'component判断组成/配料。各P属性独立判断，不要求它们同时成立。'
        'Y表示原文支持该属性，N表示原文明确为其他对象或相反状态，U表示无法确定。'
        '先确定本组的主对象，再比较属性指向的对象是否相同；同字、相似词或相关产物不代表同一对象。'
        'Y须在ids填写直接支持该属性的原文编号；N或U的ids为空数组。不要抄写或改写原文。'
        '输出matches，包含全部G和全部P；每项仅v和ids。不输出最终选材或章节。')),
        dict(role='user',content='独立属性：\n'+rules+'\n\n本篇完整主题原文：\n'+'\n\n'.join(rows))]
    def value(g):
        return a._obj(dict(v=dict(type='string',enum=['Y','N','U']),
            ids=dict(type='array',maxItems=len(g['source_ids']),uniqueItems=True,
                     items=dict(type='string',enum=g['source_ids']))))
    schema=a._obj(dict(matches=a._obj({g['id']:a._obj({p['id']:value(g) for p in atoms}) for g in groups})))
    return messages,schema,previews


def parse_matches(response,plan,groups,blocks):
    obj=parsed(response);expected={g['id'] for g in groups};pids={p['id'] for p in plan['atoms']}
    if not isinstance(obj,dict) or set(obj)!={'matches'} or not isinstance(obj['matches'],dict) or set(obj['matches'])!=expected:
        raise ValueError('属性核对遗漏或混入候选主题')
    by_block={b['id']:b for b in blocks};result={}
    for g in groups:
        row=obj['matches'][g['id']]
        if not isinstance(row,dict) or set(row)!=pids:raise ValueError('属性核对遗漏或混入条件')
        result[g['id']]={}
        for pid,x in row.items():
            if (not isinstance(x,dict) or set(x)!={'v','ids'} or x['v'] not in ('Y','N','U')
                    or not isinstance(x['ids'],list) or any(type(sid)is not str for sid in x['ids'])
                    or len(x['ids'])!=len(set(x['ids']))):raise ValueError('属性判断无效')
            if not set(x['ids'])<=set(g['source_ids']) or not set(x['ids'])<=by_block.keys():
                raise ValueError('属性引用了其他主题或未知原文')
            if (x['v']=='Y') != bool(x['ids']):raise ValueError('属性状态与证据编号不一致')
            result[g['id']][pid]={'Y':True,'N':False,'U':None}[x['v']]
    return result


def plans(engine,plan,groups,units,blocks):
    result=[]
    if not plan['atoms']:return result
    for part in ([g] for group_part in partitions(groups,units) for g in group_part):
        batch=[]
        def pack(items):
            messages,schema,previews=match_request(plan,items,units,blocks)
            count=engine.report_prompt_tokens(messages)
            if type(count)is not int or count<=0:raise ValueError('无有效输入token数')
            return dict(group_ids=[g['id'] for g in items],messages=messages,schema=schema,previews=previews,
                        input_tokens=count,max_output_tokens=match_budget(items,plan['atoms']))
        for g in part:
            candidate=pack(batch+[g])
            if candidate['input_tokens']+candidate['max_output_tokens']+MARGIN>engine.n_ctx:
                if batch:result.append(pack(batch));batch=[];candidate=pack([g])
                if candidate['input_tokens']+candidate['max_output_tokens']+MARGIN>engine.n_ctx:
                    raise ValueError('完整主题与范围属性超过上下文，未删原文或排除条件')
            batch.append(g)
        if batch:result.append(pack(batch))
    if len(result)>MAX_BATCHES:raise ValueError('范围核对批次数超过上限，未截断候选')
    return result


def reduce_matches(plan,records,groups,facts,blocks):
    from . import report_brief as b
    by_group={g['id']:g for g in groups};values={}
    for record in records:
        ids=record['group_ids']
        if len(ids)!=len(set(ids)) or not set(ids)<=by_group.keys():raise ValueError('属性批次身份重复或未知')
        current=parse_matches(record['response'],plan,[by_group[g] for g in ids],blocks)
        if values.keys()&current.keys():raise ValueError('主题重复出现在多个属性批次')
        values.update(current)
    if not plan['atoms']:values={g['id']:{} for g in groups}
    if set(values)!=by_group.keys():raise ValueError('没有核对完整候选集合')
    decisions={gid:evaluate(plan,row) for gid,row in values.items()}
    if any(v is None for v in decisions.values()):raise ValueError('部分范围属性无法确定，不能作为无匹配处理')
    chosen=[g for g in groups if decisions[g['id']]];ids={g['id'] for g in chosen}
    if any(set(g.get('required_group_ids',[]))-ids for g in chosen):
        raise ValueError('选材与必要共同前提冲突，未自动扩入排除内容')
    outline=[];size=max(1,math.ceil(len(chosen)/b.MAX_CHAPTERS))
    for i in range(0,len(chosen),size):
        part=chosen[i:i+size]
        outline.append(dict(id=f'C{len(outline)+1}',title=(part[0]['title'].strip()[:48] or '已选内容')+('等' if len(part)>1 else ''),
                            purpose='',fact_ids=[f for g in part for f in g['fact_ids']]))
    fids={fid for g in chosen for fid in g['fact_ids']};selected=[f for f in facts if f['id'] in fids]
    selection=b._selection(groups,ids,'selected' if chosen else 'no_match')
    selection.update(response_protocol=PROTOCOL,scope_plan=plan,attribute_values=values,
        group_decisions=[dict(group=g['id'],decision='include' if decisions[g['id']] else 'exclude',reason='程序按已解析范围组合属性') for g in groups],
        basis='统一范围规则＋独立属性判断；程序执行任一、同时和排除；原文及引用保持')
    return b.validate_outline(outline,selected,groups),selected,selection


def check(record,contract,groups,units,blocks,facts,focus,n_ctx):
    if record.get('protocol')!=PROTOCOL or record.get('contract')!=contract:raise ValueError('范围规则契约不一致')
    scope=record['scope_record'];messages,schema=scope_request(focus)
    count=scope['input_tokens']
    if (scope['messages']!=messages or scope['schema']!=schema or type(count)is not int or count<=0
            or scope['max_output_tokens']!=SCOPE_TOKENS or count+SCOPE_TOKENS+MARGIN>n_ctx):
        raise ValueError('范围解析输入或预算不一致')
    plan=parse_scope(scope['response'],focus)
    if record['scope_plan']!=plan:raise ValueError('范围规则被改变')
    by_group={g['id']:g for g in groups};previews=[]
    for saved in record['source_batches']:
        if not saved['group_ids'] or not set(saved['group_ids'])<=by_group.keys():raise ValueError('未知范围主题')
        part=[by_group[gid] for gid in saved['group_ids']]
        if len(partitions(part,units))!=1:raise ValueError('属性批次混入独立来源')
        messages,schema,p=match_request(plan,part,units,blocks);count=saved['input_tokens']
        if (saved['messages']!=messages or saved['schema']!=schema or saved['previews']!=p
                or saved['max_output_tokens']!=match_budget(part,plan['atoms']) or type(count)is not int or count<=0
                or count+saved['max_output_tokens']+MARGIN>n_ctx):raise ValueError('属性输入、范围或预算不一致')
        c._validate_previews(p,blocks);previews.extend(p)
    if record['previews']!=previews:raise ValueError('完整原文送模记录不一致')
    return reduce_matches(plan,record['source_batches'],groups,facts,blocks)


def execute(engine,focus,groups,units,blocks,contract,trace,stage):
    from .report_brief import _seal
    def call(phase,request):
        began=time.monotonic();item=dict(phase=phase,cached=False,**copy.deepcopy(request),response=None,model_input_delivered=False,generation_started=True)
        trace.append(item)
        try:
            response=engine.report_completion(request['messages'],request['max_output_tokens'],schema=request['schema'])
            item.update(response=response,model_input_delivered=True,output_tokens=response.get('usage',{}).get('completion_tokens'),output_chars=len(response.get('text','')))
            return dict(request,response=response)
        finally:item['seconds']=round(time.monotonic()-began,3)
    with engine.session():
        stage('正在解析包含、同时满足与排除要求')
        messages,schema=scope_request(focus);count=engine.report_prompt_tokens(messages)
        if type(count)is not int or count<=0 or count+SCOPE_TOKENS+MARGIN>engine.n_ctx:raise ValueError('范围要求超过模型预算')
        scope=call('scope-parse',dict(messages=messages,schema=schema,input_tokens=count,max_output_tokens=SCOPE_TOKENS,previews=[]))
        plan=parse_scope(scope['response'],focus)
        batches=plans(engine,plan,groups,units,blocks);records=[]
        for i,p in enumerate(batches,1):
            stage(f'正在核对资料属性 {i}/{len(batches)}')
            records.append(call('scope-attributes',p))
    return _seal(dict(protocol=PROTOCOL,contract=contract,scope_record=scope,scope_plan=plan,source_batches=records,
                      previews=[p for r in records for p in r['previews']]))


def replay_trace(record,prepared=False):
    return [dict(phase=phase,**copy.deepcopy(r),cached=not prepared,reused_from_prepared=prepared,seconds=0,model_input_delivered=True)
            for phase,r in [('scope-parse',record['scope_record'])]+[('scope-attributes',x) for x in record['source_batches']]]
