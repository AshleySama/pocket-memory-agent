# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Read an already structured source as a report, without inventing a topic."""
from html import escape
from html.parser import HTMLParser
import re


class Inline(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.out=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('strong','b','em','i','u'):self.out.append('<'+tag+'>')
        elif tag=='br':self.out.append('<br>')
    def handle_endtag(self,tag):
        if tag in ('strong','b','em','i','u'):self.out.append('</'+tag+'>')
    def handle_data(self,data):self.out.append(escape(data))


def inline(text):
    text=re.sub(r'\[\[([^\[\]\r\n]+)\]\]',r'《\1》',text)
    p=Inline();p.feed(text);p.close();return ''.join(p.out)


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
    heading=re.match(r'^\s*#{1,6}\s+([^\n]+)\n?',text)
    if heading and title:
        value=heading[1].strip();end=heading.end()
        if re.fullmatch(r'\d+[.、)]',value):
            continuation=re.match(r'[^\n]+\n?',text[end:])
            if continuation:value+=' '+continuation[0].strip();end+=continuation.end()
        normalize=lambda s:re.sub(r'\s+','',re.sub(r'^\d+[.、)]\s*','',s))
        if normalize(value)==normalize(title):text=text[end:]
    # A numeric line wrap inside explicit emphasis is formatting, not a new
    # recipe measurement. Only the presentation joins it; the source stays exact.
    text=re.sub(r'(<(?:strong|b)>\s*\d+)\.\s*\n\s*(\d+(?:[～~–—-]\d+)?\s*(?:小时|分钟|秒|℃|g|kg|ml|厘米))',r'\1.\2',text)
    text=re.sub(r'(<(?:strong|b)>[^<>\n]*)\n\s*(</(?:strong|b)>)',r'\1\2',text)
    text=re.sub(r'(?m)^(\s*\d+[.)、])\s*\n(?=\S)',r'\1 ',text)
    # A broken numbered Markdown heading remains one heading on the page.
    text=re.sub(r'(?m)^(#{1,6}[ \t]+\d+[.、)])\s*\n(?=([^#>\n][^\n]{0,119})(?:\n|$))',
                lambda m:m[1]+' ' if not re.search(r'[：:。；]',m[2]) else m[0],text)
    lines=text.splitlines();result=[];paragraph=[];items=[];current=None;start=1
    def flush_paragraph():
        if paragraph:result.append(prose_paragraph(' '.join(paragraph)));paragraph.clear()
    def flush_list():
        nonlocal current
        if current is not None:items.append(current);current=None
        if items:
            result.append(f'<ol class="source-steps" start="{start}">'+''.join(
                f'<li value="{number}">'+inline(' '.join(parts))+'</li>' for number,parts in items)+'</ol>');items.clear()
    for line in lines:
        line=line.strip()
        if not line:flush_paragraph();flush_list();continue
        heading=re.match(r'^(#{1,6})\s+(.+)',line)
        item=re.match(r'^(\d+)[.)、]\s+(.+)',line)
        if heading:
            flush_paragraph();flush_list();level=max(3,min(5,len(heading[1])))
            result.append(f'<h{level}>'+inline(heading[2])+f'</h{level}>')
        elif item:
            flush_paragraph()
            if current is not None:items.append(current)
            if not items:start=int(item[1])
            current=(int(item[1]),[item[2]])
        elif line.startswith('>'):
            flush_paragraph();flush_list();result.append('<p class="source-tip">'+inline(line.lstrip('> '))+'</p>')
        elif current is not None:current[1].append(line)
        else:paragraph.append(line)
    flush_paragraph();flush_list();return ''.join(result)


def render_structured(r,appendix=False):
    from .report_workspace import valid_ref
    a=r['adaptive'];refs={x['id']:x for x in r['refs']}
    if any(not valid_ref(x,r['units']) for x in refs.values()):raise ValueError('文稿原文位置已变化')
    used=set()
    out='<header class="report-heading"><h1>'+escape(r['title'])+'</h1></header>'
    if len(a['chapters'])>1:
        out+='<nav class="adaptive-intro" aria-label="汇编目录"><ul class="report-toc">'
        for ch in a['chapters']:
            out+='<li><a href="#section-chapter-'+escape(str(ch['id']),quote=True)+'">'+escape(ch['title'])+'</a></li>'
        out+='</ul></nav>'
    for ch in a['chapters']:
        key='chapter-'+str(ch['id']);safe_key=escape(key,quote=True)
        out+=f'<section class="section adaptive-chapter" id="section-{safe_key}"><div class="section-head"><h2>'+escape(ch['title'])+f'</h2><button class="edit" data-edit="{safe_key}">编辑说明</button></div>'
        if r.get('notes',{}).get(key):out+='<p class="edited-copy">'+escape(r['notes'][key]).replace('\n','<br>')+'</p><small class="source-note-label">人工补充</small>'
        for segment in ch['segments']:
            ref=refs[segment['ref']]
            if segment['text']!=ref['quote']:raise ValueError('本章内容与引用不同')
            used.add(ref['id'])
            out+='<div class="source-segment">'+body_html(segment['text'],ch['title'])+'<p class="source-citation">'+citation_html(ref)+'</p></div>'
        out+='</section>'
    if appendix:
        out+=evidence_html([ref for ref in refs.values() if ref['id'] in used])
    return out
