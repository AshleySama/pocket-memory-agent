# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Automatic evidence-constrained reports: the model selects, never invents facts.

Complete fact bundles carry subject/condition/list boundaries. The source itself
owns factual wording; the model ranks bundles for a compact knowledge report.
"""
from __future__ import annotations
import hashlib,json,re,time
from pathlib import Path
from .report_digest import atomic_json
from .report_source_frames import source_frames

VERSION='grounded-selection-3'
OUTPUT=360
MARGIN=96


def messages_for(title, frames):
    rows=[]
    for f in frames:
        rows.append(f'[{f["id"]}] 主题：{f.get("topic_hint") or "未命名"}；类型：{f.get("kind")}\n{f["text"]}')
    return [dict(role='system',content=(
        '/no_think\n为知识报告选取重要事实。只能返回给出的编号，不能输出或改写正文。'
        '每个不同主题保留2至5组关键定义、机制、完整条件或完整清单；原文较短时可少于2组。选最能帮助理解的事实，'
        '去掉重复解释和例子、推广闲话；必须保留成立条件、反向情况和完整枚举。不要只选定义漏掉区别和步骤。'
        '主题不同不能互相替代。输出JSON：{"selected":["F1","F3"]}，按原文顺序。')),
        dict(role='user',content='报告：'+title+'\n\n'+'\n\n'.join(rows))]


def parse_selection(response, frames):
    if response.get('finish_reason')!='stop':raise ValueError('要点选取未完整结束')
    obj=json.loads(response['text']);ids=obj.get('selected')
    allowed={f['id'] for f in frames}
    if (not isinstance(ids,list) or any(not isinstance(i,str) or i not in allowed for i in ids)
            or len(ids)!=len(set(ids))):raise ValueError('要点选择身份无效')
    return ids


def reading_plan(engine,title,frames):
    out=[];current=[]
    for f in frames:
        proposed=current+[f]
        if current and (len(proposed)>22 or engine.report_prompt_tokens(messages_for(title,proposed))+OUTPUT+MARGIN>engine.n_ctx):
            out.append(current);current=[]
        current.append(f)
        if engine.report_prompt_tokens(messages_for(title,current))+OUTPUT+MARGIN>engine.n_ctx:
            raise ValueError('完整条件/清单超过单次阅读预算，未切断原文；请按章节处理')
    if current:out.append(current)
    return out


def selection_units(frames):
    """Sentence groups selected as a unit; retain scope anchors and complete lists.

    This is compressive selection, not a claim that an extracted sentence is true.
    Whole enumerations and conditional alternatives remain indivisible.
    """
    from .report_source_frames import _flatten, _slice_ranges
    result=[]
    formal_topics={f.get('topic_hint') for f in frames if f.get('role')=='candidate'}
    for frame in frames:
        if frame.get('role') in ('quoted','narrative') or frame.get('is_structure_marker'): continue
        introduction = frame.get('role')=='unknown' and frame.get('topic_hint') in formal_topics
        text,positions=_flatten(frame['ranges'])
        compact=re.sub(r'\s+','',text)
        # Counted alternatives are atomic, but conversational "a few aspects"
        # and examples beginning "the first transaction" are not lists.
        list_block=bool(re.search(r'(?:共有|有|分为|包括)[一二三四五六七八九十两\d]+(?:种(?:方式|模式|类型|算法)|个阶段|项|步(?:骤)?)',compact)
                        or re.search(r'(?m)^\s*(?:[一二三四五六七八九十]+[、]|\d+[、）])',text))
        ends=[m.end() for m in re.finditer(r'[。！？]+[”’」』）)]*',text)]
        if not ends or list_block: spans=[(0,len(text))]
        else:
            if ends[-1]<len(text):ends[-1]=len(text)
            spans=[]; start=0
            for end in ends:
                if text[start:end].strip():spans.append((start,end))
                start=end
            # Pair conditional alternatives and their continuing clauses; never
            # sever 'if absent' from 'if present' or from a following restriction.
            grouped=[]
            for a,b in spans:
                v=text[a:b].strip()
                if grouped and re.match(r'^(?:否则|反之|相反|如果|若|只有|仅当|当.{0,35}时|但|而|所以|因此|此时|同时|后续|需要注意)',v):
                    grouped[-1]=(grouped[-1][0],b)
                else:grouped.append((a,b))
            spans=grouped
        anchor=None
        for a,b in spans:
            value=text[a:b].strip()
            # Standalone conversational salutations are retained in source, not facts.
            if not value or re.fullmatch(r'(?:好的[，,]?)?(?:面试官)?[。！!]*',re.sub(r'\s+','',value)):continue
            if introduction and not re.search(r'全称|简称|是指|指的是',value):continue
            # Pure closing transitions are source material, not knowledge claims.
            if re.fullmatch(r'以上就是.{0,40}(?:理解|回答)[。！!]*',re.sub(r'\s+','',value)):continue
            ranges=_slice_ranges(positions,a,b)
            sid=f'S{len(result)+1}'
            item=dict(frame,id=sid,text=value,ranges=ranges,parent_frame=frame['id'],anchor=anchor,
                      kind='list' if list_block else frame['kind'])
            if anchor is None:anchor=sid
            result.append(item)
    return result


def ensure_topic_coverage(frames,selected):
    chosen=set(selected);reasons=[];topics={}
    for f in frames:topics.setdefault(f.get('topic_hint') or f['parent_frame'],[]).append(f)
    for key,group in topics.items():
        if not any(f['id'] in chosen for f in group):
            chosen.add(group[0]['id']);reasons.append(dict(id=group[0]['id'],reason='topic_anchor'))
    # Coverage uses source-defined information roles, never document-specific
    # topic names or test IDs. It is a conservative omission guard, not truth proof.
    roles={
        'definition':r'全称|简称|是指|指的是',
        'mechanism':r'基于|通过|用到|实现原理|实现方法|工作流程',
        'condition':r'只有|仅|除非|如果|否则|当.{0,35}时',
        'purpose':r'目的|为了|作用|原因|避免|降低|防止',
        'contrast':r'区别|不同|反之|相反'}
    for topic,group in topics.items():
        identifiers=set(re.findall(r'[A-Za-z][A-Za-z0-9_]{2,}',topic))
        for name,pattern in roles.items():
            candidates=[f for f in group if re.search(pattern,f['text'])]
            if candidates and not any(f['id'] in chosen for f in candidates):
                def relevance(f):
                    return (sum(v in f['text'] for v in identifiers), -len(f['text']))
                best=max(candidates,key=relevance)
                chosen.add(best['id']);reasons.append(dict(id=best['id'],reason='source_role_'+name))
    # A role quota is not completeness: two conditional alternatives may both
    # belong to the same role. Keep every explicit boundary in an included
    # topic, including the negative case and finite-enumeration lead.
    boundary = re.compile(
        r'如果|否则|除非|只有|仅|只(?:需|能|在|有|允许)|'
        r'不(?:能|允许|支持|采用|适用)|不过|但是|然而|'
        r'当.{0,40}(?:时|的时候)|'
        r'(?:有|分为|包括)[一二三四五六七八九十两\d]+(?:个|种|项|步)[：:。]')
    for f in frames:
        if f['id'] not in chosen and boundary.search(re.sub(r'\s+', '', f['text'])):
            chosen.add(f['id']);reasons.append(dict(id=f['id'],reason='explicit_boundary'))
    # A later sentence cannot silently lose the subject/scope in its first sentence.
    for f in frames:
        if f['id'] in chosen and f.get('anchor') and f['anchor'] not in chosen:
            chosen.add(f['anchor']);reasons.append(dict(id=f['anchor'],reason='subject_anchor'))
    return [f['id'] for f in frames if f['id'] in chosen],reasons


def display_text(text):
    # Preserve spaces between Latin words while joining PDF CJK wrapping.
    text=re.sub(r'(?<=[A-Za-z])\s*\n\s*(?=[A-Za-z])',' ',text)
    return re.sub(r'\s*\n\s*','',text).strip()


def generate(engine,title,focus,units,register,cache_dir,stage):
    started=time.monotonic();calls=0;reused=0
    frames,omissions=source_frames(units)
    eligible=selection_units(frames)
    by_unit={u['unit_id']:u for u in units}
    cache_dir=Path(cache_dir)/VERSION
    stages=[];chosen=[];trace=[];repairs=[]
    stage('正在保留原文主题、条件和完整清单')
    with engine.session():
        batches=reading_plan(engine,title,eligible)
        import importlib.metadata
        llm=engine._llm
        stat=engine.model_path.stat()
        fingerprint=dict(protocol=VERSION,model=str(engine.model_path),size=stat.st_size,mtime=stat.st_mtime_ns,
            runtime=importlib.metadata.version('llama-cpp-python'),template=llm.metadata.get('tokenizer.chat_template'),
            sampling=dict(temperature=0,seed=0),n_ctx=engine.n_ctx,output=OUTPUT,
            revisions=[(u['unit_id'],u['revision']) for u in units])
        for index,batch in enumerate(batches,1):
            stage(f'正在组织知识要点 {index}/{len(batches)} · 完整条件与清单自动保留')
            messages=messages_for(title,batch)
            schema={'type':'object','properties':{'selected':{'type':'array','items':{'type':'string','enum':[f['id'] for f in batch]},'maxItems':len(batch)}},'required':['selected'],'additionalProperties':False}
            key=hashlib.sha256(json.dumps([fingerprint,messages,schema],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            path=cache_dir/(key+'.json');cached=False
            if path.is_file():
                saved=json.loads(path.read_text('utf-8'));response=saved['response']
                if saved.get('key')!=key:raise ValueError('进度身份不一致')
                parse_selection(response,batch);cached=True;reused+=1
            else:
                if time.monotonic()-started>3600:raise ValueError('本次长文稿已到时间上限；已完成选材保存，可继续')
                response=engine.report_completion(messages,OUTPUT,schema=schema);calls+=1
                atomic_json(path,dict(key=key,response=response,messages=messages,schema=schema,fingerprint=fingerprint))
            selected=parse_selection(response,batch)
            chosen+=selected
            trace.append(dict(id=index,cache_key=key,cached=cached,usage=response.get('usage'),seconds=response.get('seconds'),
                              input_frame_ids=[f['id'] for f in batch],selected=selected))
            stages.append(dict(id=index,status='complete',points=[],source_units=list(dict.fromkeys(r['unit_id'] for f in batch for r in f['ranges']))))
    selected,repairs=ensure_topic_coverage(eligible,chosen)
    selected_set=set(selected);output=[];claims=[];sections={};seen={};topics={};source_only=[]
    for f in eligible:
        if f['id'] not in selected_set:continue
        text=display_text(f['text'])
        if not text:continue
        refs=[register(by_unit[r['unit_id']],r['start'],r['end']) for r in f['ranges']]
        topic=display_text(f.get('topic_hint') or '知识要点')
        if re.fullmatch(r'(?:https?://[^\s]+\s*)+', text):
            source_only.append(dict(topic=topic,refs=refs,reason='来源仅含链接，未提供可归纳正文'))
            continue
        identity=(topic,re.sub(r'\s+','',text))
        if identity in seen:
            existing=seen[identity];existing['refs']=list(dict.fromkeys(existing['refs']+refs));continue
        point=dict(topic='',text=text,refs=list(dict.fromkeys(refs)),source_ids=[f['id']],kind=f['kind'])
        seen[identity]=point
        if topic not in topics:
            entry=dict(id=len(output)+1,heading=topic,points=[],status='complete',source_units=[])
            output.append(entry);topics[topic]=entry;sections[topic]=[entry['id']]
        entry=topics[topic];entry['points'].append(point)
        entry['source_units']=list(dict.fromkeys(entry['source_units']+[r['unit_id'] for r in f['ranges']]))
        claims.append(dict(text=text,topic=topic,ref=refs[0],refs=point['refs'],origin='本地模型选编 · 原文事实约束'))
    organization=dict(status='source_topic_order',labels_from_source=True)
    final_sections=[dict(title=t,batch_ids=i) for t,i in sections.items()]
    # Unverified model taxonomy can misclassify concepts even with valid IDs.
    # Keep the source's own headings and order; no invented upper-level class.
    read_units={r['unit_id'] for f in eligible for r in f['ranges']}
    digest=dict(protocol='whole-document-v1',method=VERSION,sections=final_sections,
        batches=output,map_batches=stages,trace=trace,source_frames=frames,selection_units=eligible,omissions=omissions,
        selection=dict(model_selected=chosen,selected=selected,dependency_restored=repairs,
                       not_selected=[f['id'] for f in eligible if f['id'] not in selected_set]),
        coverage=dict(total_units=len(units),read_units=len(read_units),excluded_units=len(units)-len(read_units),processed_units=len(units),
                      completed_batches=len(batches),total_batches=len(batches)),
        organization=organization,source_only=source_only)
    return digest,claims,dict(calls=calls,reused_calls=reused,seconds=round(time.monotonic()-started,3),
                             model_blocks=len(eligible),raw_claims=[],rejected=[],format_recovered=[])
