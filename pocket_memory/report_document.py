# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Human-authored report body, independent of the immutable generation record.

Only inert rich text and self-contained vector charts are accepted. Reference
links are rebuilt from saved identities; editing prose cannot create evidence.
"""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit


MAX_DOCUMENT_BYTES = 1024 * 1024
HTML_TAGS = frozenset('h2 h3 h4 h5 h6 p div section article aside span b strong i em u s '
    'strike mark small sub sup ul ol li blockquote br hr table caption colgroup col '
    'thead tbody tfoot tr th td a pre code details summary'.split())
SVG_TAGS = frozenset('svg g path rect circle ellipse line polyline polygon text tspan title desc'.split())
VOID_TAGS = frozenset('br hr col img input meta link source embed area base wbr param track'.split())
OMIT_TAGS = frozenset('script style header nav button form input select option textarea '
    'noscript template meta link base'.split())
RESOURCE_TAGS = frozenset('img image picture video audio source track iframe object embed '
    'foreignobject use feimage animate animatemotion animatetransform set'.split())
# Keep presentation classes already used by generated reports, without permitting
# editor controls or application state classes in persisted document content.
BODY_CLASSES = frozenset('section section-head section-number section-desc adaptive-chapter '
    'chapter-paragraph chapter-citations compiled-excerpt compiled-table-scroll compiled-table '
    'compiled-guidance source-segment source-citation source-field source-field-label source-steps '
    'source-tip source-note-label summary-list summary-topic panel panel-head panel-body record-panel '
    'recorded-grid index table-scroll table-caption two-col chart-box distribution edited-copy '
    'tag amber coverage-warning report-no-match muted report-purpose decision-banner ocr-record '
    'report-source-index'.split())


def _esc(value):
    return escape(str(value), quote=True)


def _color(value):
    value = value.strip().lower()
    if re.fullmatch(r'#[0-9a-f]{3}(?:[0-9a-f]{3})?(?:[0-9a-f]{2})?', value):
        return value
    if re.fullmatch(r'[a-z]{1,24}', value) and value not in ('expression', 'url', 'inherit'):
        return value
    if re.fullmatch(r'(?:rgb|rgba|hsl|hsla)\([0-9.,%\s+-]{1,80}\)', value):
        return value
    return None


def _length(value, low=0, high=400):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(px|pt|em|rem|%)?', value.strip().lower())
    if match and low <= float(match[1]) <= high:
        return match[0]
    return None


def _style(value):
    """A grammar allowlist, so CSS escapes, imports, URLs and overlays cannot pass."""
    clean = []
    for declaration in value.split(';'):
        key, sep, raw = declaration.partition(':')
        key, raw = key.strip().lower(), raw.strip().lower()
        if not sep or not raw or len(raw) > 160:
            continue
        accepted = None
        if key in ('color', 'background-color'):
            accepted = _color(raw)
        elif key == 'font-size':
            accepted = _length(raw, 1, 200)
        elif key == 'font-family' and re.fullmatch(r'[a-z0-9\s,\-\u3400-\u9fff"\']{1,120}', raw):
            accepted = raw
        elif key == 'font-weight' and re.fullmatch(r'normal|bold|[1-9]00', raw):
            accepted = raw
        elif key == 'font-style' and raw in ('normal', 'italic', 'oblique'):
            accepted = raw
        elif key == 'text-align' and raw in ('left', 'center', 'right', 'justify', 'start', 'end'):
            accepted = raw
        elif key in ('text-decoration', 'text-decoration-line') and re.fullmatch(r'(?:none|underline|line-through|overline)(?: (?:underline|line-through|overline))*', raw):
            accepted = raw
        elif key == 'line-height':
            accepted = 'normal' if raw == 'normal' else _length(raw, 0.5, 400)
        elif key in ('margin-left', 'margin-right', 'padding-left', 'text-indent'):
            accepted = _length(raw)
        elif key == 'white-space' and raw in ('normal', 'pre', 'pre-wrap', 'pre-line', 'break-spaces'):
            accepted = raw
        elif key == 'vertical-align' and raw in ('baseline', 'sub', 'super', 'top', 'middle', 'bottom'):
            accepted = raw
        elif key in ('width', 'height', 'max-width'):
            accepted = _length(raw, 0, 2000)
        elif key == 'border-collapse' and raw in ('collapse', 'separate'):
            accepted = raw
        if accepted is not None:
            clean.append(key + ':' + accepted)
    return ';'.join(clean)


def _link(value):
    value = value.strip()
    if not value or re.search(r'[\x00-\x20\x7f\\]', value):
        return None
    if re.fullmatch(r'#[A-Za-z][A-Za-z0-9_.:-]{0,127}', value):
        # Evidence links must use canonical data-ref, never a fabricated anchor.
        return None if value.startswith('#source-') else value
    parsed = urlsplit(value)
    if parsed.scheme.lower() in ('http', 'https') and parsed.netloc:
        return value
    if parsed.scheme.lower() == 'mailto' and parsed.path and not parsed.netloc:
        return value
    return None


class _BodyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {'tag': None, 'attrs': {}, 'children': []}
        self.stack = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 40000 or len(self.stack) > 120:
            raise ValueError('正文结构过于复杂，请分段精简后再保存')
        names = [name for name, _ in attrs]
        if len(names) != len(set(names)):
            raise ValueError('正文存在重复的 HTML 属性，请重新打开编辑')
        node = {'tag': tag, 'attrs': dict(attrs), 'children': []}
        self.stack[-1]['children'].append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1]['children'].append(data)


def sanitize_document_html(value, references):
    """Return balanced inert HTML; reject unknown citation identities/resources.

    Unsupported embedded media are explicitly rejected instead of being silently
    dropped by save. Plain links never auto-load their destination.
    """
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise ValueError('正文必须是文本，且不能超过 1 MB')
    refs = {}
    for ref in references:
        ref_id = ref.get('id')
        if type(ref_id) is not int or ref_id < 1 or ref_id in refs:
            raise ValueError('文稿引用编号无效或重复')
        refs[ref_id] = ref
    parser = _BodyParser()
    parser.feed(value)
    parser.close()

    def children(node, svg=False):
        return ''.join(render(child, svg) for child in node['children'])

    def render(node, svg=False):
        if isinstance(node, str):
            return _esc(node)
        tag, raw = node['tag'], node['attrs']
        if tag in OMIT_TAGS:
            return ''
        if tag in RESOURCE_TAGS:
            raise ValueError('正文含图片、嵌入内容或外部资源，当前无法完整保存；请保留原稿并移除该资源后再试')
        if any(key in raw for key in ('src', 'srcset', 'poster', 'background', 'xlink:href')):
            raise ValueError('正文含自动加载的资源，无法安全保存；原稿未改变')
        if raw.get('id') == 'report-source-appendix' or 'appendix' in (raw.get('class') or '').split():
            raise ValueError('引用附录不属于可编辑正文，请重新打开文稿编辑')
        if 'data-ref' in raw:
            ref_text = raw['data-ref'] or ''
            if tag != 'a' or not re.fullmatch(r'[1-9][0-9]*', ref_text) or int(ref_text) not in refs:
                raise ValueError('正文含未知引用，不能新增或更改原文引用编号')
            ref_id = int(ref_text)
            ref = refs[ref_id]
            label = ' · '.join(str(ref.get(key, '')) for key in ('title', 'tab') if ref.get(key))
            return (f'<a class="cite" href="#source-{ref_id}" data-ref="{ref_id}" '
                    f'aria-label="查看引用 {ref_id}" title="{_esc(label)}">[{ref_id}]</a>')
        if svg and tag not in SVG_TAGS:
            raise ValueError('图表包含不支持的内容，无法完整保存；原稿未改变')
        if tag == 'svg' or svg:
            attrs = {}
            for key, val in raw.items():
                val = val or ''
                if key in ('href', 'filter', 'clip-path', 'mask'):
                    raise ValueError('图表含外部资源或不支持的引用，无法完整保存')
                if key in ('viewbox', 'points') and re.fullmatch(r'[0-9eE.,\s+\-]{1,4000}', val):
                    attrs['viewBox' if key == 'viewbox' else key] = val
                elif key == 'd' and re.fullmatch(r'[MmZzLlHhVvCcSsQqTtAa0-9eE.,\s+\-]{1,60000}', val):
                    attrs[key] = val
                elif key in ('x', 'y', 'x1', 'x2', 'y1', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'width', 'height', 'font-size', 'stroke-width', 'dx', 'dy') and re.fullmatch(r'-?\d+(?:\.\d+)?%?', val):
                    attrs[key] = val
                elif key in ('fill', 'stroke') and _color(val):
                    attrs[key] = _color(val)
                elif key in ('opacity', 'fill-opacity', 'stroke-opacity') and re.fullmatch(r'(?:0(?:\.\d+)?|1(?:\.0+)?)', val):
                    attrs[key] = val
                elif key == 'transform' and re.fullmatch(r'(?:(?:translate|scale|rotate|matrix|skewX|skewY)\([0-9eE.,\s+\-]+\)\s*){1,12}', val):
                    attrs[key] = val
                elif key == 'text-anchor' and val in ('start', 'middle', 'end'):
                    attrs[key] = val
                elif key in ('aria-label', 'role'):
                    attrs[key] = val[:500]
            return '<' + tag + attributes(attrs) + '>' + children(node, True) + '</' + tag + '>'
        if tag == 'font':
            # Chromium's formatting commands can emit legacy <font> wrappers.
            attrs = {}
            styles = [_style(raw.get('style') or '')]
            if _color(raw.get('color') or ''):
                styles.append('color:' + _color(raw['color']))
            sizes = {'1': '10px', '2': '13px', '3': '16px', '4': '18px', '5': '24px', '6': '32px', '7': '48px'}
            if raw.get('size') in sizes:
                styles.append('font-size:' + sizes[raw['size']])
            if raw.get('face'):
                styles.append(_style('font-family:' + raw['face']))
            if styles:
                attrs['style'] = ';'.join(v for v in styles if v)
            return '<span' + attributes(attrs) + '>' + children(node) + '</span>'
        if tag not in HTML_TAGS:
            return children(node)
        attrs = {}
        classes = [part for part in (raw.get('class') or '').split() if part in BODY_CLASSES]
        if classes:
            attrs['class'] = ' '.join(dict.fromkeys(classes))
        element_id = raw.get('id') or ''
        if re.fullmatch(r'(?:section|doc)-[A-Za-z0-9_.:-]{1,120}', element_id):
            attrs['id'] = element_id
        style = _style(raw.get('style') or '')
        if raw.get('align') in ('left', 'center', 'right', 'justify'):
            style += (';' if style else '') + 'text-align:' + raw['align']
        if style:
            attrs['style'] = style
        if raw.get('title'):
            attrs['title'] = raw['title'][:1000]
        for key in ('colspan', 'rowspan', 'span', 'start', 'value'):
            val = raw.get(key) or ''
            if ((tag in ('th', 'td') and key in ('colspan', 'rowspan'))
                    or (tag in ('col', 'colgroup') and key == 'span')
                    or (tag == 'ol' and key == 'start') or (tag == 'li' and key == 'value')):
                if re.fullmatch(r'[1-9][0-9]{0,5}', val):
                    attrs[key] = val
        if tag == 'th' and raw.get('scope') in ('col', 'row', 'colgroup', 'rowgroup'):
            attrs['scope'] = raw['scope']
        if tag == 'details' and 'open' in raw:
            attrs['open'] = ''
        if tag == 'a':
            href = (raw.get('href') or '').strip()
            local_link = bool(href and not href.startswith('#') and not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', href))
            if local_link or href.lower().startswith(('file:', 'blob:', 'data:')) or href.startswith(('/', '\\')) or re.match(r'^[a-zA-Z]:[\\/]', href):
                raise ValueError('正文含本机文件或嵌入资源链接，无法保证离线保留；请保留原稿并改用普通网址')
            safe_href = _link(href)
            if safe_href:
                attrs['href'] = safe_href
                attrs['rel'] = 'noopener noreferrer'
        return '<' + tag + attributes(attrs) + '>' + ('' if tag in VOID_TAGS else children(node) + '</' + tag + '>')

    def attributes(attrs):
        return ''.join(' ' + key + '="' + _esc(val) + '"' for key, val in attrs.items())

    result = children(parser.root)
    if len(result.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise ValueError('正文格式化后超过 1 MB，请精简后再保存')
    return result


def ensure_editable_report(report):
    adaptive = report.get('adaptive') or {}
    status = adaptive.get('status')
    if status in ('outline', 'reading_paused'):
        raise ValueError('阅读与提纲阶段的标题和要求已固定，请继续原任务或新建汇编')
    if adaptive and status != 'complete' and not any(
            any(str(item.get('text') or '').strip() for item in
                [*chapter.get('paragraphs', []), *chapter.get('segments', [])])
            or any(table.get('rows') for table in chapter.get('tables', []))
            for chapter in adaptive.get('chapters', [])):
        raise ValueError('文稿尚未生成，不能编辑空失败结果；资料与错误记录仍保留')
    digest = report.get('digest') or {}
    if digest.get('protocol') == 'whole-document-v1':
        batches = digest.get('batches') or []
        read_batches = digest.get('map_batches') or []
        if not any(batch.get('points') for batch in batches) and any(
                batch.get('status') not in ('complete', 'no_points') for batch in read_batches):
            raise ValueError('文稿尚未生成，不能编辑空失败结果；资料与错误记录仍保留')


def render_document(report, appendix=False):
    """Render the saved draft through the same boundary for reader and export."""
    body = sanitize_document_html(report['document_html'], report['refs'])
    output = ('<header class="report-heading"><div class="report-title-row"><h1>'
              + _esc(report['title']) + '</h1><span class="report-review">人工编辑 · 待核对</span>'
              '</div></header><div class="report-edited-body">' + body + '</div>')
    if appendix:
        from .report_structured_view import evidence_html
        used = {int(value) for value in re.findall(r'data-ref="(\d+)"', body)}
        output += evidence_html([ref for ref in report['refs'] if ref['id'] in used])
    return output
