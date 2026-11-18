# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Exact-source structural plans: no model, product server or personal library."""
import copy
import hashlib
import unittest

from pocket_memory import report_structured as structured


def unit(uid, body, source_id=1, kind='document', field='document_text'):
    return dict(unit_id=uid, body=body, source_id=source_id, kind=kind, field=field,
                title='资料', revision=hashlib.sha256(body.encode('utf-8')).hexdigest())


def chapter_text(chapter):
    return ''.join(span['text'] for span in chapter['segments'])


class StructuredSourcePlanTests(unittest.TestCase):
    def assert_exact_coverage(self, originals, result):
        by_id = {item['unit_id']: item for item in originals}
        spans = [span for chapter in result['chapters'] for span in chapter['segments']]
        spans += result['omissions']
        for uid, original in by_id.items():
            owners = [0] * len(original['body'])
            for span in (span for span in spans if span['unit_id'] == uid):
                self.assertEqual(span['text'], original['body'][span['start']:span['end']])
                for index in range(span['start'], span['end']):
                    owners[index] += 1
            for index, char in enumerate(original['body']):
                self.assertLessEqual(owners[index], 1)
                if not char.isspace():
                    self.assertEqual(owners[index], 1, (uid, index, char))
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['coverage']['covered_nonspace_chars'],
                         result['coverage']['total_nonspace_chars'])
        self.assertEqual(result['coverage']['model_calls'], 0)

    def test_generic_peer_headings_keep_lower_headings_and_conditions(self):
        originals = [unit('p1', '## 方案甲\n条件：批准后执行。\n### 步骤\n1. 核对。\n'
                              '否则保留草稿。\n## 方案乙\n输入：现有记录。\n### 步骤\n2. 检查。')]
        result = structured.plan(originals)
        self.assertEqual([c['title'] for c in result['chapters']], ['方案甲', '方案乙'])
        self.assertIn('### 步骤\n1. 核对。\n否则保留草稿。', chapter_text(result['chapters'][0]))
        self.assertEqual(result['coverage']['heading_level'], 2)
        self.assert_exact_coverage(originals, result)

    def test_defers_for_focus_multiple_sources_ocr_or_insufficient_structure(self):
        body = '## 第一项\n内容甲。\n## 第二项\n内容乙。'
        cases = [
            ([unit('p1', body)], '仅保留第一项'),
            ([unit('p1', body), unit('p2', body, source_id=2)], ''),
            ([unit('p1', body), unit('p2', body, kind='note', field='content')], ''),
            ([unit('p1', body, field='ocr_text')], ''),
            ([unit('p1', '只有普通正文。')], ''),
            ([unit('p1', '## 唯一标题\n正文。')], ''),
            ([unit('p1', '## 相同标题\n甲。\n## 相同标题\n乙。')], ''),
            ([unit('p1', '## 第一项\n## 第二项\n')], ''),
        ]
        for originals, focus in cases:
            with self.subTest(originals=originals, focus=focus):
                self.assertIsNone(structured.plan(originals, focus))

    def test_repeated_page_headers_and_continuation_tips_stay_with_previous_section(self):
        originals = [
            unit('p1', '# 使用手册\n## 1.\n准备阶段\n准备记录。\n'),
            unit('p2', '# 使用手册\n> 注意：沿用前一阶段的限制。\n\n## 2.\n核对阶段\n核对来源。\n'),
            unit('p3', '# 使用手册\n核对后的补充说明。\n'),
        ]
        result = structured.plan(originals)
        self.assertEqual([c['title'] for c in result['chapters']], ['1. 准备阶段', '2. 核对阶段'])
        self.assertIn('注意：沿用前一阶段的限制', chapter_text(result['chapters'][0]))
        self.assertNotIn('## 2.', chapter_text(result['chapters'][0]))
        self.assertIn('核对后的补充说明', chapter_text(result['chapters'][1]))
        self.assertEqual([o['reason'] for o in result['omissions']], ['repeated_document_header'] * 3)
        self.assert_exact_coverage(originals, result)

    def test_valid_flat_frontmatter_and_standalone_navigation_have_receipts(self):
        originals = [unit('p1', '\ufeff---\ntitle: "资料说明"\ncategory: 通用\n'
                              'tags: [准备, 核对]\nupdated_at: 2026-11-18T08:00:00+08:00\n---\n'
                              '## 第一项\n正文甲。\n[[相关资料]]\n'
                              '## 第二项\n正文乙含[[内嵌名称]]，不能删掉。\n')]
        result = structured.plan(originals)
        self.assertEqual([o['reason'] for o in result['omissions']],
                         ['yaml_frontmatter', 'standalone_wiki_navigation'])
        shown = '\n'.join(chapter_text(c) for c in result['chapters'])
        self.assertNotIn('updated_at:', shown)
        self.assertIn('正文乙含[[内嵌名称]]', shown)
        self.assert_exact_coverage(originals, result)

    def test_invalid_or_unclosed_metadata_is_retained_in_introduction(self):
        for prefix in ('---\n这不是映射。\n---\n', '---\ntitle: "未闭合\n---\n',
                       '---\ntitle: 仍无结束分隔线\n'):
            with self.subTest(prefix=prefix):
                originals = [unit('p1', prefix + '## 第一项\n正文甲。\n## 第二项\n正文乙。')]
                result = structured.plan(originals)
                self.assertEqual(result['chapters'][0]['title'], '引言')
                self.assertIn(prefix, chapter_text(result['chapters'][0]))
                self.assertFalse(any(o['reason'] == 'yaml_frontmatter' for o in result['omissions']))
                self.assert_exact_coverage(originals, result)

    def test_substantive_preamble_is_not_lost(self):
        originals = [unit('p1', '适用范围：内部记录。\n未批准不得发布。\n\n'
                              '## 第一部分\n正文甲。\n## 第二部分\n正文乙。')]
        result = structured.plan(originals)
        self.assertEqual(result['chapters'][0]['title'], '引言')
        self.assertIn('未批准不得发布', chapter_text(result['chapters'][0]))
        self.assert_exact_coverage(originals, result)

    def test_fenced_examples_are_not_headings_or_navigation_noise(self):
        originals = [unit('p1', '## 第一部分\n正文甲。\n```markdown\n## 示例一\n[[不要删除]]\n'),
                     unit('p2', '## 示例二\n```\n## 第二部分\n正文乙。')]
        result = structured.plan(originals)
        self.assertEqual([c['title'] for c in result['chapters']], ['第一部分', '第二部分'])
        self.assertIn('## 示例二', chapter_text(result['chapters'][0]))
        self.assertIn('[[不要删除]]', chapter_text(result['chapters'][0]))
        self.assertEqual(result['omissions'], [])
        self.assert_exact_coverage(originals, result)

    def test_only_fenced_or_indented_headings_do_not_activate(self):
        for body in ('```\n## 假标题甲\n正文。\n## 假标题乙\n正文。\n```',
                     '    ## 假标题甲\n    正文。\n    ## 假标题乙\n    正文。'):
            with self.subTest(body=body):
                self.assertIsNone(structured.plan([unit('p1', body)]))

    def test_split_decimal_is_reported_without_repairing_source_or_segments(self):
        originals = [unit('p1', '## 第一项\n等待<strong>2.\n5～3小时</strong>。\n'
                              '## 第二项\n记录原始数值。')]
        before = copy.deepcopy(originals)
        result = structured.plan(originals)
        self.assertEqual(originals, before)
        self.assertIn('2.\n5～3小时', chapter_text(result['chapters'][0]))
        issue = result['coverage']['display_issues'][0]
        self.assertEqual(issue['kind'], 'possible_split_decimal')
        self.assertEqual(issue['text'], '2.\n5')
        self.assertTrue(issue['uncertain'])
        self.assertEqual(originals[0]['body'][issue['start']:issue['end']], issue['text'])
        self.assert_exact_coverage(originals, result)

    def test_crlf_and_split_heading_across_document_units_keep_exact_offsets(self):
        originals = [unit('p1', '# 手册\r\n## 1.\r\n'),
                     unit('p2', '# 手册\r\n准备\r\n正文甲。\r\n## 2. 核对\r\n正文乙。\r\n')]
        result = structured.plan(originals, ' \t')
        self.assertEqual([c['title'] for c in result['chapters']], ['1. 准备', '2. 核对'])
        self.assertEqual(len(result['chapters'][0]['segments']), 2)
        self.assert_exact_coverage(originals, result)

    def test_note_structure_can_be_used_but_duplicate_note_headers_are_not_noise(self):
        originals = [unit('n1', '# 页签甲\n说明甲。\n# 页签乙\n说明乙。',
                          kind='note', field='content')]
        result = structured.plan(originals)
        self.assertEqual([c['title'] for c in result['chapters']], ['页签甲', '页签乙'])
        self.assertEqual(result['omissions'], [])
        self.assert_exact_coverage(originals, result)

    def test_duplicate_unit_identity_and_unknown_channels_defer(self):
        body = '## 第一项\n甲。\n## 第二项\n乙。'
        self.assertIsNone(structured.plan([unit('p1', body), unit('p1', body)]))
        self.assertIsNone(structured.plan([unit('p1', body, field='unknown')]))
        self.assertIsNone(structured.plan([]))


if __name__ == '__main__':
    unittest.main()
