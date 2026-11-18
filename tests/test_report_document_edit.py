# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Full-report editing at the existing persistence/render/export boundary.

All source snapshots are fixtures. No model, server or user library is started.
"""
import copy
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pocket_memory.report_document import MAX_DOCUMENT_BYTES, sanitize_document_html
from pocket_memory.report_exports import ReportExportStore
from pocket_memory.report_workspace import (ReportWorkspace, chart_svg, digest,
    export_html, make_ref, render_report)


class ReportDocumentEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.workspace = ReportWorkspace(SimpleNamespace(data_dir=self.directory),
                                         SimpleNamespace(), threading.RLock())
        body = '服务启用前必须审批。负责人为林女士。'
        unit = dict(unit_id='n1:content', kind='note', source_id=1, note_id=1,
                    title='审批资料', tab='正文', field='content', revision=digest(body), body=body)
        refs = [make_ref(unit, 0, 10), make_ref(unit, 10, len(body))]
        for index, ref in enumerate(refs, 1):
            ref['id'] = index
        self.report = dict(id='a'*32, version=1, created='2026-11-18T00:00:00Z',
            updated='2026-11-18T00:00:00Z', title='原汇编标题', template='auto', template_label='资料汇编',
            selected=[dict(kind='note', id=1, title='审批资料')], units=[unit], refs=refs,
            claims=[], records=[], tables=[], actions=[], charts=[], notes={'chapter-C1': '旧的分章补充'},
            edited=False, reviewed=True, generation={'raw_claims': ['保留原始模型记录']},
            adaptive=dict(status='complete', reading_mode='source_brief_v2', state='compiled',
                facts=[{'id': 'F1', 'text': body}], chapters=[dict(id='C1', title='原章节',
                    paragraphs=[dict(text=body, refs=[1, 2])], tables=[], guidance=[],
                    content_order=[dict(kind='paragraph', index=0)])]))
        self.workspace.write(self.report)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, document, **fields):
        payload = {'version': self.workspace.read(self.report['id'])['version'],
                   'title': '人工修改标题', 'document_html': document, **fields}
        return self.workspace.save_edit(self.report['id'], payload)

    def test_roundtrip_changes_only_edit_fields_and_keeps_original_evidence(self):
        text = ('<section id="section-chapter-C1" class="section adaptive-chapter"><h2>改写的章节</h2>'
                '<p style="font-size:20px;color:#123456;background-color:#fff0aa;text-align:center">'
                '<strong>人工合并内容</strong><em>斜体</em><u>下划线</u><s>删除线</s>'
                '<a data-ref="2" href="https://wrong.test" aria-label="伪造">伪造文本</a></p>'
                '<ol start="3"><li value="3">新增段落</li></ol><blockquote>人工补充</blockquote></section>')
        result = self.save(text)
        reopened = ReportWorkspace(SimpleNamespace(data_dir=self.directory), None, threading.RLock()).read(result['id'])
        self.assertEqual(result, reopened)
        self.assertEqual(result['version'], 2)
        self.assertTrue(result['edited'])
        self.assertFalse(result['reviewed'])
        for key in ('adaptive', 'units', 'refs', 'generation', 'notes', 'selected', 'claims'):
            self.assertEqual(result[key], self.report[key], key)
        self.assertIn('font-size:20px;color:#123456;background-color:#fff0aa;text-align:center', result['document_html'])
        self.assertIn('<a class="cite" href="#source-2" data-ref="2" aria-label="查看引用 2"', result['document_html'])
        self.assertIn('>[2]</a>', result['document_html'])
        self.assertNotIn('伪造文本', result['document_html'])
        self.assertNotIn('wrong.test', result['document_html'])
        rendered = render_report(reopened)
        self.assertIn('<h1>人工修改标题</h1>', rendered)
        self.assertIn('改写的章节', rendered)
        self.assertNotIn('原章节', rendered)
        self.assertNotIn('data-edit=', rendered)
        self.assertNotIn('id="report-source-appendix"', reopened['document_html'])

    def test_export_is_real_file_and_appendix_uses_only_actual_body_references(self):
        result = self.save('<h2>单处引用</h2><p>已删去引用一，仅保留 <a data-ref="2">[999]</a></p>')
        exports = ReportExportStore(self.directory)
        receipt = exports.create(self.workspace, result['id'], result['version'])
        exported = Path(receipt['path']).read_text('utf-8')
        self.assertEqual(exported, export_html(result))
        self.assertIn('单处引用', exported)
        self.assertIn('id="source-2"', exported)
        self.assertNotIn('id="source-1"', exported)
        self.assertEqual(set(re.findall(r'data-ref="(\d+)"', exported)), {'2'})
        self.assertEqual(set(re.findall(r'id="source-(\d+)"', exported)), {'2'})
        self.assertIn('<script data-report-source-links>', exported)
        self.assertEqual(exported.count('<script'), 1)
        self.assertNotIn('<script', result['document_html'])

    def test_table_and_generated_chart_survive_save_and_export(self):
        chart = chart_svg(dict(title='清单计数', series=[dict(label='已完成', value=2), dict(label='待办', value=1)]))
        table = '<table><caption>完整表</caption><thead><tr><th scope="col">状态</th></tr></thead><tbody><tr><td rowspan="2">已完成</td></tr></tbody></table>'
        result = self.save('<h2>图表</h2>' + chart + table)
        for output in (result['document_html'], render_report(result), export_html(result)):
            self.assertIn('<svg viewBox="0 0 520 138"', output)
            self.assertIn('已完成</text>', output)
            self.assertIn('width="360.0"', output)
            self.assertIn('>2</text>', output)
            self.assertIn('>1</text>', output)
            self.assertIn('<th scope="col">状态</th>', output)
            self.assertIn('<td rowspan="2">已完成</td>', output)

    def test_unknown_citation_cannot_save_or_change_revision(self):
        before = self.workspace.path(self.report['id']).read_bytes()
        for fragment in ('<a data-ref="3">[3]</a>', '<a data-ref="0">[0]</a>',
                         '<a data-ref="1.0">[1]</a>', '<span data-ref="1">[1]</span>'):
            with self.subTest(fragment=fragment), self.assertRaisesRegex(ValueError, '未知引用'):
                self.save(fragment)
        self.assertEqual(self.workspace.path(self.report['id']).read_bytes(), before)

    def test_sanitizer_removes_executable_markup_events_and_unsafe_css(self):
        result = self.save('<script>alert(1)</script><style>@import "https://evil.test";</style>'
            '<p onclick="alert(1)" style="font-size:18px;position:fixed;background:url(https://evil.test);'
            'color:expression(alert(1));text-align:right">正文</p>'
            '<a href="java&#x73;cript:alert(1)">危险链接文本</a>'
            '<div id="source-1" contenteditable="true" data-edit="summary">不能抢占引用定位</div>')
        output = result['document_html']
        self.assertIn('font-size:18px;text-align:right', output)
        self.assertIn('危险链接文本', output)
        for forbidden in ('<script', '<style', 'onclick', 'alert(', 'javascript:', 'position:', 'url(', 'id="source-1"', 'data-edit', 'contenteditable'):
            self.assertNotIn(forbidden, output)

    def test_local_and_remote_resources_are_rejected_without_data_loss(self):
        before = self.workspace.path(self.report['id']).read_bytes()
        for resource in ('<img src="/api/files/1">', '<img src="https://remote.test/image.png">',
                         '<a href="file:///C:/private/file.pdf">原文件</a>',
                         '<a href=" file:///C:/private/file.pdf ">原文件</a>',
                         '<a href="C:\\private\\file.pdf">原文件</a>',
                         '<a href="attachments/file.pdf">原文件</a>',
                         '<iframe src="https://remote.test"></iframe>',
                         '<svg><image href="/api/files/1"/></svg>',
                         '<svg><use href="#shape"/></svg>'):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.save(resource)
        self.assertEqual(self.workspace.path(self.report['id']).read_bytes(), before)

    def test_safe_links_and_browser_font_markup_are_preserved(self):
        result = self.save('<p id="doc-extra"><font size="5" color="#ff0000" face="Arial">字号颜色</font>'
            '<a href="https://example.com/a?q=1&amp;b=2">网址</a><a href="mailto:author@example.com">邮件</a>'
            '<a href="#doc-extra">内部锚点</a><a href="#source-99">不可冒充原文引用</a></p>')
        body = result['document_html']
        self.assertIn('color:#ff0000;font-size:24px;font-family:arial', body)
        self.assertIn('href="https://example.com/a?q=1&amp;b=2"', body)
        self.assertIn('href="mailto:author@example.com"', body)
        self.assertIn('href="#doc-extra"', body)
        self.assertNotIn('href="#source-99"', body)

    def test_first_edit_can_be_undone_and_original_renderer_returns(self):
        original_render = render_report(self.report)
        saved = self.save('<p>临时人工稿</p>')
        undone = self.workspace.save_edit(saved['id'], {'version': 2, 'undo_document_edit': True})
        self.assertEqual(undone['version'], 3)
        self.assertNotIn('document_html', undone)
        self.assertNotIn('last_document_edit', undone)
        for key in ('title', 'notes', 'edited', 'reviewed', 'adaptive', 'refs', 'units', 'generation'):
            self.assertEqual(undone[key], self.report[key], key)
        self.assertEqual(render_report(undone), original_render)
        with self.assertRaisesRegex(ValueError, '没有可撤回'):
            self.workspace.save_edit(saved['id'], {'version': 3, 'undo_document_edit': True})

    def test_second_save_undo_restores_exact_previous_manual_draft(self):
        first = self.save('<p>第一次 <a data-ref="1">[1]</a></p>')
        second = self.save('<p>第二次</p>', title='第二次标题')
        undone = self.workspace.save_edit(second['id'], {'version': 3, 'undo_document_edit': True})
        self.assertEqual(undone['document_html'], first['document_html'])
        self.assertEqual(undone['title'], first['title'])
        self.assertTrue(undone['edited'])
        self.assertFalse(undone['reviewed'])
        self.assertEqual(undone['version'], 4)

    def test_legacy_title_save_updates_snapshot_without_losing_full_body(self):
        first = self.save('<p>保留完整人工稿</p>')
        changed = self.workspace.save_edit(first['id'], {'version': 2, 'title': '后续只改标题',
                                                       'notes': {'chapter-C1': '后续说明'}})
        self.assertEqual(changed['document_html'], first['document_html'])
        undone = self.workspace.save_edit(first['id'], {'version': 3, 'undo_document_edit': True})
        self.assertEqual(undone['title'], first['title'])
        self.assertEqual(undone['notes'], first['notes'])
        self.assertEqual(undone['document_html'], first['document_html'])

    def test_stale_save_and_undo_never_overwrite_newer_version(self):
        self.save('<p>较新内容</p>')
        before = self.workspace.path(self.report['id']).read_bytes()
        for payload in ({'version': 1, 'document_html': '<p>旧内容</p>'},
                        {'version': 1, 'undo_document_edit': True},
                        {'version': True, 'document_html': '<p>旧内容</p>'}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, '文稿已更新'):
                self.workspace.save_edit(self.report['id'], payload)
        self.assertEqual(self.workspace.path(self.report['id']).read_bytes(), before)

    def test_failed_atomic_write_leaves_previous_report_and_history_unchanged(self):
        self.save('<p>已保存正文</p>')
        before = self.workspace.path(self.report['id']).read_bytes()
        with patch('pocket_memory.report_workspace.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.save('<p>不能写入的新正文</p>')
        self.assertEqual(self.workspace.path(self.report['id']).read_bytes(), before)

    def test_reading_outline_and_empty_failures_cannot_be_made_into_edited_report(self):
        for status, chapters in (('outline', self.report['adaptive']['chapters']),
                                 ('reading_paused', []), ('partial', []),
                                 ('partial', [dict(id='C1', paragraphs=[], tables=[])]),
                                 ('partial', [dict(id='C1', paragraphs=[{'text': ''}])])):
            report = copy.deepcopy(self.report)
            report['adaptive'].update(status=status, chapters=chapters)
            self.workspace.write(report)
            with self.subTest(status=status, chapters=chapters), self.assertRaises(ValueError):
                self.save('<p>不能冒充已经成稿</p>')
            self.assertEqual(self.workspace.read(report['id']), report)

    def test_completed_no_match_and_partial_report_with_saved_body_are_editable(self):
        report = copy.deepcopy(self.report)
        report['adaptive'].update(status='complete', state='no_match', chapters=[])
        self.workspace.write(report)
        self.assertIn('无匹配后的人工补充', self.save('<p>无匹配后的人工补充</p>')['document_html'])
        report = copy.deepcopy(self.report)
        report['adaptive']['status'] = 'partial'
        self.workspace.write(report)
        self.assertIn('补完现有部分成稿', self.save('<p>补完现有部分成稿</p>')['document_html'])

    def test_empty_body_is_intentional_edit_and_title_is_escaped(self):
        saved = self.save('', title='<img src=x onerror=alert(1)>')
        self.assertEqual(saved['document_html'], '')
        output = export_html(saved)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', output)
        self.assertNotIn('<img', output)
        self.assertNotIn('id="report-source-appendix"', output)
        self.assertNotIn('原章节', output)

    def test_size_and_structure_limits_are_checked_before_persistence(self):
        before = self.workspace.path(self.report['id']).read_bytes()
        for content in (None, '文' * (MAX_DOCUMENT_BYTES // 3 + 1), '<div>' * 130 + '过深' + '</div>' * 130):
            with self.subTest(kind=type(content).__name__), self.assertRaises(ValueError):
                self.save(content)
        self.assertEqual(self.workspace.path(self.report['id']).read_bytes(), before)

    def test_duplicate_attributes_and_embedded_appendix_are_rejected(self):
        for text in ('<a data-ref="1" data-ref="2">伪造</a>',
                     '<details id="report-source-appendix"><summary>引用核对</summary></details>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                sanitize_document_html(text, self.report['refs'])


if __name__ == '__main__':
    unittest.main()
