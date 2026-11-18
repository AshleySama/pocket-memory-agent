# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Source-linked presentation of whole-document report digests.

Summary text is the saved paraphrase, never a replacement made from its quote.
The identity checks here protect navigation; they do not certify semantic truth.
This module renders body markup only and loads no remote assets or scripts.
"""
from __future__ import annotations

from html import escape


def _esc(value):
    return escape(str(value if value is not None else ''), quote=True)


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _cite(ref_id):
    return (f'<a class="cite" href="#source-{ref_id}" data-ref="{ref_id}" '
            f'aria-label="查看引用 {ref_id}">[{ref_id}]</a>')


def _note(key, notes):
    text = notes.get(key)
    if not isinstance(text, str) or not text.strip():
        return ''
    return ('<p class="edited-copy">' + _esc(text).replace('\n', '<br>')
            + '</p><small class="tag amber">人工补充 · 待复核</small>')


def render_digest(report, appendix=False):
    """Render a whole-document-v1 digest using the workbench's existing styles.

    References stay collapsed until the reader chooses to check a citation.
    Existing data-ref / data-edit handlers remain the navigation and edit authority.
    """
    # Import at call time so the existing workspace can dispatch to this renderer
    # without introducing a module-initialisation cycle.
    from pocket_memory.report_workspace import valid_ref

    digest = report.get('digest') or {}
    if digest.get('protocol') != 'whole-document-v1':
        raise ValueError('不支持的长文归纳版本')
    units = report.get('units') or []
    references = report.get('refs') or []
    ref_map = {}
    for ref in references:
        ref_id = ref.get('id')
        if (type(ref_id) is not int or ref_id < 1 or ref_id in ref_map
                or not valid_ref(ref, units)):
            raise ValueError('文稿引用与保存的资料快照不一致')
        ref_map[ref_id] = ref

    batches = digest.get('batches') or []
    batch_map = {}
    for batch in batches:
        batch_id = batch.get('id')
        if type(batch_id) is not int or batch_id in batch_map:
            raise ValueError('分段归纳编号无效或重复')
        batch_map[batch_id] = batch

    chapters = []
    assigned = set()
    for chapter in digest.get('sections') or []:
        ids = chapter.get('batch_ids') or []
        if not ids:
            continue
        if any(type(i) is not int or i not in batch_map or i in assigned for i in ids) or len(ids) != len(set(ids)):
            raise ValueError('归纳目录存在缺失或重复分段')
        assigned.update(ids)
        visible_ids = [i for i in ids if batch_map[i].get('points') or batch_map[i].get('status') == 'incomplete']
        if visible_ids:
            chapters.append({'title': str(chapter.get('title') or '资料归纳'), 'batch_ids': visible_ids})
    # A failed/unfinished planning step must not silently hide already saved work.
    remaining = [batch['id'] for batch in batches if batch['id'] not in assigned
                 and (batch.get('points') or batch.get('status') == 'incomplete')]
    if remaining:
        chapters.append({'title': '其他分段归纳', 'batch_ids': remaining})

    coverage = digest.get('coverage') or {}
    total_units = _count(coverage.get('total_units'))
    read_units = _count(coverage.get('read_units'))
    excluded_units = _count(coverage.get('excluded_units'))
    processed_units = _count(coverage.get('processed_units', min(total_units, read_units + excluded_units)))
    # The visible entries may each be one knowledge point. They are not model
    # reading batches and must never inflate completed work or coverage.
    map_batches = digest.get('map_batches') or []
    total_batches = _count(coverage.get('total_batches', len(map_batches)))
    completed = _count(coverage.get('completed_batches', sum(
        batch.get('status') in ('complete', 'no_points') for batch in map_batches)))
    if read_units > total_units or excluded_units > total_units or processed_units > total_units or completed > total_batches:
        raise ValueError('文稿覆盖统计无效')
    # no_points is a completed read with no substantive knowledge to present,
    # not a failed generation. Proven watermark-only units need no model read.
    finished_batches = completed
    incomplete = (read_units + excluded_units < total_units or finished_batches < total_batches
                  or any(batch.get('status') not in ('complete', 'no_points') for batch in map_batches))
    notes = report.get('notes') or {}
    title = _esc(report.get('title', '资料汇编'))
    output = [
        '<header class="report-heading"><div class="report-title-row"><div>'
        f'<div class="eyebrow">{_esc(report.get("template_label", "资料汇编"))}</div><h1>{title}</h1>'
        '</div></div><div class="report-meta">'
        f'<span>{_esc(str(report.get("created", ""))[:10])}</span>'
        '</div></header>'
    ]
    used_refs = set()

    def citations(ref_ids):
        used_refs.update(ref_ids)
        return ' '.join(_cite(ref_id) for ref_id in dict.fromkeys(ref_ids))

    def section(key, heading, body, edit_key=None):
        edit = (f'<button class="edit" data-edit="{_esc(edit_key)}">编辑说明</button>'
                if edit_key else '')
        return (f'<section class="section" id="section-{_esc(key)}">'
                '<div class="section-head">'
                f'<h2>{_esc(heading)}</h2>{edit}</div>{body}</section>')

    if chapters:
        introduction = ''
        contents = '<nav aria-label="汇编目录"><ul class="report-toc">'
        for index, chapter in enumerate(chapters, 1):
            contents += f'<li><a href="#section-digest-{index}">{_esc(chapter["title"])}</a></li>'
        contents += '</ul></nav>'
    else:
        introduction = '<p class="section-desc">尚未形成可阅读的正文。</p>'
        contents = ''
    if incomplete:
        introduction += ('<div class="decision-banner"><strong>归纳尚未完成</strong>'
                         '<p>未完成部分已在下方标出；当前内容尚未涵盖全部资料。</p></div>')
    output.append(section('summary', '目录', introduction + _note('summary', notes) + contents, 'summary'))

    for index, chapter in enumerate(chapters, 1):
        body = []
        for batch_id in chapter['batch_ids']:
            batch = batch_map[batch_id]
            heading = str(batch.get('heading') or f'分段 {batch_id}')
            body.append('<div class="record-panel">')
            if len(chapter['batch_ids']) != 1 or heading.strip() != chapter['title'].strip():
                body.append('<h3>' + _esc(heading) + '</h3>')
            points = batch.get('points') or []
            if points:
                body.append('<ol class="summary-list">')
                for point in points:
                    text = str(point.get('text') or '').strip()
                    point_refs = point.get('refs') or []
                    if not text or not point_refs or any(type(r) is not int or r not in ref_map for r in point_refs):
                        raise ValueError('归纳要点缺少有效内容或来源编号')
                    topic = str(point.get('topic') or '').strip()
                    label = ('<strong class="summary-topic">' + _esc(topic) + '：</strong>'
                             if topic and topic not in (heading, chapter['title']) else '')
                    body.append('<li><p>' + label + _esc(text) + ' ' + citations(point_refs) + '</p></li>')
                body.append('</ol>')
            status = batch.get('status')
            if status not in ('complete', 'no_points'):
                message = '本节尚未完成归纳。' if status == 'incomplete' else '本节尚未形成可用的归纳要点。'
                body.append('<p class="section-desc">' + message + '请结合原文继续核对。</p>')
                # Retain a route to source material even when generation failed.
                source_units = set(batch.get('source_units') or [])
                source_refs = [ref['id'] for ref in references if ref['unit_id'] in source_units]
                if source_refs:
                    body.append('<p class="section-desc">相关原文：' + citations(source_refs) + '</p>')
            body.append('</div>')
        output.append(section(f'digest-{index}', chapter['title'], ''.join(body)))

    # Keep every supported user-note field and its original edit binding. Empty
    # template sections are not inserted into a long-document digest.
    note_titles = {'outcomes': '成果补充', 'chart': '图表与数据说明', 'records': '内容补充',
                   'actions': '行动补充', 'gaps': '核对说明'}
    for key, heading in note_titles.items():
        if isinstance(notes.get(key), str) and notes[key].strip():
            output.append(section('note-' + key, heading, _note(key, notes), key))

    limitations = []
    for item in digest.get('source_only', []):
        limitations.append(str(item.get('topic', '')) + '：' + str(item.get('reason', '未形成知识摘要')))
    unit_map = {unit['unit_id']: unit for unit in units}
    for batch in map_batches:
        if batch.get('status') != 'incomplete':
            continue
        locations = []
        for unit_id in batch.get('source_units') or []:
            unit = unit_map.get(unit_id)
            location = (f'《{unit.get("title", "资料")}》 · {unit.get("tab") or "位置未记录"}'
                        if unit else '来源位置未记录')
            if location not in locations:
                locations.append(location)
        source_location = '；'.join(locations) or '来源位置未记录'
        limitations.append(f'{source_location}：尚未完成归纳。')
    if limitations:
        output.append('<details class="report-source-index" open><summary>未归纳内容</summary><ul>'
                      + ''.join('<li>' + _esc(item) + '</li>' for item in limitations) + '</ul></details>')

    cited_references = [ref for ref in references if ref['id'] in used_refs]
    if cited_references:
        # Every displayed citation has an anchor, even when this is collapsed.
        # Native details remains usable in the self-contained exported HTML.
        source_body = ('<div class="appendix"><details class="panel full-source"><summary><b>引用核对</b>'
                       '<span>展开查看原文</span></summary><div class="panel-body">')
        for ref in cited_references:
            field = 'OCR' if ref.get('field') == 'ocr_text' else '正文'
            source_body += (f'<article id="source-{ref["id"]}"><h3>[{ref["id"]}] {_esc(ref["title"])}'
                            f' · {_esc(ref.get("tab", ""))} · {field}</h3>'
                            f'<blockquote style="white-space:pre-wrap">{_esc(ref["quote"])}</blockquote></article>')
        source_body += '</div></details></div>'
        output.append(source_body)
    return ''.join(output)
