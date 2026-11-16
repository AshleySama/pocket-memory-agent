# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import copy
import unittest
from pocket_memory.rag.retained.citation_guard import apply_guard
from pocket_memory.rag.retained.literal_list_binding import reconcile


class LiteralListBindingTests(unittest.TestCase):
    question = '资料归档有哪些步骤？'
    body = ('## 3. 资料归档（演示流程）\n'
            '必须经过批准后才能执行；以下流程仅适用于演示副本。\n'
            '### 步骤\n'
            '1. 核对<strong>资料版本</strong>，未经确认不得发布。\n'
            '2. 将副本保存至本机，保留原目录。\n'
            '3. 通知负责人验收，不得代为签字。\n\n'
            '> 注意：未通过验收前，原目录不能删除。\n')
    raw = ('资料归档的步骤如下：\n\n'
           '1. 核对资料版本，未经确认不得发布。\n'
           '2. 将副本保存至本机，保留原目录。\n'
           '3. 通知负责人验收，不得代为签字。\n\n'
           '注意：未通过验收前，原目录不能删除。[3]')

    def fixture(self, valid_wrong=True):
        source = dict(index=1, source_type='note', source_id=7, node_id='note:2',
                      title='操作手册', kind='content', evidence=self.body,
                      raw_evidence=[dict(field='content', char_start=0,
                                         char_end_exclusive=len(self.body), quote=self.body)])
        sources = [source]
        if valid_wrong:
            sources.append(dict(index=3, source_type='document', source_id=7,
                                title='无关报告', evidence='电影票房与观众口碑。', kind='document'))
        response = apply_guard(dict(answer=self.raw, sources=[], provided_sources=sources,
                                    trace=dict(finish_reason='stop')))
        notes = {7:dict(id=7, content=self.body)}
        return response, notes

    def test_valid_wrong_label_rebound_to_complete_original_section(self):
        response, notes = self.fixture()
        result = reconcile(self.question, response, notes)
        self.assertEqual(result['sources'][0]['title'], '操作手册')
        self.assertIn('必须经过批准', result['answer'])
        self.assertIn('原目录不能删除', result['answer'])
        self.assertEqual(result['trace']['literal_list_binding']['old_indices'], [3])
        self.assertEqual(result['provided_sources'], response['provided_sources'])
        self.assertEqual(result['trace']['literal_list_binding']['new_generations'], 0)

    def test_invalid_section_number_recovers_only_as_full_original(self):
        response, notes = self.fixture(False)
        self.assertEqual(response['answer_status'], 'citation_check_failed')
        result = reconcile(self.question, response, notes)
        self.assertEqual(result['answer_status'], 'answer')
        self.assertIn('必须经过批准', result['answer'])

    def test_recovered_list_locates_every_step_and_its_conditions(self):
        from pocket_memory.rag.retained.registry import EvidenceRegistry
        from pocket_memory.rag.citation_display import presentation
        response, notes = self.fixture()
        result = EvidenceRegistry(notes, {}).finish(reconcile(self.question, response, notes))
        shown = presentation(result['sources'][0], result['answer'])
        for clause in ['必须经过批准', '未经确认不得发布', '保留原目录',
                       '不得代为签字', '原目录不能删除']:
            self.assertIn(clause, shown['display_evidence'])
        for span in shown['display_spans']:
            self.assertEqual(self.body[span['char_start']:span['char_end_exclusive']], span['quote'])

    def test_correct_citation_remains_exactly_unchanged(self):
        response, notes = self.fixture()
        response = apply_guard(dict(answer=self.raw.replace('[3]', '[1]'),
            provided_sources=response['provided_sources'], sources=[], trace=dict(finish_reason='stop')))
        self.assertEqual(reconcile(self.question, response, notes), response)

    def test_presentation_lead_optional_particle_does_not_change_subject(self):
        for lead in ['资料归档步骤如下：', '资料归档的步骤如下：', '资料归档操作流程：']:
            with self.subTest(lead=lead):
                response, notes = self.fixture()
                raw = lead + '\n' + self.raw.split('\n', 1)[1]
                response = apply_guard(dict(answer=raw, sources=[],
                    provided_sources=response['provided_sources'], trace=dict(finish_reason='stop')))
                result = reconcile(self.question, response, notes)
                self.assertIn('literal_list_binding', result['trace'])
                self.assertIn('必须经过批准', result['answer'])

    def test_changed_condition_number_missing_reordered_and_extra_claim_are_not_recovered(self):
        for changed in [
            self.raw.replace('未经确认不得发布', '确认前可以发布'),
            self.raw.replace('保留原目录', '保留2个目录'),
            self.raw.replace('3. 通知负责人验收，不得代为签字。\n', ''),
            self.raw.replace('1. 核对资料版本，未经确认不得发布。\n2. 将副本保存至本机，保留原目录。',
                             '1. 将副本保存至本机，保留原目录。\n2. 核对资料版本，未经确认不得发布。'),
            self.raw.replace('[3]', '\n这能确保完全没有风险。[3]'),
        ]:
            with self.subTest(changed=changed):
                response, notes = self.fixture()
                response = apply_guard(dict(answer=changed, sources=[],
                    provided_sources=response['provided_sources'], trace=dict(finish_reason='stop')))
                self.assertEqual(reconcile(self.question, response, notes), response)

    def test_unknown_scope_conditions_and_wrong_subject_are_not_dropped(self):
        response, notes = self.fixture()
        self.assertEqual(reconcile('合同审批有哪些步骤？', response, notes), response)
        self.assertEqual(reconcile('资料归档为什么这样操作？', response, notes), response)
        body = '本页仅适用于测试账号。\n' + self.body
        notes[7]['content'] = body
        source=response['provided_sources'][0]
        source.update(evidence=body, raw_evidence=[dict(field='content',char_start=0,
                      char_end_exclusive=len(body),quote=body)])
        self.assertEqual(reconcile(self.question, response, notes), response)

    def test_ambiguous_source_stale_field_and_missing_original_are_not_rebound(self):
        for variant in ['duplicate', 'stale', 'ocr', 'missing', 'length', 'not_supplied', 'partial_span']:
            with self.subTest(variant=variant):
                response, notes = self.fixture()
                source = response['provided_sources'][0]
                if variant == 'duplicate':
                    duplicate = copy.deepcopy(source); duplicate['index']=2
                    response['provided_sources'].append(duplicate)
                elif variant == 'stale': notes[7]['content'] = notes[7]['content'].replace('保留原目录', '删除原目录')
                elif variant == 'ocr': source['raw_evidence'][0]['field']='ocr_text'
                elif variant == 'missing': notes.clear()
                elif variant == 'length': response['trace']['finish_reason']='length'
                elif variant == 'not_supplied': response['provided_sources']=response['provided_sources'][1:]
                else:
                    span=source['raw_evidence'][0]; span['char_start']=self.body.index('1.')
                    span['quote']=self.body[span['char_start']:]
                self.assertEqual(reconcile(self.question,response,notes),response)

    def test_ancestor_title_conditions_and_original_footnotes_do_not_escape_guard(self):
        for body in ['# 仅限测试环境，禁止生产使用\n'+self.body,
                     self.body+'另见原资料说明[3]。\n']:
            with self.subTest(body=body):
                response, notes=self.fixture()
                notes[7]['content']=body
                response['provided_sources'][0].update(evidence=body,
                    raw_evidence=[dict(field='content',char_start=0,char_end_exclusive=len(body),quote=body)])
                self.assertEqual(reconcile(self.question,response,notes),response)

    def test_multi_reference_and_comparison_operators_are_not_rewritten(self):
        response, notes=self.fixture()
        response=apply_guard(dict(answer=self.raw.replace('[3]','[1,3]'),sources=[],
            provided_sources=response['provided_sources'],trace=dict(finish_reason='stop')))
        self.assertEqual(reconcile(self.question,response,notes),response)
        response, notes=self.fixture()
        body=self.body.replace('核对<strong>资料版本</strong>，未经确认不得发布。','> 5分钟时停止。')
        notes[7]['content']=body
        response['provided_sources'][0].update(evidence=body,
            raw_evidence=[dict(field='content',char_start=0,char_end_exclusive=len(body),quote=body)])
        response=apply_guard(dict(answer=self.raw.replace('核对资料版本，未经确认不得发布。','5分钟时停止。'),
            sources=[],provided_sources=response['provided_sources'],trace=dict(finish_reason='stop')))
        self.assertEqual(reconcile(self.question,response,notes),response)


if __name__ == '__main__':
    unittest.main()
