# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Source presentation preserves structure without granting source HTML authority."""
import copy
import hashlib
from html import escape
from html.parser import HTMLParser
import unittest

from pocket_memory.report_structured_view import body_html, render_structured


class Elements(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class SourceMarkupTests(unittest.TestCase):
    def test_plain_office_lines_keep_their_visible_boundaries(self):
        lines = ['办公核对记录', '资料已经收齐', '审批单等待签字', '附件需要逐项复核']
        html = body_html('\n'.join(lines))
        self.assertEqual(sum(tag == 'br' for tag, _ in Elements(html).elements), len(lines) - 1)
        for line in lines:
            self.assertIn(line, html)
        self.assertNotIn('<h3>', html)

    def test_markdown_lists_emphasis_and_tables_are_structural(self):
        html = body_html('## 范围\n- **必要条件**\n  - *补充说明*\n'
                         '  - 不得省略\n\n3、 初次核对\n9、 最终确认\n\n'
                         '| 字段 | 条件 |\n| --- | --- |\n| 金额 | **8万元** |')
        tags = Elements(html).elements
        self.assertEqual(sum(tag == 'ul' for tag, _ in tags), 2)
        self.assertIn('<strong>必要条件</strong>', html)
        self.assertIn('<em>补充说明</em>', html)
        self.assertEqual([attrs['value'] for tag, attrs in tags if tag == 'li' and 'value' in attrs], ['3', '9'])
        self.assertIn(('ol', {'class': 'source-steps', 'start': '3'}), tags)
        self.assertIn('<th>字段</th>', html)
        self.assertIn('<td><strong>8万元</strong></td>', html)

    def test_html_blocks_and_table_boundaries_survive(self):
        html = body_html('<div><p>第一段</p><p>第二段</p><ul><li>甲</li><li>乙</li></ul></div>\n'
                         '<table><tr><th>名称</th><th>数量</th></tr>'
                         '<tr><td colspan="1">记录</td><td>2</td></tr></table>')
        self.assertIn('<p>第一段</p><p>第二段</p>', html)
        self.assertIn('<ul><li>甲</li><li>乙</li></ul>', html)
        self.assertIn('<td colspan="1">记录</td><td>2</td>', html)
        self.assertIn(('table', {'class': 'compiled-table'}), Elements(html).elements)

    def test_source_html_cannot_create_resources_links_or_citations(self):
        html = body_html('<div onclick="bad()"><script>bad_script()</script><style>bad_style</style>'
                         '<img src="https://invalid.example/image" onerror="bad()" alt="图示说明">'
                         '<a href="javascript:bad()" data-ref="99" class="cite">源链接文字</a>'
                         '<iframe src="https://invalid.example/frame">bad_frame</iframe>'
                         '<p style="background:url(https://invalid.example/image)">保留正文</p></div>')
        for tag, attrs in Elements(html).elements:
            self.assertNotIn(tag, ('script', 'style', 'img', 'iframe', 'a', 'object', 'svg'))
            self.assertFalse(set(attrs) & {'src', 'href', 'style', 'onclick', 'onerror', 'data-ref'})
            self.assertNotIn('cite', attrs.get('class', '').split())
        for rejected in ('bad_script', 'bad_style', 'bad_frame', 'invalid.example'):
            self.assertNotIn(rejected, html)
        self.assertIn('图示说明源链接文字', html)
        self.assertIn('<p>保留正文</p>', html)

    def test_code_examples_remain_literal_and_plain_steps_keep_original_numbers(self):
        html = body_html('前置说明\n3) 开始核对\n7) 完成\n\n'
                         '```text\n3、 **原样** [[标记]] <script>literal()</script>\n```\n\n'
                         '`[[行内示例]]` 与 [[可读链接]]')
        self.assertIn('<li value="3">', html)
        self.assertIn('<li value="7">', html)
        self.assertIn('3、 **原样** [[标记]] &lt;script&gt;literal()&lt;/script&gt;', html)
        self.assertIn('<code>[[行内示例]]</code>', html)
        self.assertIn('《可读链接》', html)

    def test_numeric_wrap_repairs_never_change_literal_code_blocks(self):
        literal = '3.\n照录\n<strong>2.\n5分钟</strong>\n## 1.\n标题'
        cases = ['```text\n' + literal + '\n```',
                 '\n'.join('    ' + line for line in literal.splitlines()),
                 '<pre><code>' + escape(literal) + '</code></pre>',
                 '<div><pre><code>' + escape(literal) + '</code></pre></div>']
        for source in cases:
            with self.subTest(source=source):
                html = body_html(source)
                self.assertIn(escape(literal), html)
                self.assertNotIn('<li', html)

    def test_ragged_tables_keep_all_original_rows_as_text(self):
        for row in ('| 项目甲 | 8万元 | 不得提前支付 |', '| 项目甲 |'):
            table = '| 项目 | 金额 |\n| --- | --- |\n' + row
            with self.subTest(row=row):
                html = body_html('核对表\n\n' + table + '\n\n后续说明')
                self.assertIn('<pre><code>' + escape(table) + '\n</code></pre>', html)
                self.assertNotIn('<table', html)
                self.assertIn('核对表', html)
                self.assertIn('后续说明', html)

    def test_rectangular_table_uses_parser_escaped_pipe_rules(self):
        html = body_html('| 项目 | 条件 |\n| --- | --- |\n| 甲\\|乙 | 完整保留 |')
        self.assertIn('<table', html)
        self.assertNotIn('<pre>', html)
        self.assertIn('<td>甲|乙</td>', html)
        self.assertIn('<td>完整保留</td>', html)

    def test_rendering_keeps_saved_source_ranges_and_escapes_source_label(self):
        body = '## 条件\n- **不得执行**，除非获批准。\n- 必须保留记录。'
        unit = dict(unit_id='u1', kind='note', source_id=1, note_id=1, title='原文',
                    tab='正文', field='content', body=body,
                    revision=hashlib.sha256(body.encode()).hexdigest())
        ref = {key: value for key, value in unit.items() if key != 'body'}
        ref.update(id=1, start=0, end=len(body), quote=body)
        saved = dict(title='汇编', units=[unit], refs=[ref], notes={},
            adaptive=dict(chapters=[dict(id='C1', title='条件', source_label='<img src=x>来源',
                segments=[dict(unit_id='u1', start=0, end=len(body), text=body, ref=1)])]))
        saved['adaptive']['chapters'].append(dict(id='C2', title='条件', source_label='第二来源', segments=[]))
        before = copy.deepcopy(saved)
        html = render_structured(saved, appendix=True)
        self.assertEqual(saved, before)
        self.assertIn('<p class="source-note-label">&lt;img src=x&gt;来源</p>', html)
        self.assertIn('href="#section-chapter-C1">&lt;img src=x&gt;来源 · 条件</a>', html)
        self.assertIn('href="#section-chapter-C2">第二来源 · 条件</a>', html)
        self.assertNotIn('<h3>条件</h3>', html)
        self.assertIn('<strong>不得执行</strong>，除非获批准。', html)
        self.assertIn('data-ref="1"', html)
        self.assertIn('id="source-1"', html)


if __name__ == '__main__':
    unittest.main()
