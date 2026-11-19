# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Read an already structured source as a report, without inventing a topic."""
from html import escape
import re
from .report_source_markup import format_outside_code, render_body, render_inline


def inline(text):
    return render_inline(text)


def citation_html(ref):
    """Keep the existing reference handler, with provenance in its tooltip."""
    from .report_workspace import citation
    label=' · '.join(str(ref.get(key,'')) for key in ('title','tab') if ref.get(key))
    return citation(ref['id']).replace('<a ', '<a title="'+escape(label,quote=True)+'" ', 1)


def evidence_html(refs):
    """Only cited passages, collapsed by default; never a full-source dump."""
    if not refs:return ''
    out='<details class="section appendix" id="report-source-appendix"><summary>引用核对</summary>'
    for ref in refs:
        out+=('<article id="source-'+escape(str(ref['id']),quote=True)+'"><h3>['+escape(str(ref['id']))+'] '
              +escape(str(ref.get('title','资料')))+' · '+escape(str(ref.get('tab','')))
              +'</h3><blockquote>'+escape(ref['quote'])+'</blockquote></article>')
    return out+'</details>'


def prose_paragraph(text):
    field=re.match(r'^([^：:\n]{1,14}[：:])\s*(.+)$',text)
    if field:
        return ('<p class="source-field"><strong class="source-field-label">'+inline(field[1])
                +'</strong><span>'+inline(field[2])+'</span></p>')
    return '<p>'+inline(text)+'</p>'


def body_html(text,title=''):
    heading=re.match(r'^(?:[ \t]*\r?\n)* {0,3}#{1,6}[ \t]+([^\n]+)\n?',text)
    if heading and title:
        value=heading[1].strip();end=heading.end()
        if re.fullmatch(r'\d+[.、)]',value):
            continuation=re.match(r'[^\n]+\n?',text[end:])
            if continuation:value+=' '+continuation[0].strip();end+=continuation.end()
        normalize=lambda s:re.sub(r'\s+','',re.sub(r'^\d+[.、)]\s*','',s))
        if normalize(value)==normalize(title):text=text[end:]
    def repair_wraps(value):
        # Preserve the established display repairs; literal code stays untouched.
        value=re.sub(r'(<(?:strong|b)>\s*\d+)\.\s*\n\s*(\d+(?:[～~–—-]\d+)?\s*(?:小时|分钟|秒|℃|g|kg|ml|厘米))',r'\1.\2',value)
        value=re.sub(r'(<(?:strong|b)>[^<>\n]*)\n\s*(</(?:strong|b)>)',r'\1\2',value)
        value=re.sub(r'(?m)^(\s*\d+[.)、])\s*\n(?=\S)',r'\1 ',value)
        value=re.sub(r'(?m)^(#{1,6}[ \t]+\d+[.、)])\s*\n(?=([^#>\n][^\n]{0,119})(?:\n|$))',
                    lambda m:m[1]+' ' if not re.search(r'[：:。；]',m[2]) else m[0],value)
        return value
    return render_body(format_outside_code(text,repair_wraps))


def render_structured(r,appendix=False):
    from .report_workspace import valid_ref
    a=r['adaptive'];refs={x['id']:x for x in r['refs']}
    if any(not valid_ref(x,r['units']) for x in refs.values()):raise ValueError('文稿原文位置已变化')
    used=set()
    out='<header class="report-heading"><h1>'+escape(r['title'])+'</h1></header>'
    if len(a['chapters'])>1:
        out+='<nav class="adaptive-intro" aria-label="汇编目录"><ul class="report-toc">'
        for ch in a['chapters']:
            label=(str(ch['source_label'])+' · ' if ch.get('source_label') else '')+ch['title']
            out+='<li><a href="#section-chapter-'+escape(str(ch['id']),quote=True)+'">'+escape(label)+'</a></li>'
        out+='</ul></nav>'
    for ch in a['chapters']:
        key='chapter-'+str(ch['id']);safe_key=escape(key,quote=True)
        source_label=('<p class="source-note-label">'+escape(str(ch['source_label']))+'</p>') if ch.get('source_label') else ''
        out+=f'<section class="section adaptive-chapter" id="section-{safe_key}">'+source_label+'<div class="section-head"><h2>'+escape(ch['title'])+f'</h2><button class="edit" data-edit="{safe_key}">编辑说明</button></div>'
        if r.get('notes',{}).get(key):out+='<p class="edited-copy">'+escape(r['notes'][key]).replace('\n','<br>')+'</p><small class="source-note-label">人工补充</small>'
        layout=a.get('reading_layout')
        units={u['unit_id']:u for u in r['units']}
        document_layout=(layout and all(units[s['unit_id']]['kind']=='document' for s in ch['segments'])
                         and len({(units[s['unit_id']]['source_id'],units[s['unit_id']]['field']) for s in ch['segments']})==1)
        citations=[]
        for segment in ch['segments']:
            ref=refs[segment['ref']]
            if segment['text']!=ref['quote']:raise ValueError('本章内容与引用不同')
            used.add(ref['id'])
            if document_layout:
                citations.append('<p class="source-citation">'+citation_html(ref)+'</p>')
            else:
                out+='<div class="source-segment">'+body_html(segment['text'],ch['title'])+'<p class="source-citation">'+citation_html(ref)+'</p></div>'
        if document_layout:
            from .report_reading_view import chapter_text
            out+='<div class="source-segment source-reading">'+body_html(chapter_text(ch,units,layout),ch['title'])+''.join(citations)+'</div>'
        out+='</section>'
    if appendix:
        out+=evidence_html([ref for ref in refs.values() if ref['id'] in used])
    return out
