# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Enumerated-list regression cases with fictional inventory notes."""
from pathlib import Path
import copy
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory.rag.retained.enumerated_list import EnumeratedList, validate_list

R1 = '风险一：相邻展柜里的样品外观接近，盘点时可能记错柜号。处理动作：逐柜登记样品；负责人：清点员。'
R2 = '风险二：两批样品使用相同型号，只看外观无法区分批次。处理动作：核对每件样品的批次标签；负责人：登记员。'
R3 = '风险三：备用货架空间有限，返库时可能没有空位。处理动作：预留临时存放区；负责人：库管员。'
BODY = '本周完成了展柜的首次登记。\n\n下周将继续整理样品图册。\n\n' + '\n\n'.join((R1, R2, R3))


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
        for q in ['项目复盘项目有哪些风险', '项目复盘有什么风险', '项目复盘里提到的风险是什么？',
                  '《项目复盘》中有哪些风险？', '请列出《项目复盘》的全部风险',
                  '《项目复盘》的风险有哪几项？']:
            with self.subTest(q=q):
                r = self.ask(q)
                self.assertIsNotNone(r)
                self.assertEqual(r['trace']['generation_calls'], 0)
                self.assertEqual(len(r['trace']['enumerated_list']['plan']['records']), 3)
                self.assertEqual(len(r['sources']), 5)
                for p in (R1, R2, R3):
                    self.assertIn(p.split('处理动作')[0], r['answer'])
                # Ancillary fields can carry conditions; keep whole records.
                self.assertIn('处理动作', r['answer'])
                self.assertTrue(r['citation_validation']['passed'])
                for s in r['sources'][:3]:
                    self.assertIn('处理动作', s['evidence'])
                    self.assertEqual(len(s['raw_evidence']), 1)
                    self.assertNotIn('本周完成了展柜', s['evidence'])
                # Progress narrative retains exact citations in expandable
                # context instead of becoming the main answer. Unknown or
                # qualifying prose has separate preservation tests below.
                self.assertNotIn('原文说明', r['answer'])
                self.assertNotIn('本周完成了展柜', r['answer'])
                self.assertEqual(len(r['trace']['enumerated_list']['plan']['background']), 2)
                self.assertEqual(len(r['context_passages']), 2)
                self.assertEqual([p['source_index'] for p in r['context_passages']], [4, 5])
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
                     BODY.replace('风险三：', '风险三 '), BODY + '\n\n风险四：']:
            ns = copy.deepcopy(self.notes); ns[27]['content'] = body
            with self.subTest(body=body):
                self.assertIsNone(self.ask('项目复盘项目有哪些风险', ns))

    def test_existing_conditions_stay_verbatim(self):
        body = '仅在展期盘点时适用，日常收货须另行登记。\n\n' + '\n\n'.join((R1, R2, R3))
        body = body.replace('风险二：', '风险二：如果没有批次标签，则')
        ns = {27: note(27, '项目复盘', body)}
        r = self.ask('项目复盘项目有哪些风险', ns)
        self.assertIsNotNone(r)
        self.assertIn('仅在展期盘点时适用，日常收货须另行登记。', r['answer'])
        self.assertIn('如果没有批次标签，则', r['answer'])

    def test_multiple_tabs_are_not_silently_chosen(self):
        ns = copy.deepcopy(self.notes)
        ns[28]['content'] = '风险一：版本过期。\n\n风险二：还未签字。'
        self.assertIsNone(self.ask('项目复盘项目有哪些风险', ns))

    def test_subordinate_condition_field_not_hidden(self):
        ns = copy.deepcopy(self.notes)
        ns[27]['content'] = BODY.replace('处理动作：逐柜登记样品', '前提：仅限借展样品')
        r = self.ask('项目复盘项目有哪些风险', ns)
        self.assertIn('前提：仅限借展样品',r['answer'])

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
