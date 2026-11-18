# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Paginated local report-source discovery; no model and no implicit selection.

Reuse the library's SQLite identities and multi-tab ownership. Only the current
page's excerpts leave the store, never the complete library body in the browser.
"""
import re
from .report_sections import readable


def _excerpt(text, terms):
    text=re.sub(r'\s+', ' ', readable(text or ''))
    positions=[text.lower().find(t) for t in terms if t in text.lower()]
    at=max(0,min(positions)-35) if positions else 0
    return ('…' if at else '')+text[at:at+180]+('…' if len(text)>at+180 else '')


def discover(store, query='', kind='all', page=1, page_size=30):
    query=str(query).strip()
    if len(query)>100:raise ValueError('查找关键词请控制在100字以内')
    if kind not in ('all','note','document'):raise ValueError('资料类型无效')
    try:page=int(page);page_size=int(page_size)
    except (ValueError,TypeError):raise ValueError('分页参数无效')
    if not 1<=page<=100000 or not 1<=page_size<=50:raise ValueError('分页参数超出范围')
    terms=list(dict.fromkeys(t.lower() for t in re.split(r'\s+',query) if t))
    if len(terms)>8:raise ValueError('请使用最多8个查找关键词')
    # Each term may match a different tab of the same root note. Match tags and
    # OCR as well; don't drop child-only matches as a root-title filter would.
    note_where=["n.parent_note_id IS NULL", "n.source_type != 'welcome'"]
    note_params=[]
    for term in terms:
        note_where.append("""EXISTS(SELECT 1 FROM notes u WHERE (u.id=n.id OR u.parent_note_id=n.id)
          AND (instr(lower(coalesce(u.title,'')||' '||coalesce(u.tab_name,'')||' '||
               coalesce(u.content,'')||' '||coalesce(u.ocr_text,'')||' '||
               coalesce(u.category,'')||' '||coalesce(u.subcategory,'')),?)>0
          OR EXISTS(SELECT 1 FROM note_tags nt JOIN tags t ON t.id=nt.tag_id
                    WHERE nt.note_id=u.id AND instr(lower(t.name),?)>0)))""")
        note_params.extend([term,term])
    doc_where=["EXISTS(SELECT 1 FROM document_chunks c WHERE c.document_id=d.id AND length(trim(c.text))>0)"]
    doc_params=[]
    for term in terms:
        doc_where.append("""(instr(lower(d.title||' '||d.original_name),?)>0 OR EXISTS(
            SELECT 1 FROM document_chunks c WHERE c.document_id=d.id
            AND instr(lower(c.text||' '||c.location||' '||c.section||' '||c.sheet),?)>0))""")
        doc_params.extend([term,term])
    selects=[];params=[]
    if kind!='document':
        selects.append("SELECT 'note' kind,n.id,n.title,n.updated_at FROM notes n WHERE "+' AND '.join(note_where));params+=note_params
    if kind!='note':
        selects.append("SELECT 'document' kind,d.id,d.title,d.updated_at FROM documents d WHERE "+' AND '.join(doc_where));params+=doc_params
    catalog=' UNION ALL '.join(selects)
    with store._lock:
        db=store.connection
        total=db.execute('SELECT count(*) FROM ('+catalog+')',params).fetchone()[0]
        # Stable pagination: titles first when the query is an exact substring,
        # then recent material; root identity breaks equal-title/date ties.
        order="CASE WHEN instr(lower(title),?)>0 THEN 0 ELSE 1 END," if query else ''
        rows=db.execute('SELECT * FROM ('+catalog+') ORDER BY '+order+
                        'updated_at DESC,kind,id DESC LIMIT ? OFFSET ?',
                        [*params,*([query.lower()] if query else []),page_size,(page-1)*page_size]).fetchall()
        items=[]
        for row in rows:
            sid=row['id'];item=dict(kind=row['kind'],id=sid,title=row['title'],updated=row['updated_at'])
            if row['kind']=='note':
                tabs=db.execute("SELECT id,title,tab_name,category,subcategory,content,ocr_text FROM notes WHERE id=? OR parent_note_id=? ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,tab_order,id",(sid,sid,sid)).fetchall()
                tags=db.execute('SELECT DISTINCT t.name FROM tags t JOIN note_tags nt ON nt.tag_id=t.id JOIN notes u ON u.id=nt.note_id WHERE u.id=? OR u.parent_note_id=? ORDER BY t.name',(sid,sid)).fetchall()
                def score(t):
                    title=(t['title']+' '+t['tab_name']).lower();text=(t['content']+' '+(t['ocr_text'] or '')).lower()
                    return sum(3*(term in title)+(term in text) for term in terms)
                hit=max(tabs,key=score);body=hit['content']
                if terms and not any(t in body.lower() for t in terms) and any(t in (hit['ocr_text'] or '').lower() for t in terms):body=hit['ocr_text'];field='OCR'
                else:field='正文'
                item.update(tabs=len(tabs),category=tabs[0]['category'],tags=[t[0] for t in tags],
                            snippet=_excerpt(body,terms),matched_tab=hit['tab_name'] or '页签1',matched_field=field)
            else:
                clauses=['c.document_id=?'];args=[sid]
                if terms:
                    clauses.append('('+' OR '.join('instr(lower(c.text),?)>0' for _ in terms)+')');args+=terms
                hit=db.execute('SELECT text,location FROM document_chunks c WHERE '+' AND '.join(clauses)+' ORDER BY chunk_order,id LIMIT 1',args).fetchone()
                if hit is None:hit=db.execute('SELECT text,location FROM document_chunks WHERE document_id=? ORDER BY chunk_order,id LIMIT 1',(sid,)).fetchone()
                item.update(tabs=1,category='文档',tags=[],snippet=_excerpt(hit['text'],terms) if hit else '',matched_tab=hit['location'] if hit else '',matched_field='正文')
            items.append(item)
    return dict(items=items,total=total,page=page,page_size=page_size,query=query)
