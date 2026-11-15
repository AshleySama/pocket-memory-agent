# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import copy
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory.report_sections import list_parts, groups, presentation_records, card_group
from pocket_memory.report_workspace import render_report, export_html
from test_report_workspace import WorkspaceTests


class ReadingNoise(unittest.TestCase):
    def test_wrapped_ip_and_decimals_are_not_numbered_lists(self):
        for text in ('四元组（10.\n23.\n15.\n3，59461，\n192.\n168.\n8.\n135，8080）\n源端口自动分配。',
                     '1.25万元\n2.50万元', '1.\n正文\n2.\n其他正文'):
            self.assertEqual(list_parts(text), ('', []))

    def test_explicit_numbers_and_conditions_survive(self):
        lead, items = list_parts('两项必须同时满足：\n1、30天内提交\n2、50%以上通过\n不得省略任一条件。')
        self.assertEqual(lead, '两项必须同时满足：')
        self.assertEqual(items, ['30天内提交', '50%以上通过\n不得省略任一条件。'])
        self.assertEqual(list_parts('1. Mysql隔离级别\n不能省略条件\n2. 幻读定义')[1],
                         ['Mysql隔离级别\n不能省略条件', '幻读定义'])
        self.assertEqual(list_parts('1. 30天内提交\n2. 50%以上通过')[1], ['30天内提交', '50%以上通过'])

    def test_semicolon_conditions_are_a_complete_paragraph(self):
        text = '以下四个条件同时满足：互斥；请求保持；不可抢占；循环等待。互斥条件不能破坏。'
        result = groups([dict(field='document_text', ref=1, title='条件', text=text)])
        self.assertEqual(result[0]['paragraph'], text)
        rendered = card_group(result[0], str, str)
        self.assertIn(text, rendered)
        self.assertNotIn('item-number', rendered)

    def make_report(self, tail, count=5, kind='document', field='document_text'):
        r = dict(units=[], refs=[], records=[])
        for i in range(count):
            body = '参数：端口8080。\n1、先检查版本\n2、条件不符不得执行。\n' + tail
            uid = f'd1:{i+1}'
            r['units'].append(dict(kind=kind, field=field, source_id=1, unit_id=uid, body=body))
            r['refs'].append(dict(id=i+1, unit_id=uid, start=0, end=len(body), quote=body))
            r['records'].append(dict(ref=i+1, unit_id=uid, field=field, title='文档', text=body))
        return r

    def test_proven_footer_removed_only_from_view(self):
        tail = '示例学习资料\n学习资料\n示例学习资料\n示例学习'
        r = self.make_report(tail)
        before = copy.deepcopy(r)
        shown = presentation_records(r)
        self.assertTrue(all('示例学习' not in x['text'] for x in shown))
        self.assertTrue(all('端口8080' in x['text'] and '条件不符不得执行' in x['text'] for x in shown))
        self.assertEqual(r, before)

    def test_notes_ocr_single_page_and_warnings_are_not_cleaned(self):
        tail = '示例学习资料\n学习资料\n示例学习资料\n示例学习'
        for r in (self.make_report(tail, count=1), self.make_report(tail, kind='note', field='content'),
                  self.make_report(tail, field='ocr_text'),
                  self.make_report('禁止用于生产。\n必须人工复核。\n禁止用于生产。')):
            self.assertEqual(presentation_records(r), r['records'])


class EmptySections(WorkspaceTests):
    def test_empty_learning_sections_hidden_but_manual_notes_retained(self):
        r = self.build('learning')
        r.update(records=[], actions=[], tables=[], claims=[])
        out = render_report(r)
        for key in ('chart', 'actions', 'gaps'):
            self.assertNotIn('id="section-' + key + '"', out)
        self.assertNotIn('学习核对：先对照', out)
        r['notes'] = {key: '人工说明-' + key for key in ('chart', 'actions', 'gaps')}
        out = render_report(r)
        for key in r['notes']:
            self.assertIn('人工说明-' + key, out)

    def test_offline_appendix_collapsed_and_all_snapshots_retained(self):
        r = self.build('learning')
        before = copy.deepcopy(r)
        out = export_html(r)
        self.assertIn('<details class="section appendix" id="report-source-appendix">', out)
        for ref in r['refs']:
            self.assertIn('id="source-' + str(ref['id']) + '"', out)
        self.assertEqual(r, before)


if __name__ == '__main__':
    unittest.main()
