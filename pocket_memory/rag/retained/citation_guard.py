# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Keep validated answer generation, verify labels, repair at most once.

No numeric guess/clamping/nearest citation. A repair explicitly asks the local
model to bind the unchanged answer to existing evidence, with exact quotes.
Semantic support remains a separately reviewed property.
"""
import json
import re
from .citation_protocol import source_map
from .citation_protocol_first_attempt import validate as validate_quoted

CITE=re.compile(r'[\[【]([\d,，、\s–—-]+)[\]】]')

def block_indices(block):
    values=[]
    for token in re.findall(r'\d+\s*[-–—]\s*\d+|\d+',block):
        bounds=re.findall(r'\d+',token)
        if len(bounds)==2:
            start,end=map(int,bounds)
            values.extend(range(start,end+1) if start<=end and end-start<1000 else [0])
        else: values.append(int(token))
    return values

def indices(answer):
    return list(dict.fromkeys(i for block in CITE.findall(answer) for i in block_indices(block)))

def relabel(answer,refs):
    mapping={source['index']:position+1 for position,source in enumerate(refs)}
    def replacement(match):
        return ''.join(f'[{mapping[i]}]' for i in block_indices(match.group(1)))
    return CITE.sub(replacement,answer),[{**source,'index':position+1} for position,source in enumerate(refs)]

def repair_schema(keys):
    return {'type':'object','properties':{'evidence':{'type':'array','minItems':1,'maxItems':3,
      'items':{'type':'object','properties':{'source_key':{'type':'string','enum':list(keys)},
      'quote':{'type':'string'}},'required':['source_key','quote'],'additionalProperties':False}}},
      'required':['evidence'],'additionalProperties':False}

def apply_guard(response,repair_callback=None):
    answer=response['answer']
    supplied=response['provided_sources']
    valid={source['index']:source for source in supplied}
    cited=indices(answer)
    invalid=[i for i in cited if i not in valid]
    response['trace']['answer_before_citation_guard']=answer
    response['trace']['citation_guard']={'original_indices':cited,'invalid_indices':invalid,'repair_calls':0}
    if not invalid:
        refs=[valid[i] for i in cited]
        response['answer'],response['sources']=relabel(answer,refs)
        response['answer_status']='answer' if cited else 'no_citation'
        response['citation_validation']={'passed':True,'errors':[],
            'scope':'all displayed references resolve; absence of references and semantic support require separate review'}
        return response
    source_keys=source_map(supplied)
    text=CITE.sub('',answer).strip()
    payload=None
    repair_trace={}
    if repair_callback:
        try:
            payload,repair_trace=repair_callback(text,source_keys)
        except Exception as exc:
            payload=None
            repair_trace={'error':type(exc).__name__+': '+str(exc),'finish_reason':'error'}
        response['trace']['citation_guard']['repair_calls']=1
        response['trace']['citation_guard']['repair']=repair_trace
    wrapped={'status':'answer','claims':[{'text':text,'evidence':payload.get('evidence',[]) if isinstance(payload,dict) else []}]}
    # Markdown table separators can disappear when copying a row. Remove only
    # those structural pipes for occurrence checking; never fuzzy-match words,
    # numbers or punctuation. Display still uses the untouched supplied source.
    canonical_sources={k:dict(s) for k,s in source_keys.items()}
    canonical_wrapped=json.loads(json.dumps(wrapped,ensure_ascii=False))
    normalized=[]
    for ref in canonical_wrapped['claims'][0]['evidence']:
        if not isinstance(ref,dict): continue
        key=ref.get('source_key')
        if key in canonical_sources and any(line.strip().startswith('|') and line.count('|')>=3 for line in canonical_sources[key]['evidence'].splitlines()):
            canonical_sources[key]['evidence']=canonical_sources[key]['evidence'].replace('|','')
            if isinstance(ref.get('quote'),str): ref['quote']=ref['quote'].replace('|','')
            normalized.append(key)
    response['trace']['citation_guard']['table_separator_normalization']=normalized
    errors=validate_quoted(canonical_wrapped,canonical_sources)
    if repair_trace.get('finish_reason','stop')!='stop': errors.append('repair_not_completed')
    if errors:
        response.update(answer='这次回答的引用未通过校验，暂不展示未经核对的结论。可先查看检索到的原文。',
           sources=[],answer_status='citation_check_failed',retrieved_evidence=supplied,
           citation_validation={'passed':False,'errors':errors})
        return response
    keys=list(dict.fromkeys(item['source_key'] for item in payload['evidence']))
    # Evidence remains the actual supplied text, not the model's quote or a guess.
    response['sources']=[{**source_keys[key],'index':i+1,'source_key':key} for i,key in enumerate(keys)]
    response['answer']=text+' '+''.join(f'[{i+1}]' for i in range(len(keys)))
    response['answer_status']='answer_citations_rebound'
    response['citation_validation']={'passed':True,'errors':[],'repaired':True,
       'scope':'one explicit local binding pass; source key and normalized quote occurrence checked (whitespace/table separators); original factual text preserved; semantic support separately reviewed'}
    return response

def local_repair(engine,text,sources):
    context='\n\n'.join(f'{key}: {s["title"]} / {s["location"]}\n{s["evidence"]}' for key,s in sources.items())
    messages=[{'role':'system','content':
        '你只做引用校对，不改写答案。给定答案的编号无效，请为答案中的全部事实选择支持证据。'
        '只输出JSON，evidence数组每项有source_key和quote。source_key从给定SOURCE标识选择，'
        'quote必须是该标识下连续的原文，覆盖答案中相关事实；不能将另一个证据的原文配到此标识。'
        '表格请摘包含对象、核心功能、输入资料等所问字段的完整一行。多段答案需要选全支持它们的证据。'},
        {'role':'user','content':f'待核对的答案：\n{text}\n\n可用证据：\n{context}'}]
    generated=engine._llm.create_chat_completion(messages=messages,max_tokens=384,temperature=0,
                 response_format={'type':'json_object','schema':repair_schema(sources)})
    choice=generated['choices'][0]; raw=choice['message']['content'] or ''
    try: payload=json.loads(raw)
    except json.JSONDecodeError: payload=None
    return payload,{'messages':messages,'raw':raw,'usage':generated.get('usage'),'finish_reason':choice['finish_reason']}
