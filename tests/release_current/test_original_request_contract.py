# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Full-query and conservative-context regressions with fictional office notes."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory.rag.retained.enumerated_list import EnumeratedList, validate_list
from pocket_memory.rag.retained.section_projection import SectionProjection, validate_sections


def note(i, title, content, parent=None, tab=''):
    return dict(id=i, title=title, content=content, parent_note_id=parent, tab_name=tab)


INTRO = ('本周完成了会场的三项准备工作。行政组画好了入口示意图，逐层标注了会议室和茶水间的位置。'
         '会务组打印了入场券样张，每一张都留有姓名栏和场次栏。排练组拍摄了空场照片供布置人员比对。\n\n'
         '下周的重点是整理会务手册。前台组将汇总接待用语，场务组将整理座位示意图，两组还会交换版面样张。')
RISKS = [
    '风险一：两个入口的引导牌颜色相同，来宾可能走错会场。处理动作：给各入口标出楼层和厅号；负责人：场务组；完成时间：布展前一天。',
    '风险二：纸质胸牌遇雨可能模糊，接待员无法辨认场次。处理动作：将胸牌放进透明套，另备一份纸质签到表；负责人：前台组；完成时间：开门前。',
    '风险三：两场会议交接时共用一台投影仪，后一场可能延迟开场。处理动作：登记设备交接时间并预留备用机；负责人：设备组；完成时间：彩排当日。',
]


class OriginalRequestContractBehavior(unittest.TestCase):
    def response(self, cls, question, notes):
        prepared = cls(notes).prepare(question)
        return prepared['direct_record_response'] if prepared else None

    def plan(self, cls, response):
        key = 'enumerated_list' if cls is EnumeratedList else 'section_projection'
        return response['trace'][key]['plan']

    def test_original_two_questions_share_plan_and_omit_only_progress_context(self):
        body = INTRO + '\n\n' + '\n\n'.join(RISKS)
        notes = {27: note(27, '项目复盘', body, tab='本周完成'),
                 28: note(28, '风险与行动项', '', parent=27, tab='风险与行动项')}
        answers = []
        for question in ('项目复盘项目有哪些风险', '项目复盘有什么风险',
                         '《项目复盘》有什么风险？', '《项目复盘》的风险有哪几项？',
                         '请问，项目复盘有哪些风险呢？'):
            with self.subTest(question=question):
                response = self.response(EnumeratedList, question, notes)
                self.assertIsNotNone(response)
                answers.append(response['answer'])
                plan = self.plan(EnumeratedList, response)
                self.assertEqual(len(plan['records']), 3)
                self.assertEqual(len(plan['dependencies']), 2)
                self.assertEqual(len(plan['background']), 2)
                self.assertEqual([p['quote'] for p in response['context_passages']],
                                 [p['quote'] for p in plan['dependencies']])
                self.assertNotIn('原文说明', response['answer'])
                self.assertNotIn('本周完成了会场', response['answer'])
                self.assertNotIn('下周的重点', response['answer'])
                for risk in RISKS:
                    self.assertIn(risk, response['answer'])
                self.assertEqual(validate_list(question, plan, notes), [])
                self.assertEqual(response['trace']['generation_calls'], 0)
                for source in response['sources']:
                    self.assertEqual(source['source_id'], 27)
                    self.assertEqual(source['tab_name'], '本周完成')
                for omitted in plan['background']:
                    raw = omitted['span']
                    self.assertEqual(body[raw['char_start']:raw['char_end_exclusive']], raw['quote'])
                    source = next(s for s in response['sources'] if s['raw_evidence'] == [raw])
                    self.assertTrue(any(p['source_index'] == source['index'] and p['quote'] == raw['quote']
                                        for p in response['context_passages']))
                self.assertEqual(notes[27]['content'], body)
        self.assertEqual(len(set(answers)), 1)

    def test_common_synonyms_apply_to_source_labels_in_both_structures(self):
        for label, title in [('费用', '器材租借说明'), ('检查项', '入库验看记录'), ('步骤', '试样交接说明')]:
            variants = [f'有什么{label}', f'有哪些{label}', f'有哪几项{label}',
                        f'{label}有哪几条', f'{label}是什么', f'列出所有{label}',
                        f'的{label}包括哪些', f'里面提到的{label}有什么',
                        f'有什么{label}呢', f'有哪几项{label}呀', f'{label}有哪些啊']
            for cls in (EnumeratedList, SectionProjection):
                body = (f'{label}一：核对记录。\n\n{label}二：确认封签。' if cls is EnumeratedList
                        else f'{label}：\n核对记录；确认封签。')
                notes = {41: note(41, title, body)}
                for suffix in variants:
                    question = f'《{title}》{suffix}？'
                    with self.subTest(label=label, cls=cls.__name__, question=question):
                        response = self.response(cls, question, notes)
                        self.assertIsNotNone(response)
                        contract = self.plan(cls, response)['request_contract']
                        self.assertEqual(contract['labels'], [label])
                        self.assertEqual(contract['filters'], [])
                        self.assertEqual(contract['residual'], '')
                        self.assertEqual(response['trace']['generation_calls'], 0)

    def test_whole_request_rejects_filters_negation_unknown_subjects_and_partial_compounds(self):
        questions = [
            '《器材租借说明》有什么超过50元的费用？',
            '《器材租借说明》有什么费用不需要支付？',
            '《器材租借说明》没有哪些费用？',
            '《器材租借说明》有什么费用，报名费除外？',
            '《器材租借说明》有什么费用，只列一项。',
            '《器材租借说明》的费用有哪几项，需要如何减免？',
            '《器材租借说明》有什么费用和负责人？',
            '《器材租借说明》的乙项目有什么费用？',
            '《器材租借说明》如果逾期，有什么费用？',
            '《器材租借说明》请分析费用有哪些。',
            '《器材租借说明》的费用有几个？',
            '《未保存的说明》有什么费用？',
            '有什么费用？',
        ]
        for cls in (EnumeratedList, SectionProjection):
            body = ('费用一：租金80元。\n\n费用二：押金20元。' if cls is EnumeratedList
                    else '费用：\n租金80元；押金20元。')
            notes = {51: note(51, '器材租借说明', body)}
            for question in questions:
                with self.subTest(cls=cls.__name__, question=question):
                    self.assertIsNone(self.response(cls, question, notes))

    def test_scaffolding_cannot_hide_another_literal_label_partition(self):
        for cls in (EnumeratedList, SectionProjection):
            body = ('输入一：甲。\n\n输入二：乙。\n\n项目输入一：丙。\n\n项目输入二：丁。'
                    if cls is EnumeratedList else '输入：\n甲；乙。\n\n项目输入：\n丙；丁。')
            notes = {52: note(52, '接口移交记录', body)}
            with self.subTest(cls=cls.__name__):
                self.assertIsNone(self.response(cls, '《接口移交记录》项目输入有哪些？', notes))

    def test_unrelated_progress_in_another_domain_stays_in_source(self):
        intro = '阶段进展：清点了仓库旧包装，并拍摄货架照片。\n\n后续计划：整理影像档案。'
        for cls in (EnumeratedList, SectionProjection):
            body = (intro + '\n\n要求一：核对封签。\n\n要求二：逐件点数。' if cls is EnumeratedList
                    else intro + '\n\n要求：\n核对封签；逐件点数。')
            notes = {61: note(61, '箱件移交说明', body)}
            response = self.response(cls, '《箱件移交说明》有什么要求？', notes)
            self.assertIsNotNone(response)
            self.assertNotIn('清点了仓库旧包装', response['answer'])
            self.assertNotIn('整理影像档案', response['answer'])
            self.assertEqual(notes[61]['content'], body)
            self.assertEqual(len(self.plan(cls, response)['background']), 2)
            self.assertEqual(len(response['context_passages']), 2)
            for passage in response['context_passages']:
                source = next(s for s in response['sources'] if s['index'] == passage['source_index'])
                self.assertEqual(source['display_evidence'], passage['quote'])

    def test_qualifications_in_progress_intro_and_opaque_paragraphs_remain_cited(self):
        contexts = [
            '阶段进展：包装已完成。仅在室内办理交接。',
            '后续计划：复核签字。周五前提交原件。',
            '近期进展：已核对材料。退货时，本清单所列金额全部免除。',
            '阶段总结：核对记录。未获批准不得执行，例外情况须另行评估。',
            '本阶段已完成审批。只在试点期间执行。',
            '后续计划：完成清点。只对授权人员开放。',
            '阶段进展：已完成核对。只供室内使用。',
            '阶段进展：手续已办好，限于试点班组使用。',
            '阶段进展：手续已办好。这是接收专用通道。',
            '阶段进展：手续已办好。从周一起执行新版规则。',
            '协议失效时，已领取物品须原样退还。',
            '所有数值以最终通知为准。',
        ]
        for cls in (EnumeratedList, SectionProjection):
            records = ('要求一：核对封签。\n\n要求二：逐件点数。' if cls is EnumeratedList
                       else '要求：\n核对封签；逐件点数。')
            for context in contexts:
                notes = {71: note(71, '箱件移交说明', context + '\n\n' + records)}
                with self.subTest(cls=cls.__name__, context=context):
                    response = self.response(cls, '《箱件移交说明》有什么要求？', notes)
                    self.assertIsNotNone(response)
                    self.assertIn(context, response['answer'])
                    self.assertTrue(any(context == raw['quote'] for source in response['sources']
                                        for raw in source['raw_evidence']))
                    self.assertEqual(response['context_passages'], [])

    def test_backward_dependencies_across_records_and_tabs_preserve_intro(self):
        intro = '阶段进展：核对流程已改为双人签字。'
        for cls in (EnumeratedList, SectionProjection):
            for in_tab in (False, True):
                tail = '执行时须依据上述流程。'
                records = ('要求一：核对封签。\n\n要求二：逐件点数。' if cls is EnumeratedList
                           else '要求：\n核对封签；逐件点数。')
                body = intro + '\n\n' + records + ('' if in_tab else tail)
                notes = {81: note(81, '箱件移交说明', body)}
                if in_tab:
                    notes[82] = note(82, '流程补充', tail, parent=81, tab='流程补充')
                with self.subTest(cls=cls.__name__, in_tab=in_tab):
                    response = self.response(cls, '《箱件移交说明》有什么要求？', notes)
                    self.assertIsNotNone(response)
                    self.assertIn(intro, response['answer'])
                    self.assertIn(tail, response['answer'])
                    self.assertEqual(self.plan(cls, response)['background'], [])

    def test_plan_guard_rejects_changed_context_decision_or_request(self):
        for cls, guard in ((EnumeratedList, validate_list), (SectionProjection, validate_sections)):
            intro = '阶段进展：拍摄了货架照片。'
            body = intro + ('\n\n费用一：租金80元。\n\n费用二：押金20元。' if cls is EnumeratedList
                            else '\n\n费用：\n租金80元；押金20元。')
            notes = {91: note(91, '器材租借说明', body)}
            question = '《器材租借说明》有什么费用？'
            response = self.response(cls, question, notes)
            self.assertIsNotNone(response)
            plan = self.plan(cls, response)
            for mutate in [lambda p: p['background'].clear(),
                           lambda p: p['background'][0].update(relation='guess'),
                           lambda p: p['request_contract'].update(residual='只列一项'),
                           lambda p: p['request_contract'].update(labels=['要求'])]:
                changed = copy.deepcopy(plan)
                mutate(changed)
                self.assertTrue(guard(question, changed, notes))


if __name__ == '__main__':
    unittest.main()
