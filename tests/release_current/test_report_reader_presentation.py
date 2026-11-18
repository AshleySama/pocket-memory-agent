# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Reader-facing report contracts; no model generation or personal data."""
import copy
import hashlib
import re
import unittest

from pocket_memory.report_adaptive_view import render_adaptive
from pocket_memory.report_compiled_view import render_compiled
from pocket_memory.report_digest_view import render_digest
from pocket_memory.report_structured_view import body_html, render_structured


def source(uid, body):
    return dict(unit_id=uid, kind='document', source_id=1, note_id=None,
                title='很长的原始资料文件名_用于核对出处', tab='正文', field='document_text',
                revision=hashlib.sha256(body.encode('utf-8')).hexdigest(), body=body)


def reference(item, ref_id):
    result = {key: value for key, value in item.items() if key != 'body'}
    result.update(id=ref_id, start=0, end=len(item['body']), quote=item['body'])
    return result


def report(mode):
    body = '## 1. 核对材料\n材料：记录8g、清单2份。\n### 步骤\n1. 核对原始记录。\n3. 条件不符时不得执行。\n> 保留人工判断。'
    units = [source('p1', body), source('p2', '未选入正文的独立来源内容。')]
    chapter = dict(id='C1', title='1. 核对材料', purpose='核对材料', status='complete',
                   segments=[dict(unit_id='p1', start=0, end=len(body), text=body, ref=1)],
                   paragraphs=[dict(text=body, refs=[1])], tables=[],
                   content_order=[dict(kind='paragraph', index=0)], guidance=[])
    return dict(id='a'*32, title='专题内容', version=3, created='2026-11-18',
                template_label='专题报告', selected=[dict(kind='document', id=1, title='资料')],
                units=units, refs=[reference(item, index) for index, item in enumerate(units, 1)],
                notes={'chapter-C1': '人工补充仍保留。'}, focus='',
                generation=dict(calls=9, seconds=123.4), planning_generation=dict(calls=2, seconds=10),
                limitations=['工程保留记录，不属于读者正文。'],
                adaptive=dict(reading_mode=mode, status='complete', chapters=[chapter],
                              coverage=dict(total_units=2, read_units=2), issues=[]))


class ReaderPresentationTests(unittest.TestCase):
    def test_completed_report_modes_drop_engineering_copy_without_mutating_storage(self):
        cases = [('source_structured_v1', render_structured),
                 ('source_compiled_v1', render_compiled),
                 ('source_brief_v2', render_compiled),
                 ('model_written', render_adaptive)]
        for mode, renderer in cases:
            with self.subTest(mode=mode):
                saved = report(mode)
                before = copy.deepcopy(saved)
                html = renderer(saved, appendix=True)
                for unwanted in ('编制说明', '资料覆盖', '次新调用', '本地4B', '本地 4B',
                                 '完整快照', 'report-footer', '<summary>参考资料</summary>',
                                 '工程保留记录，不属于读者正文。'):
                    self.assertNotIn(unwanted, html)
                self.assertIn('人工补充仍保留。', html)
                self.assertIn('条件不符时不得执行。', html)
                self.assertEqual(saved, before)

    def test_cited_passages_keep_anchors_but_exclude_unused_sources(self):
        for mode, renderer in [('source_structured_v1', render_structured),
                               ('source_compiled_v1', render_compiled),
                               ('model_written', render_adaptive)]:
            with self.subTest(mode=mode):
                html = renderer(report(mode), appendix=True)
                details = re.search(r'<details[^>]*id="report-source-appendix"[^>]*>', html)[0]
                self.assertNotRegex(details, r'\sopen(?:\s|>|=)')
                self.assertIn('href="#source-1"', html)
                self.assertIn('data-ref="1"', html)
                self.assertIn('id="source-1"', html)
                self.assertNotIn('id="source-2"', html)
                self.assertNotIn('未选入正文的独立来源内容', html)

    def test_source_heading_has_no_second_generated_chapter_number(self):
        saved = report('source_structured_v1')
        html = render_structured(saved)
        self.assertIn('<h2>1. 核对材料</h2>', html)
        self.assertNotIn('section-number', html)
        self.assertNotIn('<h3>1. 核对材料</h3>', html)
        self.assertIn('title="很长的原始资料文件名_用于核对出处 · 正文"', html)
        self.assertNotIn('很长的原始资料文件名_用于核对出处</p>', html)

    def test_steps_retain_original_numbers_conditions_and_formatting(self):
        html = body_html('## 1.\n主题\n材料：记录8g。\n### 步骤\n1. 核对。\n'
                         '3. <strong>不得执行</strong>，除非获批准。\n'
                         '> 不得省略此条件。', '1. 主题')
        self.assertIn('class="source-field"', html)
        self.assertIn('<h3>步骤</h3>', html)
        self.assertIn('<li value="1">', html)
        self.assertIn('<li value="3">', html)
        self.assertNotIn('<li value="2">', html)
        self.assertIn('<strong>不得执行</strong>，除非获批准。', html)
        self.assertIn('不得省略此条件。', html)

    def test_digest_keeps_only_visible_citations_and_real_incomplete_notice(self):
        saved = report('model_written')
        saved['notes'] = {'gaps': '人工核对说明。'}
        saved['digest'] = dict(
            protocol='whole-document-v1',
            sections=[dict(title='1. 主题', batch_ids=[1])],
            batches=[dict(id=1, heading='1. 主题', status='complete',
                          points=[dict(text='原始记录需要核对。', refs=[1])])],
            map_batches=[dict(id=1, status='incomplete', source_units=['p1'])],
            coverage=dict(total_units=2, read_units=1, total_batches=1, completed_batches=0))
        before = copy.deepcopy(saved)
        html = render_digest(saved, appendix=True)
        self.assertIn('归纳尚未完成', html)
        self.assertIn('人工核对说明。', html)
        self.assertIn('id="source-1"', html)
        self.assertNotIn('id="source-2"', html)
        self.assertIn('<details class="panel full-source">', html)
        for unwanted in ('已处理', '模型阅读', 'report-footer', '完整快照', 'section-number'):
            self.assertNotIn(unwanted, html)
        self.assertEqual(saved, before)


if __name__ == '__main__':
    unittest.main()
