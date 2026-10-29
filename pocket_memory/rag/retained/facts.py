# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Conservative, source-bound records; no LLM, question IDs, gold or answer repair.

Only explicit one-note scopes are eligible. Unproved relations return to the
unchanged retained pipeline. A direct answer and its source are one object.
"""
import copy
import hashlib
import re
import unicodedata
from pathlib import Path

VERSION = 'bound-original-records-v1'
from .structure import SectionIndex

def norm(text):
    return re.sub(r'[\s“”"\'，,。？?：:、]', '', unicodedata.normalize('NFKC', text))

def span(text, a, b):
    assert 0 <= a < b <= len(text)
    return dict(field='content', char_start=a, char_end_exclusive=b, quote=text[a:b])

def lines(text):
    return [(m.start(), m.end(), m.group().strip()) for m in re.finditer(r'[^\r\n]+', text)]

def plain_question(text):
    text = norm(text).strip('!！呀啊呢吧哈')
    text = re.sub(r'^(?:麻烦)?(?:请|帮我)?(?:问一下|问|告诉我|查一下|核对一下|看一下|我想知道)', '', text)
    return text

def single_scope(question, notes):
    # Source title is an explicit source expression, never inferred from scores.
    m = re.fullmatch(r'\s*(?:请)?(?:只|仅)?(?:根据|按|看)\s*《([^》]+)》(?:(?:的)?[“"]?(首页|页签\d+)[”"]?)?(?:正文)?(?:回答)?[：:，,]?\s*(.+)', question, re.S)
    if not m or '《' in m[3]:
        return None, None, 'not_one_explicit_source_expression'
    found = [n for n in notes.values() if norm(n['title']) == norm(m[1])]
    if len(found) != 1:
        return None, None, 'source_title_not_unique'
    n = found[0]; children = [v for v in notes.values() if v.get('parent_note_id') == n['id']]
    if m[2] and m[2] != '首页':
        matches = [v for v in children if norm(m[2]) in (norm(v.get('tab_name') or ''), norm(v['title']))]
        if len(matches) != 1:
            return None, None, 'member_missing_or_ambiguous'
        n = matches[0]
    elif children and m[2] != '首页':
        return None, None, 'unspecified_multi_tab_scope'
    return n, plain_question(m[3]), None

def year_ranges(text):
    result = []; boundaries = []
    for a, b, value in lines(text):
        raw = re.sub(r'^[一二三四五六七八九十]+[、.．]\s*', '', value)
        boundary = re.match(r'^(\d{4})年', raw)
        if boundary:boundaries.append((a,boundary[1]))
        if re.match(r'^今年.{0,30}(?:目标|计划|安排|任务)',raw):boundaries.append((a,None))
        m = re.fullmatch(r'(\d{4})年[^。！？；;]{0,22}(?:回顾|总结|工作|安排|任务|目标|展望)', raw)
        if m:
            result.append(dict(year=m[1], start=a, end=b, heading=value))
    for i, item in enumerate(result):
        next_heading=result[i+1]['start'] if i+1<len(result) else len(text)
        item['scope_end'] = min(next_heading,next((a for a,y in boundaries if a>item['start'] and y!=item['year']),len(text)))
    return result

def annual_numeric(q, text):
    years = re.findall(r'(?<!\d)(\d{4})年', q)
    if len(years) != 1 or re.search(r'累计|合计|总共|过去|平均每|分别|与|和|及|比较|还是|是否|多少倍|个百分点|同比上升|同比下降', q):
        return None, 'not_single_explicit_annual_metric'
    year = years[0]
    # Keep the metric itself exact; only grammatical quantity suffixes differ.
    rest = q.split(year+'年', 1)[1]
    if q.split(year+'年', 1)[0]:
        return None, 'unparsed_prefix_before_year'
    city = re.match(r'^([^的]{2,8}市)的?', rest)
    subject = None
    if city:
        subject = city[1]; rest = rest[city.end():]
    else:
        rest = re.sub(r'^(?:全市|我市|本市)的?', '', rest)
    rest = rest.replace('的', '')
    growth = re.fullmatch(r'(.{3,24})增长(?:了)?(?:多少|百分之多少)', rest)
    ordinary = re.fullmatch(r'(.{3,24}?)(?:是多少|有多少|为多少)', rest)
    if not growth and not ordinary:
        return None, 'unparsed_numeric_question'
    metric = (growth or ordinary)[1]
    aliases = [metric]
    if metric.endswith('人数'):
        aliases.append(metric[:-2])
    if any(re.search(r'原告|被告|赔偿|请求|计划|目标|预计|预算|增长|增幅|相比', v) for v in aliases):
        return None, 'unsupported_assertion_role_or_metric'
    ranges = year_ranges(text); candidates = []
    # A finite physical paragraph and its certified annual heading are retained.
    for a, b, paragraph in lines(text):
        region = text[a:b]
        if re.search(r'仅|只限|不含|不包括|试点|部分地区|部分企业|若|如果|假设|在.{0,12}条件下',region):
            continue
        if subject:
            city_name=re.escape(subject.removesuffix('市'))
            paragraph_subject=bool(re.match(r'\s*'+year+r'年[，,]?\s*(?:是)?'+city_name+r'(?:市)?(?:历史|经济|全市)',region))
            # A document mention cannot override a different paragraph subject.
            city_mentions=re.findall(r'[\u4e00-\u9fff]{2,8}市',region)
            conflict=any(subject.removesuffix('市') not in city for city in city_mentions)
            if not paragraph_subject or conflict:
                continue
        local_years = set(re.findall(r'(?<!\d)(\d{4})年', region))
        parent = next((h for h in ranges if h['end'] <= a < h['scope_end']), None)
        if local_years:
            if local_years != {year}:
                continue
            annual = None
        elif parent and parent['year'] == year and re.search(r'回顾|总结', parent['heading']):
            annual = parent
        else:
            continue
        if re.search(r'主要预期目标|主要目标(?:是|为)|(?:计划|拟|力争)(?:实现|达到|完成|增长|新增)|申请|请求|诉称|辩称|判决|认定|到\d{4}年|截至', region):
            continue
        for part in re.finditer(r'[^。！？；;]+', region):
            piece = part.group()
            for alias in aliases:
                # The field starts at a clause boundary, not in another longer field.
                pat = re.compile(r'(?:^|[，,、])\s*(?:其中)?(?:(?:全市|我市|本市|全县|全省|全区))?'+re.escape(alias)+r'(?:为|达到|达|是)?\s*(\d+(?:\.\d+)?)\s*(亿元|万元|万人|万户|千人|千户|元|人|户|个|%|％)')
                for m in pat.finditer(piece):
                    unit = m[2]
                    if metric.endswith('人数') and not unit.endswith('人'):
                        continue
                    end = m.end()
                    if growth:
                        tail = re.match(r'[、，,\s]*(?:(实际|同比|比上年|年均))?增长\s*(\d+(?:\.\d+)?)\s*[%％]', piece[end:])
                        if not tail:
                            continue
                        end += tail.end(); value = tail[2]+'%'; qualifier = tail[1] or ''
                    else:
                        value = m[1]+unit; qualifier = ''
                    qualifier_tail=re.match(r'(?:左右|以上|以下|以内|以外|余|多)?',piece[end:])[0]
                    end+=len(qualifier_tail);value+=qualifier_tail
                    if re.match(r'\s*(?:至|到|或|[-—～~/])',piece[end:]):
                        continue
                    start = a + part.start() + m.start()
                    while start < a + part.start()+end and text[start] in '，,、 \t\u3000':start += 1
                    stop = a + part.start()+end
                    sentence_start=max(region.rfind('。',0,part.start()),region.rfind('！',0,part.start()),region.rfind('？',0,part.start()))+1
                    estimated=bool(re.search(r'预计|估计',region[sentence_start:part.start()+m.start()]))
                    # Direct time in a paragraph must be a leading scope, not a comparison elsewhere.
                    if local_years and not re.match(r'\s*(?:据统计[，,])?'+year+r'年', region):
                        continue
                    raws = ([span(text, annual['start'], annual['end'])] if annual else []) + [span(text, a, b)]
                    candidates.append(dict(kind='annual_metric', metric=metric, value=value, year=year,
                        subject=subject or '指定资料的报告主体', qualifier=qualifier,
                        statement_status='original_estimate' if estimated else 'original_reported_value',
                        value_quote=span(text, start, stop), raw_evidence=raws,
                        proof='exact metric clause + unique explicit annual scope',
                        answer=('按原文预计口径，' if estimated else '')+f'{year}年，{text[start:stop]}。'))
    # Multiple occurrences are not voted or selected by answer similarity.
    unique = {(c['value_quote']['char_start'], c['value_quote']['char_end_exclusive']):c for c in candidates}
    if len(unique) != 1:
        return None, 'metric_occurrence_not_unique' if unique else 'no_unique_bound_annual_metric'
    return next(iter(unique.values())), None

def directory_record(q, text):
    form=r'(?:本报告从(?:几个方面|多少个方面|哪些方面|哪几个方面)总结(?:了)?工作|(?:本报告的)?(?:工作回顾|工作总结)(?:包括|包含|列出|涉及)(?:几个方面|多少个方面|哪些方面|哪几个方面))'
    if not re.fullmatch(form,q):
        return None, 'not_explicit_review_directory_question'
    if re.search(r'每个|具体|原因|效果|比较|建议|措施|阶段|预算', q):
        return None, 'directory_body_or_other_relation_requested'
    tree = SectionIndex(text)
    parents = [h for h in tree.headings if re.search(r'工作回顾|工作总结', h['title']) and h['children_ids']]
    if len(parents) != 1:
        return None, 'review_parent_not_unique'
    parent = parents[0]; children = [tree._by_id[id] for id in parent['children_ids']]
    reason = tree._chinese_parent_reason(parent, children)
    if reason or len(children) > 12:
        return None, reason or 'directory_too_large'
    # Every parenthetical marker inside the parent must resolve to these children.
    markers = list(re.finditer(r'(?m)^[ \t\u3000]*[（(]\s*([一二三四五六七八九十]+)\s*[）)]', text[parent['end']:parent['section_end']]))
    if len(markers) != len(children):
        return None, 'unresolved_or_nested_parenthetical_marker'
    raw = [span(text, parent['start'],parent['end'])] + [span(text,h['start'],h['end']) for h in children]
    titles = [h['title'] for h in children]
    answer=f'“{parent["title"]}”共列出{len(children)}个方面。'
    if re.search(r'哪些|哪几|列出|分别', q):answer += '\n'+'\n'.join(titles)
    return dict(kind='complete_directory', metric='完整同级目录', value=len(children), titles=titles,
        raw_evidence=raw, proof='unique explicit review parent + complete contiguous child sequence + boundary audit',answer=answer), None

def key_value_record(q, text):
    m = re.fullmatch(r'(?:这份|该|本)?(?:资料|文档|技术规范)?(?:的)?([\u4e00-\u9fffA-Za-z_]{2,20})(?:是什么|是多少|为何值)', q)
    if not m:
        return None,'not_exact_key_value_question'
    key=m[1];hits=[]
    for a,b,v in lines(text):
        r=re.fullmatch(r'\s*'+re.escape(key)+r'\s*[：:]\s*([^\r\n]{1,100})\s*',text[a:b])
        if r and not re.search(r'[|<>]|原告|被告|申请|建议|预计|目标|例如|示例',r[1]):
            hits.append(dict(kind='explicit_field',metric=key,value=r[1],raw_evidence=[span(text,a,b)],
                proof='exact question field = unique standalone original key-value field',answer=f'{key}：{r[1]}。'))
    if len(hits)!=1:return None,'field_not_unique_or_not_standalone'
    # Object rows require their established row/column protocol, not this route.
    if re.search(r'\|[^\n]*\||<table|<tr',text,re.I):return None,'table_uses_retained_contract'
    return hits[0],None

class BoundFacts:
    def __init__(self,notes):self.notes=notes;self.audit={}
    def prepare(self,question):
        note,q,why=single_scope(question,self.notes)
        self.audit=dict(version=VERSION,active=False,question=question,reason=why,checks=[])
        if why:return None
        self.audit.update(note_id=note['id'],normalized_question=q)
        text=note.get('content')
        if not isinstance(text,str) or not text.strip():self.audit['reason']='no_original_body';return None
        if re.search(r'<(?:table|tr)|\|[^\n]+\||\S[^\r\n]*\t[^\r\n]*\S',text,re.I):
            self.audit['reason']='table_uses_retained_contract';return None
        for rule in (annual_numeric,directory_record,key_value_record):
            record,why=rule(q,text);self.audit['checks'].append(dict(rule=rule.__name__,reason=why))
            if record:
                source=dict(index=1,source_type='note',source_id=note['id'],source_key=f'note:{note["id"]}',
                    title=note['title'],kind='content',parent_note_id=note.get('parent_note_id'),tab_name=note.get('tab_name'),
                    evidence='\n'.join(r['quote'] for r in record['raw_evidence']),raw_evidence=record['raw_evidence'],
                    location='原文 '+', '.join(f'{r["char_start"]}–{r["char_end_exclusive"]}' for r in record['raw_evidence']),
                    coverage_complete=True,reason='原始结构唯一绑定：'+record['proof'])
                # All values and claims remain attached to exact source revision.
                record.update(source_id=note['id'],parent_note_id=note.get('parent_note_id'),tab_name=note.get('tab_name'),
                    source_revision=hashlib.sha256(text.encode()).hexdigest())
                self.audit.update(active=True,reason=None,record=copy.deepcopy(record))
                response=dict(answer=record['answer']+' [1]',sources=[source],provided_sources=[copy.deepcopy(source)],
                    answer_status='answered',citation_validation=dict(passed=True,errors=[]),
                    trace=dict(bound_facts=copy.deepcopy(self.audit),generation_calls=0,
                               rendering='original structure-bound record; no generated prose',messages=[]))
                return dict(mode='single_free_text',sources=[source],direct_record_response=response)
        self.audit['reason']='no_complete_unique_record';return None
