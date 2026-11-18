# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Source-faithful report view. Editorial guidance is never styled as evidence."""
from html import escape
import re


def _prose(value):
    """Show editor formatting as readable text without trusting source HTML.

    Only known formatting wrappers are removed. Unknown markup stays escaped;
    the source snapshot and citation offsets are never changed.
    """
    text=re.sub(r'<br\s*/?>','\n',str(value),flags=re.I)
    text=re.sub(r'</(?:p|div)\s*>','\n',text,flags=re.I)
    text=re.sub(r'</?(?:strong|span|b|i|em|u|p|div)(?:\s[^<>]*?)?\s*>','',text,flags=re.I)
    return escape(text,quote=True).replace('\n','<br>')


def render_compiled(r, appendix=False):
    from .report_workspace import valid_ref
    from .report_structured_view import body_html, citation_html, evidence_html
    esc=lambda s:escape(str(s),quote=True)
    a=r['adaptive'];refs={v['id']:v for v in r['refs']}
    if any(not valid_ref(v,r['units']) for v in refs.values()):
        raise ValueError('文稿引用与快照不一致')
    # Old saved compilations get the same reader-facing cleanup as new ones.
    used=set()
    for chapter in a.get('chapters',[]):
        for paragraph in chapter.get('paragraphs',[]):used.update(paragraph['refs'])
        for table in chapter.get('tables',[]):
            for row in table['rows']:used.update(row['refs'])
    if not used.issubset(refs):raise ValueError('选编内容引用不存在')
    refs={key:value for key,value in refs.items() if key in used}
    def cites(ids):
        if not ids or any(n not in refs for n in ids):
            raise ValueError('原文内容缺少有效引用')
        return '<span class="chapter-citations">'+''.join(citation_html(refs[n]) for n in dict.fromkeys(ids))+'</span>'
    text=f'<header class="report-heading"><h1>{esc(r["title"])}</h1>'
    if a['status']!='complete':text+='<p class="report-review">部分内容待补充</p>'
    text+='</header>'
    if a.get('state') == 'no_match' and not a.get('chapters'):
        return (text+'<section class="section report-no-match"><h2>未找到符合整理要求的内容</h2>'
                +'<p>所选资料没有提供足以完成本次要求的内容。请补充相关资料，或调整整理要求后重新生成。</p>'
                +'<p class="muted">整理要求：'+esc(r.get('focus',''))+'</p></section>')
    if len(a.get('chapters',[]))>1:
        text+='<nav class="adaptive-intro" aria-label="汇编目录"><ul class="report-toc">'
        for c in a['chapters']:
            text+=f'<li><a href="#section-chapter-{esc(c["id"])}">{esc(c["title"])}</a></li>'
        text+='</ul></nav>'

    def item_html(kind,item):
        if kind=='paragraph':
            return ('<div class="compiled-excerpt">'+body_html(item['text'])
                    +'<p class="source-citation">'+cites(item['refs'])+'</p></div>')
        rows=[]
        for row in item['rows']:
            if len(row['cells'])!=len(item['columns']):raise ValueError('原表列数不一致，未错列显示')
            cells=[_prose(s) for s in row['cells']]
            cells[-1]+=' '+cites(row['refs'])
            rows.append('<tr>'+''.join('<td>'+s+'</td>' for s in cells)+'</tr>')
        caption=item.get('caption','')
        visible_caption=('<caption>'+esc(caption)+'</caption>' if caption and not caption.endswith('（原文表格）') else '')
        return ('<div class="compiled-table-scroll" tabindex="0"><table class="compiled-table" title="'+esc(caption)+'">'
                +visible_caption+'<thead><tr>'+''.join('<th scope="col">'+esc(s)+'</th>' for s in item['columns'])
                +'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>')

    for i,c in enumerate(a.get('chapters',[]),1):
        key='chapter-'+str(c['id'])
        text+=f'<section class="section adaptive-chapter" id="section-{esc(key)}"><div class="section-head"><h2>{esc(c["title"])}</h2><button class="edit" data-edit="{esc(key)}">编辑说明</button></div>'
        if r.get('notes',{}).get(key):
            text+='<p class="edited-copy">'+esc(r['notes'][key]).replace('\n','<br>')+'</p><small class="tag amber">人工补充 · 待复核</small>'
        for guidance in c.get('guidance',[]):
            if guidance.get('kind')!='editorial':raise ValueError('导读身份无效')
            if re.sub(r'^编写建议[：:]\s*','',guidance['text']).strip()==c.get('purpose','').strip():continue
            text+='<aside class="compiled-guidance"><small>编写建议 · 非原文规定</small><p>'+esc(guidance['text'])+'</p></aside>'
        runs=[]
        for order in c.get('content_order',[]):
            kind=order['kind'];item=c['paragraphs' if kind=='paragraph' else 'tables'][order['index']]
            placement=item.get('placement','main')
            if not runs or runs[-1][0]!=placement:runs.append((placement,[]))
            runs[-1][1].append(item_html(kind,item))
        # Keep the incoming source order: separating all "main" excerpts first
        # can show a sentence ending before the source paragraph that starts it.
        for placement,parts in runs:
            # A saved chapter keeps its content. No second full-document
            # reading area is appended merely because a source was long.
            text+=''.join(parts)
        text+='</section>'
    issues=[issue for issue in a.get('issues',[]) if not isinstance(issue,dict) or issue.get('kind')!='empty_chapters']
    if issues:
        text+='<section class="section" id="section-gaps"><h2>仍需核对</h2>'+''.join('<p class="coverage-warning">'+esc(v.get('message','') if isinstance(v,dict) else v)+'</p>' for v in issues)+'</section>'
    if appendix:
        text+=evidence_html(list(refs.values()))
    return text
