# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Independent source-label scenarios, frozen before running the candidate.

These tests exercise the generic admission boundary, not benchmark scores.
No model, external library, or user database is read or modified.
"""
from pathlib import Path
import copy
import hashlib
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory.rag.retained.enumerated_list import EnumeratedList, validate_list


def note(note_id, title, content, parent=None, tab=''):
    return dict(id=note_id, title=title, content=content,
                parent_note_id=parent, tab_name=tab)


class EnumeratedListGeneralization(unittest.TestCase):
    def response(self, question, notes, ocr=None):
        prepared = EnumeratedList(notes, ocr).prepare(question)
        return prepared['direct_record_response'] if prepared else None

    def assert_original_identity(self, question, notes, response):
        self.assertEqual(response['trace']['generation_calls'], 0)
        self.assertEqual(response['trace']['messages'], [])
        self.assertTrue(response['citation_validation']['passed'])
        plan = response['trace']['enumerated_list']['plan']
        self.assertEqual(validate_list(question, plan, notes), [])
        record_count = len(plan['records'])
        self.assertEqual(len(response['sources']), record_count + len(plan['dependencies']))
        for dependency, source in zip(plan['dependencies'], response['sources'][record_count:]):
            # A context citation must open that context, even when its note ID
            # matches a list member. Sharing a note is not sharing a span.
            self.assertEqual(source['raw_evidence'], [dependency])
            self.assertEqual(source['display_spans'], [dependency])
            self.assertEqual(source['display_evidence'], dependency['quote'])
            self.assertIn(dependency['quote'] + f' [{source["index"]}]', response['answer'])
        for source in response['sources']:
            original = notes[source['source_id']]
            self.assertEqual(source['source_key'], 'note:' + str(original['id']))
            self.assertEqual(source['source_field'], 'content')
            self.assertEqual(source['parent_note_id'], original['parent_note_id'])
            self.assertEqual(source['tab_name'], original['tab_name'])
            self.assertEqual(len(source['raw_evidence']), 1)
            self.assertTrue(source['display_spans'])
            for span in source['raw_evidence'] + source['display_spans']:
                self.assertEqual(span['source_id'], original['id'])
                self.assertEqual(span['field'], 'content')
                self.assertEqual(span['quote'], original['content'][
                    span['char_start']:span['char_end_exclusive']])
                self.assertEqual(span['source_revision'], hashlib.sha256(
                    original['content'].encode('utf-8')).hexdigest())

    def test_labels_are_read_from_independent_documents(self):
        scenarios = [
            ('交接办理记录', '步骤', [
                '步骤一：核对清单与封签。前提：封签完整才可继续。',
                '步骤二：双方逐件点数。限制：不得用总重量替代点数。',
                '步骤三：双方签字后移交钥匙。异常：数量不符则停止移交。']),
            ('试样领取说明', '要求', [
                '要求1：申请人出示有效凭证。例外：过期凭证不得领取。',
                '要求2：每人领取一份。条件：代领须同时出示书面授权。']),
            ('活动收费说明', '费用', [
                '费用一：报名费为80元。减免：学生凭有效证明支付40元。',
                '费用二：材料费为20元。限制：仅在领取材料时收取。']),
            ('设备搬运说明', '注意事项', [
                '注意事项一：断开电源后方可搬运。条件：备用电源也须断开。',
                '注意事项二：搬运时保持竖直。例外：包装损坏时停止操作。']),
        ]
        for index, (title, label, records) in enumerate(scenarios, 101):
            notes = {index: note(index, title, '\n\n'.join(records))}
            questions = [f'《{title}》有哪些{label}？',
                         f'《{title}》的{label}有哪几项？',
                         f'请列出《{title}》的全部{label}']
            for question in questions:
                with self.subTest(title=title, question=question):
                    response = self.response(question, notes)
                    self.assertIsNotNone(response)
                    self.assertEqual(len(response['sources']), len(records))
                    for record in records:
                        # Ancillary fields contain restrictions and exceptions:
                        # preserving only their presence in context is not enough.
                        self.assertIn(record, response['answer'])
                        self.assertTrue(any(record in source['evidence']
                                            for source in response['sources']))
                    self.assert_original_identity(question, notes, response)

    def test_continuous_list_across_tabs_keeps_each_source_identity(self):
        records = ['检查项一：确认包装未破损。', '检查项二：核对标识与清单。',
                   '检查项三：确认封签编号一致。限制：不一致时禁止接收。',
                   '检查项四：确认交接双方签字。']
        notes = {
            71: note(71, '来料验看记录', '\n\n'.join(records[:2]), tab='到货验看'),
            72: note(72, '封签与签收', '\n\n'.join(records[2:]), parent=71, tab='封签与签收'),
            93: note(93, '另一批来料', '检查项一：标签不同。\n\n检查项二：批次不同。'),
        }
        question = '《来料验看记录》有哪些检查项？'
        response = self.response(question, notes)
        self.assertIsNotNone(response)
        self.assertEqual([s['source_id'] for s in response['sources']], [71, 71, 72, 72])
        self.assertEqual(response['trace']['enumerated_list']['plan']['scope_note_ids'], [71, 72])
        for record in records:
            self.assertIn(record, response['answer'])
        self.assertNotIn('标签不同', response['answer'])
        self.assert_original_identity(question, notes, response)

    def test_recognized_source_conditions_remain_visible_and_cited(self):
        condition = '仅在室内环境下适用，室外作业须另行评估。'
        notes = {30: note(30, '安装交接要求', condition + '\n\n'
                          '要求一：先检查端口。条件：检查前不得上电。\n\n'
                          '要求二：完成绝缘检查后签字。例外：潮湿时暂停检查。')}
        question = '《安装交接要求》的要求有哪些？'
        response = self.response(question, notes)
        self.assertIsNotNone(response)
        for required in (condition, '检查前不得上电', '潮湿时暂停检查'):
            self.assertIn(required, response['answer'])
            self.assertTrue(any(required in s['evidence'] for s in response['sources']))
        self.assert_original_identity(question, notes, response)

    def test_same_topic_without_unique_scope_does_not_choose_a_document(self):
        notes = {
            41: note(41, '甲活动收费说明', '费用一：报名费80元。\n\n费用二：材料费20元。'),
            42: note(42, '乙活动收费说明', '费用一：报名费60元。\n\n费用二：材料费免费。'),
        }
        for question in ['活动的费用有哪些？', '收费说明有哪些费用？',
                         '《甲活动收费说明》和《乙活动收费说明》的费用有哪些？']:
            with self.subTest(question=question):
                self.assertIsNone(self.response(question, notes))
        ambiguous = copy.deepcopy(notes)
        ambiguous[41]['title'] = ambiguous[42]['title'] = '活动收费说明'
        self.assertIsNone(self.response('《活动收费说明》有哪些费用？', ambiguous))

    def test_filter_negation_explanation_and_compound_requests_fall_back_whole(self):
        notes = {51: note(51, '活动收费说明',
                          '费用一：报名费80元。减免：学生支付40元。\n\n'
                          '费用二：材料费20元。条件：领取材料才收取。')}
        questions = [
            '《活动收费说明》中超过50元的费用有哪些？',
            '《活动收费说明》中不收取的费用有哪些？',
            '《活动收费说明》有哪些费用，不要列报名费。',
            '《活动收费说明》不包含哪些费用？',
            '《活动收费说明》为什么收取这些费用？',
            '《活动收费说明》的费用是什么意思？',
            '《活动收费说明》有哪些费用和负责人？',
            '《活动收费说明》有哪些费用，怎样申请减免？',
            '《活动收费说明》共有几项费用？',
            '《活动收费说明》中AQS是什么？',
        ]
        for question in questions:
            with self.subTest(question=question):
                self.assertIsNone(self.response(question, notes))

    def test_gaps_restarts_and_partial_numbering_do_not_claim_a_complete_list(self):
        bodies = [
            '步骤一：准备材料。\n\n步骤三：归还材料。',
            '步骤二：准备材料。\n\n步骤三：归还材料。',
            '步骤一：准备材料。\n\n步骤一：归还材料。',
            '步骤一：准备材料。\n\n步骤二 归还材料。',
            '步骤一：准备材料。\n\n步骤二：',
        ]
        for body in bodies:
            with self.subTest(body=body):
                notes = {61: note(61, '借还办理记录', body)}
                self.assertIsNone(self.response('《借还办理记录》有哪些步骤？', notes))

    def test_other_tab_conditions_and_ocr_cannot_be_ignored(self):
        notes = {
            81: note(81, '发放办理说明', '要求一：核对证明。\n\n要求二：领取后签字。'),
            82: note(82, '附加说明', '要求：尚须核对新修订事项。', parent=81),
        }
        question = '《发放办理说明》有哪些要求？'
        self.assertIsNone(self.response(question, notes))
        notes[82]['content'] = ''
        self.assertIsNone(self.response(question, notes, {82: '要求三：领取当日须再次核验。'}))

    def test_external_condition_must_be_visible_or_the_whole_request_falls_back(self):
        # Mechanism-level safety probe. This qualifier does not start with the
        # legacy dependency vocabulary and does not repeat the dynamic label.
        # A matching label sequence alone must not erase a binding exception.
        condition = '退货时，本清单所列金额全部免除。'
        notes = {91: note(91, '材料收费说明',
                          '费用一：装配费80元。\n\n费用二：包装费20元。\n\n' + condition)}
        response = self.response('《材料收费说明》有哪些费用？', notes)
        if response is not None:
            self.assertIn(condition, response['answer'])
            self.assertTrue(any(condition in s['evidence'] for s in response['sources']))

    def test_unknown_front_and_tail_context_preserved_without_a_condition_dictionary(self):
        before = '临时借用协议尚未批准，本清单仅供核对，不代表允许执行。'
        after = '协议失效时，已领取物品须原样退还。'
        notes = {110: note(110, '物品借还记录', before + '\n\n'
                           '步骤一：核对物品。\n\n步骤二：双方签字。\n\n' + after)}
        question = '《物品借还记录》有哪些步骤？'
        response = self.response(question, notes)
        self.assertIsNotNone(response)
        self.assertIn(before, response['answer'])
        self.assertIn(after, response['answer'])
        self.assertLess(response['answer'].index('步骤二：'), response['answer'].index('原文说明'))
        self.assert_original_identity(question, notes, response)
        plan = response['trace']['enumerated_list']['plan']
        for modify in [lambda p: p['dependencies'].pop(),
                       lambda p: p['dependencies'][0].update(source_revision='not-current'),
                       lambda p: p['dependencies'][0].update(tab_name='another-tab')]:
            changed = copy.deepcopy(plan)
            modify(changed)
            self.assertTrue(validate_list(question, changed, notes))

    def test_context_only_tab_gets_its_own_citation(self):
        context = '外借协议失效时，本清单全部暂停执行。'
        notes = {
            121: note(121, '附件交接说明', '要求一：核对封签。\n\n要求二：领取时签字。', tab='交接'),
            122: note(122, '协议补充', context, parent=121, tab='协议补充'),
        }
        question = '《附件交接说明》有哪些要求？'
        response = self.response(question, notes)
        self.assertIsNotNone(response)
        self.assertIn(context, response['answer'])
        self.assertEqual([s['source_id'] for s in response['sources']], [121, 121, 122])
        extra_source = response['sources'][-1]
        self.assertEqual(extra_source['raw_evidence'][0]['quote'], context)
        self.assertIn(context + ' [3]', response['answer'])
        self.assert_original_identity(question, notes, response)

    def test_other_label_between_members_is_not_lost_by_paragraph_overlap(self):
        context = '上限一：每件最多支付20元，超过部分不予报销。'
        notes = {131: note(131, '寄存收费说明',
                           '费用一：寄存费10元。\n' + context + '\n费用二：包装费15元。')}
        question = '《寄存收费说明》有哪些费用？'
        response = self.response(question, notes)
        self.assertIsNotNone(response)
        self.assertIn(context, response['answer'])
        self.assert_original_identity(question, notes, response)

    def test_large_external_context_falls_back_instead_of_being_truncated(self):
        context = '背景记录与适用环境须完整核对。' * 100
        notes = {141: note(141, '执行办理记录', context + '\n\n'
                           '步骤一：核对单据。\n\n步骤二：确认交接。')}
        self.assertIsNone(self.response('《执行办理记录》有哪些步骤？', notes))


if __name__ == '__main__':
    unittest.main()
