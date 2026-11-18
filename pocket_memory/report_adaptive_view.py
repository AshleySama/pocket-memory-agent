# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Reading-first view for source-bound adaptive chapters, shared with offline export."""
from html import escape
import re

def esc(v): return escape(str(v),quote=True)

def display_paragraph(p,units):
    """Hide a leaked *terminal* internal ID list; preserve raw generation in JSON.

    If the original material contains that code, it could be meaningful and is
    never removed. This only replaces redundant control notation with real cites.
    """
    text=p['text'];m=re.search(r'(?:\s|[（(])*(F\d+(?:[,，]\s*F\d+)*)(?:[）)])?\s*$',text)
    if m and set(re.findall(r'F\d+',m.group(1)))==set(p.get('fact_ids',[])):
        if not any(m.group(1) in u['body'] for u in units):text=text[:m.start()].rstrip()
    return text

def render_adaptive(r,appendix=False):
    if r['adaptive'].get('reading_mode')=='source_structured_v1':
        from .report_structured_view import render_structured
        return render_structured(r,appendix)
    if r['adaptive'].get('reading_mode') in ('source_compiled_v1','source_brief_v2') and r['adaptive'].get('status') not in ('outline','reading_paused'):
        from .report_compiled_view import render_compiled
        return render_compiled(r,appendix)
    from .report_workspace import valid_ref
    from .report_structured_view import citation_html,evidence_html
    a=r['adaptive']; cov=a.get('coverage',{}); notes=r.get('notes',{})
    refs={ref['id']:ref for ref in r['refs']}
    if any(not valid_ref(ref,r['units']) for ref in refs.values()):
        raise ValueError('文稿引用与快照不一致')
    used=set()
    def cites(ids):
        used.update(ids)
        return ''.join(citation_html(refs[n]) for n in ids)
    if a.get('status')=='reading_paused':
        text=f'<header class="report-heading"><span class="eyebrow">阅读进度已保存</span><h1>{esc(r["title"])}</h1></header>'
        text+=f'<p>已完成 {cov.get("completed_batches",0)} / {cov.get("total_batches",0)} 批阅读。未读完的资料不会跳过，也不会用部分资料直接成稿。</p>'
        text+='<p>继续后沿用这次选择的资料快照、整理要求和已完成批次。关闭页面或重启应用后也可从这里继续。</p>'
        for issue in a.get('issues',[]):text+='<p class="coverage-warning">'+esc(issue.get('message',''))+'</p>'
        return text+'<button id="resume-reading" type="button" class="primary">继续阅读并生成提纲</button><p id="resume-reading-error" class="error" role="alert"></p>'
    if a.get('status')=='outline':
        result=f'<header class="report-heading"><h1>{esc(r["title"])}</h1><span class="report-review">提纲已保存 · 尚未成稿</span></header>'
        result+='<p class="report-purpose">'+esc(r.get('focus',''))+'</p><ol>'
        for c in a.get('outline',[]):result+='<li><b>'+esc(c['title'])+'</b><p>'+esc(c['purpose'])+'</p></li>'
        result+='</ol><p>可在工作台调整章节，再按生成提纲时的资料快照继续成稿。</p>'
        if a.get('issues'):
            result+='<p class="coverage-warning">部分资料提取仍有缺口；提纲确认不等于内容验收。</p>'
        return result
    partial=a.get('status')!='complete'
    text=f'<header class="report-heading"><h1>{esc(r["title"])}</h1>'
    if partial:text+='<p class="report-review">部分内容待补充</p>'
    text+='</header>'
    if len(a.get('chapters',[]))>1:
        text+='<nav class="adaptive-intro" aria-label="汇编目录"><ul class="report-toc">'
        for c in a['chapters']:
            text+=f'<li><a href="#section-chapter-{esc(c["id"])}">{esc(c["title"])}</a></li>'
        text+='</ul></nav>'
    for i,c in enumerate(a.get('chapters',[]),1):
        key='chapter-'+str(c['id'])
        text+=f'<section class="section adaptive-chapter" id="section-{esc(key)}"><div class="section-head"><h2>{esc(c["title"])}</h2><button class="edit" data-edit="{esc(key)}">编辑说明</button></div>'
        if notes.get(key):
            text+='<p class="edited-copy">'+esc(notes[key]).replace('\n','<br>')+'</p><small class="tag amber">人工补充 · 待复核</small>'
        for p in c.get('paragraphs',[]):
            ids=p.get('refs',[])
            if not ids or any(n not in refs for n in ids):raise ValueError('章节引用不在文稿来源中')
            text+='<p class="chapter-paragraph">'+esc(display_paragraph(p,r['units'])).replace('\n','<br>')+' <span class="chapter-citations">'+cites(ids)+'</span></p>'
        if c.get('status')!='complete':
            text+='<p class="coverage-warning">本章仍有要点未写入成稿，请在下方未覆盖项中核对。</p>'
        text+='</section>'
    missing=set(cov.get('uncovered_fact_ids',[]))
    facts={f['id']:f for f in a.get('facts',[])}
    if partial or missing:
        text+='<section class="section" id="section-gaps"><div class="section-head"><h2>仍需补充与核对</h2></div>'
        if missing:
            text+='<ul>'+''.join('<li>'+esc(facts[f]['text'])+' '+cites([n for n in facts[f].get('refs',[]) if n in refs])+'</li>' for f in missing if f in facts)+'</ul>'
        for issue in a.get('issues',[]):
            text+='<p class="coverage-warning">'+esc(issue.get('message','') if isinstance(issue,dict) else issue)+'</p>'
        if notes.get('gaps'):text+='<p class="edited-copy">'+esc(notes['gaps'])+'</p>'
        text+='</section>'
    if appendix:
        text+=evidence_html([ref for key,ref in refs.items() if key in used])
    return text
