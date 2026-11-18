# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Bounded source actions and comparison operands; never rewrite the question.

Offsets refer to original Unicode text. Unsupported relations are not projected.
This grammar is not a general natural-language or semantic-scope classifier.
"""
import re
import unicodedata

ORIGINAL_REQUEST_VERSION = 'original-label-request-v1'
LABEL_CONNECTORS = ('以及', '和', '与', '及', '、')


def original_label_request(question, labels, notes):
    """Bind the complete request to source labels, shared by prose projections.

    Scope masking consumes only metadata/source-action syntax already owned by
    the scope resolver. Every other character must belong to this grammar.
    No keyword-only admission, inferred subject, filtering or semantic aliases.
    Multiple literal labels are allowed; each producer decides which source
    structure it can prove. Ambiguous label/connector partitions fall back.
    """
    # field_projection imports the field contracts in this module. Resolve the
    # source helpers at call time to avoid a module initialization cycle.
    from .field_projection import strip_scopes
    from .source_scope import norm

    names = {norm(label): label for label in labels}
    if not names or len(names) != len(labels) or '' in names:
        return None
    request = norm(strip_scopes(question, notes))
    terminal = request.rstrip('？?。！!')
    requests = [terminal]
    if terminal.endswith(('呢', '呀', '啊')):
        requests.append(terminal[:-1])
    prefix = (r'(?:(?:请问|请|帮我)?(?:只根据|只依据|根据|依据|只看|只读|参考)?)'
              r'[，,:：]*(?:(?:项目|笔记|报告|资料|正文)?(?:里面|中|里|内)?'
              r'(?:提到|提及|记录|列出|描述)?的?)')
    forms = (
        r'(?:有|存在|包括|包含)?(?:哪些|哪几项|哪几条|哪几个|什么)(?P<fields>.+)',
        r'(?P<fields>.+?)(?:分别)?(?:有|包括|包含)?'
        r'(?:哪些|哪几项|哪几条|哪几个|是什么内容|是什么|是哪些|有哪些|有什么内容|有什么|是谁)',
        r'(?:请)?(?:列出|列举)的?(?P<fields>.+)',
    )
    candidates = set()

    def literal_fields(text, start, selected=()):
        if len(selected) >= 8:
            return
        for key in names:
            if not text.startswith(key, start) or key in selected:
                continue
            end = start + len(key)
            found = selected + (key,)
            yield end, found
            for connector in LABEL_CONNECTORS:
                if text.startswith(connector, end):
                    yield from literal_fields(text, end + len(connector), found)

    for text in requests:
        # Bind literal labels before applying scaffolding. A single greedy
        # regex capture could consume 项目 in a literal 项目输入 label, or 有
        # at the end of a label, and hide another valid partition.
        for start in range(len(text)):
            for end, found in literal_fields(text, start):
                field = r'(?:全部|所有)?' + re.escape(text[start:end])
                for form in forms:
                    exact = form.replace('(?P<fields>.+?)', field).replace('(?P<fields>.+)', field)
                    if re.fullmatch(prefix + exact, text):
                        candidates.add(found)
    if len(candidates) != 1:
        return None
    return dict(version=ORIGINAL_REQUEST_VERSION, question=question,
                normalized_request=request,
                labels=[names[key] for key in next(iter(candidates))],
                selection='all_original_members', filters=[], residual='')

FIELD = r'(?:(?:已(?:经)?保存的?|保存的?)?\s*)(?P<field>OCR(?:文本)?|正文)(?:里|中)?'
ACTION = re.compile(
    r'(?P<action>(?:不要|不能|禁止|不得|别|不)(?:读取|使用|采用|参考|依据|引用|援引|检索|查|读|用|看)|'
    r'(?:只|仅)(?:读取|使用|采用|参考|依据|引用|援引|根据|读|用|看|按)|读取|使用|采用|参考|忽略|无需)'
    r'[\s的]*' + FIELD, re.I)
IMPLICIT_OCR = re.compile(r'(?:已经|已)?保存的OCR(?:文本)?|'
    r'(?:的OCR|OCR文本)(?:里|中|[，,。?？]|$)|OCR(?:里|中)', re.I)

def field_actions(text, offset=0, bound_source=False):
    actions=[]
    for m in ACTION.finditer(text):
        verb=m['action']; field='ocr_text' if m['field'].lower().startswith('ocr') else 'content'
        # "Ignore conditions in OCR" is not the same as excluding the OCR field.
        if verb=='忽略' and re.match(r'(?:的|中|里)',text[m.end():]):continue
        polarity=('neutral' if verb=='无需' else
                  'exclude' if re.match(r'不要|不能|禁止|不得|别|不|忽略',verb) else 'allow')
        operation='cite' if re.search(r'引用|援引',verb) else 'read'
        actions.append(dict(start=offset+m.start(),end=offset+m.end(),field=field,
                            polarity=polarity,action=verb,operation=operation))
    for m in IMPLICIT_OCR.finditer(text):
        if any(a['start'] < offset+m.end() and a['end'] > offset+m.start() for a in actions):continue
        # "不要省略 OCR 中的条件" concerns answer completeness, not source access.
        prefix=re.split(r'[，,。?？;；]',text[:m.start()])[-1]
        if re.search(r'(?:不要|不能|不得|不)(?:省略|遗漏|忽略)',prefix):continue
        if re.search(r'不要|不能|不得|禁止|别|无需',prefix):
            actions.append(dict(start=offset+m.start(),end=offset+m.end(),field='ocr_text',
                                polarity='unresolved',action='unparsed_negative_source',operation='unknown'))
            continue
        # Mentioning OCR as a topic is not an instruction to read saved OCR.
        if not bound_source and not re.search(r'保存的?OCR',m[0],re.I):continue
        actions.append(dict(start=offset+m.start(),end=offset+m.end(),field='ocr_text',
                            polarity='allow',action='saved_field_reference',operation='read'))
    allowed={a['field'] for a in actions if a['polarity']=='allow'}
    excluded={a['field'] for a in actions if a['polarity']=='exclude'}
    conflict=bool(allowed & excluded) or any(a['polarity']=='unresolved' for a in actions)
    if not allowed:allowed={'content'}
    allowed-=excluded
    return allowed,excluded,actions,conflict or not allowed


def endpoint_projection(q, fields, entities, positions):
    """Bind explicit successive entity/property groups, not their Cartesian product.

    A shared subject group (A and B's field) remains the rectangular old path.
    This endpoint path requires one named entity per group. A comparison requires
    exactly two field mentions in its clause and two different named entities.
    """
    if not fields or not entities or min(a for a,b,h in fields)>=max(a for a,b,n in entities):return None
    bound=[];previous=-1;group=[]
    for a,b,h in sorted(fields):
        next_group=[e for e in sorted(entities) if previous<e[0] and e[1]<=a]
        if next_group:group=next_group
        if len(group)!=1:return None
        ea,eb,name=group[0]
        bound.append(dict(entity=name,field=h,entity_span=original_span(positions,ea,eb),
                          field_span=original_span(positions,a,b),start=a,end=b))
        previous=b
    operations=[]
    for m in re.finditer(r'一样|相同|一致|差异',q):
        left=max((i for i,c in enumerate(q[:m.start()]) if c in ',，;；?？。'),default=-1)+1
        cells=[v for v in bound if left<=v['start'] and v['end']<=m.start()]
        if len(cells)!=2 or cells[0]['entity']==cells[1]['entity']:return None
        operations.append(dict(kind='cross_cell_compare',operands=[
            {k:v for k,v in c.items() if k not in ('start','end')} for c in cells],
            predicate_span=original_span(positions,m.start(),m.end())))
    projections=[]
    for cell in bound:
        item=next((p for p in projections if p['entity']==cell['entity']),None)
        if item is None:item=dict(entity=cell['entity'],fields=[]);projections.append(item)
        if cell['field'] not in item['fields']:item['fields'].append(cell['field'])
    return dict(projections=projections,operations=operations)

def normalized_positions(text):
    chars=[];positions=[]
    for i,c in enumerate(text):
        for v in unicodedata.normalize('NFKC',c).casefold():
            if not v.isspace():chars.append(v);positions.append(i)
    return ''.join(chars),positions

def original_span(positions,a,b):
    return [positions[a],positions[b-1]+1]

def bind_operations(q, field_spans, entity_spans, entities, positions):
    """Only rectangular projections and explicit same-column comparisons.

    A property before a later entity implies endpoint-specific projection; do
    not transform it into every entity x every property. Repeated field mentions
    retain their own predicate scope rather than joining a global field set.
    """
    if len(set(entities))>1 and entity_spans and field_spans:
        if min(a for a,b,h in field_spans) < max(a for a,b,n in entity_spans):
            return None
    operations=[]
    for m in re.finditer(r'一样|相同|一致|差异',q):
        left=max((i for i,c in enumerate(q[:m.start()]) if c in ',，;；?？。'),default=-1)+1
        operands=[(a,b,h) for a,b,h in field_spans if left<=a and b<=m.start()]
        if not operands or len(set(entities))<2:return None
        for a,b,h in operands:
            operations.append(dict(kind='same_field_across_entities',field=h,
                entities=list(dict.fromkeys(entities)),field_span=original_span(positions,a,b),
                predicate_span=original_span(positions,m.start(),m.end())))
    return operations
