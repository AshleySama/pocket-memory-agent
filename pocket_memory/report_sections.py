# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Source-derived report views. Structure is visual, never a causal/score claim."""
from __future__ import annotations
from html import unescape
import re
from collections import Counter


def readable(text):
    # Only a display projection: references always retain the untouched field.
    text = re.sub(r'<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)>', '', text, flags=re.I)
    text = re.sub(r'<br\s*/?>|</(?:p|div|li)>', '\n', text, flags=re.I)
    return unescape(re.sub(r'<[^>]+>', '', text)).strip()


def quote_span(block, quote, claim):
    """Return an original span. Never fuzzy-match words, numbers or punctuation."""
    if len(quote)>=8 and quote in block:
        a=block.index(quote);return a,a+len(quote),'exact'
    if len(claim)>=12 and block.count(claim)==1:
        a=block.index(claim);return a,a+len(claim),'claim_is_verbatim'
    def inline_code_projection(text):
        ignored=set()
        for m in re.finditer(r'`([^`\n]+)`',text):
            ignored.update((m.start(),m.end()-1))
        indices=[i for i in range(len(text)) if i not in ignored]
        return ''.join(text[i] for i in indices),indices
    plain,indices=inline_code_projection(block)
    wanted,_=inline_code_projection(quote)
    if len(wanted)>=12 and plain.count(wanted)==1:
        a=plain.index(wanted);start=indices[a];end=indices[a+len(wanted)-1]+1
        # Include a paired formatting delimiter adjacent to the matched text.
        if start>0 and start-1 not in indices:start-=1
        if end<len(block) and end not in indices:end+=1
        return start,end,'inline_code_delimiters'
    return None


def groups(records):
    """Explicit Markdown headings and lists, and source-delimited prose groups."""
    out=[]; heading_by_source={}
    for record in records:
        if record['field']=='ocr_text': continue
        text=readable(record['text'])
        source=(record.get('unit_id',record['title']))
        heading=heading_by_source.get(source,record['title'])
        lines=text.splitlines()
        if len(lines)>1 and len(lines[0])<40 and lines[0].endswith(('：',':')):
            heading=lines[0].rstrip('：:')
            text='\n'.join(lines[1:]).strip()
        if len(lines)==1 and (re.fullmatch(r'#{1,6}\s+.+',text) or (text.endswith(('：',':')) and len(text)<40)):
            heading=re.sub(r'^#+\s*','',text).rstrip('：:')
            heading_by_source[source]=heading
            continue
        items=[]
        markers=list(re.finditer(r'(?:^|\n)\s*(?:[-*]\s+|\d+[.、．）)]\s*)',text))
        inline=list(re.finditer(r'第[一二三四五六七八九十]+[，、]',text))
        matches=markers if len(markers)>=2 else inline if len(inline)>=2 else []
        if matches:
            intro=text[:matches[0].start()].strip()
            if intro: heading=intro
            items=[text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)].strip() for i,m in enumerate(matches)]
        elif '；' in text:
            # Sentence boundaries stay visible; no inferred classification.
            items=[s.strip() for s in re.split(r'[；]',text) if s.strip()]
            if len(items)<2: items=[]
        if items:
            out.append({'title':heading,'items':items,'ref':record['ref'],'source':record['title'],'field':record['field']})
    return out


def flow_groups(records):
    """Only show direction that is explicitly written with arrows or 先/再/最后."""
    flows=[]
    for record in records:
        if record['field']=='ocr_text': continue
        text=readable(record['text'])
        # Arrow chains are bounded by blank lines or a subsequent prose sentence.
        for m in re.finditer(r'(?m)^[ \t]*[A-Za-z_][^\n]*(?:\n[ \t]*(?:->|→)[^\n]*)+',text):
            nodes=[s.strip() for s in re.split(r'\s*(?:->|→)\s*',m.group())]
            if 2<=len(nodes)<=12: flows.append({'title':'原文关联链路','items':nodes,'ref':record['ref']})
        m=re.search(r'先([^。；\n]+?)[，,]再([^。；\n]+?)[，,]最后([^。；\n]+)',text)
        if m: flows.append({'title':'原文明确的处理顺序','items':list(m.groups()),'ref':record['ref']})
        m=re.search(r'([^\n：:]{0,30}顺序[^\n：:]*[：:])\s*\n([^\n]+)',text)
        if m:
            nodes=[s.strip() for s in re.split(r'[，,]',m.group(2)) if s.strip()]
            if 2<=len(nodes)<=12: flows.append({'title':m.group(1).rstrip('：:'),'items':nodes,'ref':record['ref']})
    return flows


def card_group(g,esc,cite,ordered=False):
    cls='evidence-flow' if ordered else 'evidence-grid'
    items=''.join(f'<article><span class="item-number">{i+1:02}</span><p>{esc(text)}</p></article>' for i,text in enumerate(g['items']))
    return f'<div class="panel source-shape"><div class="panel-head"><h3>{esc(g["title"])}</h3>{cite(g["ref"])}</div><div class="{cls}">{items}</div></div>'


def focus_view(r,esc,cite,table_html):
    """Return a distinct view and its truthful caption, with no model dependency."""
    kind=r['template']; gs=groups(r['records']); flows=flow_groups(r['records'])
    if kind=='proposal':
        body='<p class="section-desc">按资料自带的栏目区分当前能力、未包含能力与升级方向；原文清单不代表已批准的项目计划。</p>'
        body+=''.join(card_group(g,esc,cite) for g in gs)
        if not gs: body+='<p>尚无明确能力清单，见下方原文。</p>'
        return '能力边界与拟建工作包',body
    if kind=='meeting':
        body='<div class="meeting-context"><b>依据决策与行动记录整理 · 非会议实录</b><span>会议时间：未确认</span><span>参会人员：未确认</span><span>决议审批与执行状态：未确认</span></div>'
        body+=''.join(card_group(g,esc,cite) for g in gs)
        if r['actions']:
            body+='<div class="action-board">'+''.join(f'<article class="panel"><div class="panel-head"><h3>行动 {i+1}</h3>{cite(a["ref"])}</div><div class="panel-body"><p>{esc(a["action"])}</p><dl><dt>负责人（原文）</dt><dd>{esc(a["owner"])}</dd><dt>期限（原文）</dt><dd>{esc(a["due"])}</dd><dt>执行状态</dt><dd>资料未提供</dd></dl></div></article>' for i,a in enumerate(r['actions']))+'</div>'
        return '决策条目与行动看板',body
    if kind=='compare':
        clauses=[]
        for rec in r['records']:
            if rec['field']=='ocr_text': continue
            text_body=readable(rec['text'])
            # A parallel source paragraph is a comparison; generic advice about
            # what a user can do is not another option in that comparison.
            record_clauses=[];predicates=[]
            for text in re.split(r'(?<=[。；])',text_body):
                text=text.strip()
                m=re.match(r'([^，。；：\n]{2,18}?)(擅长|适合|不是|用于|更像|可以|提供)',text)
                if m:
                    record_clauses.append((m.group(1),text,rec['ref']))
                    predicates.append((m.group(1),m.group(2)))
            if max(Counter(p for _,p in set(predicates)).values(),default=0)>=2:
                clauses.extend(record_clauses)
        body='<p class="section-desc">并列展示资料明确描述的能力与适用条件；没有实测数据，不作评分、排名或性能优劣推断。</p>'
        if clauses:
            body+='<div class="comparison-grid">'+''.join(f'<article class="panel"><div class="panel-head"><h3>{esc(label)}</h3>{cite(ref)}</div><div class="panel-body"><p>{esc(text)}</p></div></article>' for label,text,ref in clauses)+'</div>'
        body+=''.join(card_group(g,esc,cite,True) for g in flows)
        return '能力对照与使用路径',body
    if kind=='learning':
        body='<p class="section-desc">链路与顺序仅来自原文箭头或明确步骤；不把不同笔记的字段名称自动合并，也不自动裁决历史修改要求。</p>'
        body+=''.join(card_group(g,esc,cite,True) for g in flows)
        body+=''.join(card_group(g,esc,cite) for g in gs)
        return '知识链路与操作清单',body
    # Research: same dimensions must come from an explicit table, not guessed cells.
    body='<p class="section-desc">按原文场景与条件组织资料；结构图表示原文并列关系，不表示已证明的因果或实施先后。</p>'
    body+=''.join(card_group(g,esc,cite) for g in gs)
    for t in r['tables']:
        body+=f'<div class="panel source-shape"><div class="panel-head"><h3>{esc(t["title"])} · 原文条件矩阵</h3>{cite(t["ref"])}</div>'+table_html(t['headers'],[[esc(v) for v in row] for row in t['rows']])+'</div>'
    return '场景与落地条件图谱',body


def complete_records(r,esc,cite,table_html):
    """All saved prose and every valid table remain inspectable, across tabs."""
    body=''
    for selected in r['selected']:
        records=[x for x in r['records'] if r['refs'][x['ref']-1]['source_id']==selected['id'] and r['refs'][x['ref']-1]['kind']==selected['kind']]
        tables=[t for t in r['tables'] if r['refs'][t['ref']-1]['source_id']==selected['id'] and r['refs'][t['ref']-1]['kind']==selected['kind']]
        inner=''
        for rec in records:
            ref=r['refs'][rec['ref']-1]; text=readable(rec['text'])
            label=esc(ref['tab'])+' · '+('OCR 原文 · 未推断字段' if ref['field']=='ocr_text' else '正文')
            passage=f'<div class="source-passage"><small>{label}</small><p>{esc(text)} {cite(rec["ref"])}</p></div>'
            if ref['field']=='ocr_text': passage='<details><summary>'+label+'</summary>'+passage+'</details>'
            inner+=passage
        for t in tables:
            ref=r['refs'][t['ref']-1]
            inner+='<div class="original-table"><h3>'+esc(ref['tab'])+' · 完整原表 '+cite(t['ref'])+'</h3>'+table_html(t['headers'],[[esc(v) for v in row] for row in t['rows']])+'</div>'
        body+='<details class="panel full-source" open><summary><b>'+esc(selected['title'])+'</b><span>原文与完整表格</span></summary><div class="panel-body">'+inner+'</div></details>'
    return body
