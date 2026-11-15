# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Frozen behavior cases before implementation; exposed developer checks, no LLM."""
from pathlib import Path
import copy
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory.rag.retained.enumerated_list import EnumeratedList, validate_list

R1 = '风险一：导入资料主题过杂，会让检索命中很多相似词，却缺少能够直接回答问题的证据。处理动作：按主题整理资料；负责人：演示人。'
R2 = '风险二：长 Excel 或扫描 PDF 的原文很难阅读，用户即使打开来源也看不出答案在哪里。处理动作：保留行范围和页码；负责人：演示人。'
R3 = '风险三：本地 4B 模型冷启动需要加载，首次问答可能较慢。处理动作：提前加载模型；负责人：演示人。'
BODY = '本周完成了三项可演示工作。\n\n下周将继续验证资料。\n\n' + '\n\n'.join((R1, R2, R3))


def note(i, title, content, parent=None, tab=''):
    return dict(id=i, title=title, content=content, parent_note_id=parent, tab_name=tab)


class EnumeratedListBehavior(unittest.TestCase):
    def setUp(self):
        self.notes = {27: note(27, '项目复盘', BODY, tab='本周完成'),
                      28: note(28, '风险与行动项', '', parent=27, tab='风险与行动项')}

    def ask(self, q, notes=None):
        p = EnumeratedList(notes or self.notes).prepare(q)
        return p['direct_record_response'] if p else None

    def test_original_and_paraphrases_keep_three_risks(self):
        for q in ['项目复盘项目有哪些风险', '项目复盘里提到的风险是什么？',
                  '《项目复盘》中有哪些风险？', '请列出《项目复盘》的全部风险',
                  '《项目复盘》的风险有哪几项？']:
            with self.subTest(q=q):
                r = self.ask(q)
                self.assertIsNotNone(r)
                self.assertEqual(r['trace']['generation_calls'], 0)
                self.assertEqual(len(r['trace']['enumerated_list']['plan']['records']), 3)
                self.assertEqual(len(r['sources']), 4)
                for p in (R1, R2, R3):
                    self.assertIn(p.split('处理动作')[0], r['answer'])
                # Review tightened this contract before HTTP/model testing:
                # arbitrary ancillary fields may carry conditions, so preserve
                # complete records instead of silently hiding their tails.
                self.assertIn('处理动作', r['answer'])
                self.assertTrue(r['citation_validation']['passed'])
                for s in r['sources'][:3]:
                    self.assertIn('处理动作', s['evidence'])
                    self.assertEqual(len(s['raw_evidence']), 1)
                    self.assertNotIn('本周完成了三项', s['evidence'])
                context = r['sources'][3]
                self.assertEqual(context['display_evidence'], context['raw_evidence'][0]['quote'])
                self.assertEqual(context['display_spans'], context['raw_evidence'])
                self.assertIn(context['display_evidence'] + ' [4]', r['answer'])
                for s in r['sources']:
                    self.assertTrue(s['display_spans'])
                    for span in s['raw_evidence'] + s['display_spans']:
                        self.assertEqual(BODY[span['char_start']:span['char_end_exclusive']], span['quote'])

    def test_dynamic_label_not_risk_specific(self):
        ns = {1: note(1, '样机检查记录', '检查项1：电源已断开。\n\n检查项2：防护盖未拆除。')}
        r = self.ask('样机检查记录有哪些检查项？', ns)
        self.assertIsNotNone(r)
        self.assertIn('防护盖未拆除', r['answer'])
        self.assertEqual(len(r['sources']), 2)

    def test_whole_question_falls_back(self):
        for q in ['项目复盘有哪些风险和处理措施？', '项目复盘有哪些高优先级风险？',
                  '项目复盘的风险为什么会发生？', '项目复盘有几个风险？',
                  '《项目复盘》的风险和负责人分别是什么？', '项目复盘中如何避免风险？',
                  'AQS是什么？', '有哪些风险？']:
            with self.subTest(q=q):
                self.assertIsNone(self.ask(q))

    def test_gap_duplicate_and_unterminated_record_fall_back(self):
        for body in [BODY.replace('风险二', '风险四'), BODY.replace('风险二', '风险一'),
                     BODY.replace('风险三：', '风险三 '), BODY.replace('风险三：本地', '风险三：') + '\n\n风险四：']:
            ns = copy.deepcopy(self.notes); ns[27]['content'] = body
            with self.subTest(body=body):
                self.assertIsNone(self.ask('项目复盘项目有哪些风险', ns))

    def test_existing_conditions_stay_verbatim(self):
        body = '仅在演示环境下适用，生产环境须另行评估。\n\n' + '\n\n'.join((R1, R2, R3))
        body = body.replace('风险二：', '风险二：如果没有行范围，则')
        ns = {27: note(27, '项目复盘', body)}
        r = self.ask('项目复盘项目有哪些风险', ns)
        self.assertIsNotNone(r)
        self.assertIn('仅在演示环境下适用，生产环境须另行评估。', r['answer'])
        self.assertIn('如果没有行范围，则', r['answer'])

    def test_multiple_tabs_are_not_silently_chosen(self):
        ns = copy.deepcopy(self.notes)
        ns[28]['content'] = '风险一：版本过期。\n\n风险二：还未签字。'
        self.assertIsNone(self.ask('项目复盘项目有哪些风险', ns))

    def test_subordinate_condition_field_not_hidden(self):
        ns = copy.deepcopy(self.notes)
        ns[27]['content'] = BODY.replace('处理动作：按主题整理资料', '前提：仅限未归档资料')
        r = self.ask('项目复盘项目有哪些风险', ns)
        self.assertIn('前提：仅限未归档资料',r['answer'])

    def test_unparsed_label_in_other_tab_blocks_completeness(self):
        ns = copy.deepcopy(self.notes)
        ns[28]['content'] = '风险：还有未确认事项。'
        self.assertIsNone(self.ask('项目复盘项目有哪些风险', ns))

    def test_strict_ocr_and_unknown_titles_not_substituted(self):
        for q in ['只读《项目复盘》的OCR，风险有哪些？', '《不存在的记录》有哪些风险？']:
            self.assertIsNone(self.ask(q))

    def test_family_ocr_is_not_silently_excluded(self):
        parser = EnumeratedList(self.notes, {28: '风险四：资料版本未核对。'})
        self.assertIsNone(parser.prepare('项目复盘项目有哪些风险'))

    def test_guard_rejects_omission_wrong_value_and_identity(self):
        q = '项目复盘项目有哪些风险'
        r = self.ask(q)
        plan = r['trace']['enumerated_list']['plan']
        self.assertEqual(validate_list(q, plan, self.notes), [])
        for mutate in [lambda p: p['records'].pop(),
                       lambda p: p['records'][0].update(display_quote='不属于原文'),
                       lambda p: p['records'][0].update(source_id=28),
                       lambda p: p['records'][0].update(end=p['records'][0]['display_end'])]:
            p = copy.deepcopy(plan); mutate(p)
            self.assertTrue(validate_list(q, p, self.notes))


if __name__ == '__main__':
    unittest.main()
