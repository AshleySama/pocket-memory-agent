# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Frozen source-heading projection contracts; no model or external database."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory.rag.retained.section_projection import SectionProjection, validate_sections


def note(i, title, content, parent=None, tab=''):
    return dict(id=i, title=title, content=content, parent_note_id=parent, tab_name=tab)


BODY = ('背景：\n资料分别保存在制度、会议记录和项目周报中。不能把模型输出当成正式审批。\n\n'
        '场景：\n研发可搜索历史方案；生产可检查异常记录。每个场景均须保留人工确认点。\n\n'
        '落地条件：\n数据来源清楚；问题边界明确；人员职责保留；效果可衡量。未满足条件不得上线。')


class SectionProjectionBehavior(unittest.TestCase):
    def setUp(self):
        self.notes = {31: note(31, 'AI 赋能制造业项目', BODY)}

    def ask(self, question, notes=None, ocr=None):
        prepared = SectionProjection(self.notes if notes is None else notes, ocr).prepare(question)
        return prepared['direct_record_response'] if prepared else None

    def assert_source_binding(self, question, notes, response):
        self.assertEqual(response['trace']['generation_calls'], 0)
        self.assertEqual(response['trace']['messages'], [])
        self.assertTrue(response['citation_validation']['passed'])
        plan = response['trace']['section_projection']['plan']
        self.assertEqual(validate_sections(question, plan, notes, response=response), [])
        for source in response['sources']:
            self.assertEqual(len(source['raw_evidence']), 1)
            self.assertEqual(source['display_spans'], source['raw_evidence'])
            raw = source['raw_evidence'][0]
            original = notes[source['source_id']]
            self.assertEqual(raw['quote'], original['content'][raw['char_start']:raw['char_end_exclusive']])
            self.assertEqual(source['display_evidence'], raw['quote'])
            self.assertIn(raw['quote'] + f' [{source["index"]}]', response['answer'])

    def test_single_fields_and_normal_lookup_forms(self):
        for q, field in [
            ('《AI 赋能制造业项目》的背景是什么？', '背景'),
            ('《AI 赋能制造业项目》有哪些场景？', '场景'),
            ('请列出《AI 赋能制造业项目》的落地条件。', '落地条件'),
            ('AI 赋能制造业项目里背景是什么？', '背景'),
        ]:
            with self.subTest(question=q):
                r = self.ask(q)
                self.assertIsNotNone(r)
                self.assertEqual(r['trace']['section_projection']['plan']['labels'], [field])
                self.assertIn('不能把模型输出当成正式审批', r['answer'])
                self.assertIn('未满足条件不得上线', r['answer'])
                self.assert_source_binding(q, self.notes, r)

    def test_multiple_fields_all_consumed_and_question_order_preserved(self):
        for join in ('和', '与', '及', '以及', '、'):
            q = f'《AI 赋能制造业项目》的场景{join}背景分别是什么？'
            with self.subTest(join=join):
                r = self.ask(q)
                self.assertIsNotNone(r)
                self.assertEqual(r['trace']['section_projection']['plan']['labels'], ['场景', '背景'])
                self.assertTrue(r['answer'].startswith('场景：'))
                self.assert_source_binding(q, self.notes, r)

    def test_labels_come_from_data_not_a_business_dictionary(self):
        text = '交付窗口：\n周三10:00至12:00，节假日不开放。\n\n封装方式：\n使用防潮袋，不得重复使用破损包装。'
        ns = {61: note(61, '样品接收规则', text)}
        q = '《样品接收规则》的交付窗口和封装方式是什么？'
        r = self.ask(q, ns)
        self.assertIsNotNone(r)
        self.assertIn('10:00至12:00，节假日不开放', r['answer'])
        self.assertIn('不得重复使用破损包装', r['answer'])
        self.assert_source_binding(q, ns, r)

    def test_same_level_markdown_sections(self):
        text = '## 收件窗口\n09:30–11:00，仅工作日。\n\n## 签收人员\n值班员；本人不在时不允许代签。'
        ns = {62: note(62, '材料收件流程', text)}
        q = '《材料收件流程》的签收人员是谁？'
        r = self.ask(q, ns)
        self.assertIsNotNone(r)
        self.assertIn('本人不在时不允许代签', r['answer'])
        self.assertIn('09:30–11:00，仅工作日', r['answer'])
        self.assert_source_binding(q, ns, r)

    def test_front_tail_and_other_tab_context_stay_visible_with_own_references(self):
        text = '临时协议尚未签字，以下仅供核对。\n\n窗口：\n工作日9点开始。\n\n地点：\n楼下一层。\n\n协议失效时，全部停止执行。'
        ns = {70: note(70, '接收安排说明', text, tab='安排'),
              71: note(71, '协议补充', '预约取消后，不收取任何金额。', parent=70, tab='协议补充')}
        q = '《接收安排说明》的窗口是什么？'
        r = self.ask(q, ns)
        self.assertIsNotNone(r)
        for quote in ('临时协议尚未签字', '协议失效时，全部停止执行', '预约取消后，不收取任何金额'):
            self.assertIn(quote, r['answer'])
        self.assert_source_binding(q, ns, r)
        for source in r['sources'][1:]:
            self.assertNotEqual(source['index'], 1)
            self.assertNotEqual(source['display_evidence'], r['sources'][0]['display_evidence'])

    def test_unknown_compound_comparison_filter_and_explanation_fall_back(self):
        for q in ['《AI 赋能制造业项目》的背景和预算是什么？',
                  '《AI 赋能制造业项目》的背景和场景有什么区别？',
                  '《AI 赋能制造业项目》哪些场景不需要人工？',
                  '《AI 赋能制造业项目》的场景为什么适合制造业？',
                  '《AI 赋能制造业项目》的场景与其他项目相比怎样？',
                  '《AI 赋能制造业项目》没有哪些场景？',
                  '《AI 赋能制造业项目》的场景是什么，并说明收益。',
                  '不看《AI 赋能制造业项目》，背景是什么？',
                  '不要根据《AI 赋能制造业项目》回答，背景是什么？',
                  '排除《AI 赋能制造业项目》，有哪些场景？',
                  '项目的背景是什么？', 'AQS是什么？']:
            with self.subTest(question=q):
                self.assertIsNone(self.ask(q))

    def test_ambiguous_field_partition_duplicate_titles_and_objects_fall_back(self):
        body = '输入：\n原始资料。\n\n输出：\n结果清单。\n\n输入和输出：\n两个接口彼此隔离。'
        ns = {80: note(80, '接口交接说明', body)}
        self.assertIsNone(self.ask('《接口交接说明》的输入和输出是什么？', ns))
        ns = {81: note(81, '样品接收规则', '地点：\n甲楼。'),
              82: note(82, '样品接收规则', '地点：\n乙楼。')}
        self.assertIsNone(self.ask('《样品接收规则》的地点是什么？', ns))
        ns[82]['title'] = '第二批接收规则'
        self.assertIsNone(self.ask('《样品接收规则》和《第二批接收规则》的地点是什么？', ns))
        ns[82].update(title='第二页', parent_note_id=81)
        self.assertIsNone(self.ask('《样品接收规则》的地点是什么？', ns))

    def test_unsupported_format_and_empty_section_fall_back(self):
        bad_bodies = [
            '背景：资料分散。\n场景：搜索资料。',
            '背景：\n\n场景：\n搜索资料。',
            '背景：\n资料分散。\n\n场景：',
            '# 背景\n资料分散。\n\n## 场景\n搜索资料。',
            '# 背景\n资料分散。\n\n场景：\n搜索资料。',
            '背景：\n资料分散。\n\n 背景补充：\n需核对权限。',
            '##背景\n资料分散。\n\n## 场景\n搜索资料。',
            '背景：\n<p>资料分散。</p>\n\n场景：\n搜索资料。',
            '背景：\n| 字段 | 值 |\n| --- | --- |\n\n场景：\n搜索资料。',
            '```\n背景：\n示例文本。\n```\n\n场景：\n搜索资料。',
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                self.assertIsNone(self.ask('《结构记录》的背景是什么？', {90: note(90, '结构记录', body)}))
        self.assertIsNone(self.ask('《AI 赋能制造业项目》的背景是什么？', ocr={31: '另有识别结果。'}))

    def test_character_budgets_return_none_without_truncating(self):
        for body in ['背景：\n' + '甲' * 2501,
                     '附加记录。' * 250 + '\n\n背景：\n必要文字。']:
            self.assertIsNone(self.ask('《结构记录》的背景是什么？', {91: note(91, '结构记录', body)}))

    def test_old_numbered_list_and_free_prose_do_not_get_claimed(self):
        cases = [('风险一：资料过杂。\n\n风险二：冷启动慢。', '有哪些风险'),
                 ('先准备资料，再确认日期，最后演示。', '步骤是什么'),
                 ('有三个目标：第一，可读取；第二，可核对；第三，可编辑。', '目标有哪些')]
        for body, suffix in cases:
            self.assertIsNone(self.ask('《演示记录》' + suffix, {92: note(92, '演示记录', body)}))

    def test_guard_rejects_missing_context_boundaries_identity_and_render_tampering(self):
        q = '《AI 赋能制造业项目》的背景是什么？'
        response = self.ask(q)
        self.assertIsNotNone(response)
        plan = response['trace']['section_projection']['plan']
        mutations = [lambda p: p['context'].pop(),
                     lambda p: p['sections'][0].update(end=p['sections'][0]['end']-1),
                     lambda p: p['sections'][0].update(parent_note_id=999),
                     lambda p: p['sections'][0].update(source_revision='stale'),
                     lambda p: p.update(labels=['场景']),
                     lambda p: p.update(scope_note_ids=[])]
        for mutate in mutations:
            changed = copy.deepcopy(plan); mutate(changed)
            self.assertTrue(validate_sections(q, changed, self.notes))
        for mutate in [lambda r: r.update(answer=r['answer'].replace('不能', '可以')),
                       lambda r: r['sources'][0].update(display_evidence='另一段'),
                       lambda r: r['sources'][0]['raw_evidence'][0].update(quote='其他内容'),
                       lambda r: r['provided_sources'][0].update(source_id=999)]:
            changed = copy.deepcopy(response); mutate(changed)
            self.assertTrue(validate_sections(q, plan, self.notes, response=changed))


if __name__ == '__main__':
    unittest.main()
