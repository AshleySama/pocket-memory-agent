# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import copy
from types import SimpleNamespace
import unittest
from pocket_memory.rag.citation_display import presentation, aligned_focus
from pocket_memory.rag.retained.registry import EvidenceRegistry

class ParaphraseFocusTests(unittest.TestCase):
    def present(self, body, answer, **changes):
        note=SimpleNamespace(id=7,parent_note_id=3,tab_name='办公',content=body)
        raw=dict(index=1,source_type='note',source_id=7,source_key='note:7',kind='content',title='办公',evidence=body,
                 raw_evidence=[dict(field='content',char_start=0,char_end_exclusive=len(body),quote=body)])
        src=EvidenceRegistry({7:vars(note)},{}).decorate(raw); src.update(changes)
        before=copy.deepcopy(src)
        out=presentation(src,answer,store=SimpleNamespace(get_note=lambda _:note))
        self.assertEqual(src,before)
        for s in out.get('display_spans',[]):
            self.assertEqual(body[s['char_start']:s['char_end_exclusive']],s['quote'])
        return out

    def test_distributed_rewording_keeps_original_sentence(self):
        sentence='说明：保存资料内容过杂，会让搜索命中很多近似词，却缺少能够直接回应问题的内容。'
        answer='保存资料内容太杂，会导致搜索命中大量近似词，却缺乏能够直接回应问题的内容[1]。'
        out=self.present('导读。'+sentence+'其他说明。',answer)
        self.assertEqual(out['display_evidence'],sentence)
        self.assertEqual(out['display_focus_methods'],{'aligned':1})
        self.assertFalse(out['display_partial_focus'])

    def test_subject_substitution_cannot_use_shared_tail(self):
        body='办公协调员负责安排会场，会议在材料确认后举行。'
        out=self.present(body,'项目负责人负责安排会场，会议在资料确认后举行[1]。')
        self.assertEqual(out['display_spans'],[])

    def test_changed_numeric_value_or_category_rejected(self):
        pairs=[('本项目预计预算为12万元，付款后开始实施。','本项目预计预算为21万元，付款后开始执行'),
               ('甲工程本次贷款本金为12万元，付款后开始实施。','甲工程本次贷款利息为12万元，付款后开始执行'),
               ('本项目预计预算为十二万元，付款后开始实施。','本项目预计预算为二十一万元，付款后开始执行')]
        for body,answer in pairs:
            with self.subTest(answer=answer):self.assertEqual(self.present(body,answer+'[1]。')['display_spans'],[])

    def test_changed_identifier_rejected(self):
        body='离线检索采用Engine1索引文件，检索结果可以逐项核对。'
        self.assertEqual(self.present(body,'离线检索采用Engine2索引资料，检索结果可以逐项确认[1]。')['display_spans'],[])

    def test_polarity_and_conditions_cannot_be_changed(self):
        pairs=[('材料核验完成之后，项目可以开始执行。','材料核验完成之前，项目可以开始实施'),
               ('办公协调员不负责会场安排，会议在材料确认后举行。','办公协调员负责会场安排，会议在资料确认后举行'),
               ('如果资料已经核验通过，文件才能提交给项目负责人。','只要资料已经核验通过，文件即可提交给项目负责人')]
        for body,answer in pairs:
            with self.subTest(answer=answer):self.assertEqual(self.present(body,answer+'[1]。')['display_spans'],[])

    def test_single_fact_substitution_is_not_paraphrase_alignment(self):
        body='本项目最终负责人是张三，所有文件已经核验通过。'
        self.assertFalse(aligned_focus('本项目最终负责人是李四，所有文件已经核验通过',body))

    def test_review_counterexamples_do_not_hide_behind_distributed_edits(self):
        pairs=[('本项目最终负责人是张三，所有文件已经核验通过。','本项目最终负责人是李四，所有文件已经校验通过'),
               ('甲项目：保存资料内容过杂，会让搜索命中很多近似词，却缺少能够直接回应问题的内容。',
                '乙项目：保存资料内容太杂，会导致搜索命中大量近似词，却缺乏能够直接回应问题的内容'),
               ('本项目技术负责人不支持甲方案但支持乙方案，所有资料已经完成核验。',
                '本项目技术负责人支持甲方案但不支持乙方案，所有资料已经完成校验')]
        for body,answer in pairs:
            with self.subTest(answer=answer):self.assertEqual(self.present(body,answer+'[1]。')['display_spans'],[])

    def test_ambiguity_does_not_choose_one_location(self):
        body='甲项目保存资料主题过杂，会让检索命中很多近似词。乙项目保存资料主题过杂，会让检索命中很多近似词。'
        self.assertEqual(self.present(body,'该项目保存资料主题太杂，会导致检索命中大量近似词[1]。')['display_spans'],[])
        same='保存资料内容过杂，会让搜索命中很多近似词，却缺少能够直接回应问题的内容。'
        answer='保存资料内容太杂，会导致搜索命中大量近似词，却缺乏能够直接回应问题的内容[1]。'
        self.assertEqual(self.present(same+'\n'+same,answer)['display_spans'],[])

    def test_unrelated_clause_does_not_get_partial_long_tail_pass(self):
        body='离线设备需要先安装本地模型，检索结果可以逐条核对。'
        self.assertEqual(self.present(body,'离线设备可以完成所有任务，检索结果可以逐项核对[1]。')['display_spans'],[])

    def test_other_citation_unassigned_or_structured_preserved(self):
        body='保存资料内容过杂，会让搜索命中很多近似词，却缺少能够直接回应问题的内容。'
        answer='保存资料内容太杂，会导致搜索命中大量近似词，却缺乏能够直接回应问题的内容'
        for suffix in ('。','[2]。'):
            self.assertEqual(self.present(body,answer+suffix)['display_spans'],[])
        self.assertEqual(self.present(body,answer+'[1]。',coverage_complete=True),{})

if __name__=='__main__':unittest.main()
