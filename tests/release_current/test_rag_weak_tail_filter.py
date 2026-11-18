# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Structural protection checks; no LLM, timing claim or semantic gold proxy."""
import copy
import unittest

from pocket_memory.rag.retained.weak_tail_filter import filter_weak_tails


QUESTION='设备为什么报警，应该如何解决？'


def raw(body,start,end):
    return dict(field='content',char_start=start,char_end_exclusive=end,quote=body[start:end])


def prose(eid,sid,body,start,end,title='操作记录'):
    return dict(evidence_id=eid,source_key=f'note:{sid}',source_id=sid,source_type='note',
        title=title,kind='content',source_field='content',source_verification='saved_original_spans',
        evidence=body[start:end],raw_evidence=[raw(body,start,end)],paragraph_range=[start,end],truncated=False)


def fixture(background='展厅记录介绍了空间布置和留念照片。',row_action='推荐分类与标签',outside=''):
    main='报警来自连接线路松动；处理时重新检查接口并记录核对结果。'
    body=main+'\n\n'+background
    header='| 对象名称 | 处理动作 | 状态 |'
    divider='| --- | --- | --- |'
    row=f'| 档案归类工具 | {row_action} | 试用 |'
    other='| 术语比对工具 | 汇总术语出处 | 规划 |'
    table=outside+header+'\n'+divider+'\n'+row+'\n'+other
    row_start=table.index(row);header_start=table.index(header)
    extra=dict(evidence_id='row',source_key='note:2',source_id=2,source_type='note',
        title='工具明细',entity='档案归类工具',kind='table_row',source_field='content',
        source_verification='saved_original_spans',evidence=header+'\n'+row,
        raw_evidence=[raw(table,header_start,header_start+len(header)),raw(table,row_start,row_start+len(row))],truncated=False)
    sources=[prose('main',1,body,0,len(main)),prose('alternate',1,body,len(main)+2,len(body)),extra]
    notes={1:dict(content=body),2:dict(content=table)}
    roles={'main':['primary_prose'],'alternate':['alternate_prose'],'row':['independent_row']}
    scores={'main':.99,'alternate':.004,'row':.002}
    return sources,notes,roles,scores


class WeakTailFilterTests(unittest.TestCase):
    def apply(self,question=QUESTION,**changes):
        sources,notes,roles,scores=fixture(**changes)
        kept,audit=filter_weak_tails(question,sources,scores=scores,roles=roles,notes=notes)
        return sources,kept,{item['evidence_id']:item for item in audit}

    def test_only_optional_verified_units_are_removed_without_rewriting(self):
        sources,kept,audit=self.apply()
        self.assertEqual([s['evidence_id'] for s in kept],['main'])
        self.assertIs(kept[0],sources[0])
        self.assertEqual(audit['main']['reason'],'primary_full_query_anchor')
        self.assertEqual(audit['row']['action'],'drop')
        self.assertEqual(audit['alternate']['action'],'drop')
        self.assertNotIn('evidence',audit['row']);self.assertNotIn('quote',audit['row'])

    def test_thresholds_are_all_required_and_not_a_uniform_score_cut(self):
        sources,notes,roles,scores=fixture()
        for highest,tail in ((.79,.001),(.99,.0101),(.8,.009)):
            with self.subTest(highest=highest,tail=tail):
                updated=dict(scores,main=highest,row=tail)
                kept,_=filter_weak_tails(QUESTION,sources,scores=updated,roles=roles,notes=notes)
                self.assertIn(sources[2],kept)
        roles['row']=[]
        kept,audit=filter_weak_tails(QUESTION,sources,scores=scores,roles=roles,notes=notes)
        self.assertIn(sources[2],kept)
        self.assertEqual(audit[-1]['reason'],'not_optional_channel_tail')

    def test_old_rows_primary_anchors_and_explicit_sources_are_preserved(self):
        sources,notes,roles,scores=fixture();scores['main']=.001
        roles['row']=['old_row','independent_row']
        kept,audit=filter_weak_tails(QUESTION,sources,scores=scores,roles=roles,notes=notes)
        self.assertIn(sources[0],kept);self.assertIn(sources[2],kept)
        roles['row']=['independent_row'];scores['main']=.99
        kept,audit=filter_weak_tails(QUESTION,sources,scores=scores,roles=roles,notes=notes,explicit_sources={'note:2'})
        self.assertIn(sources[2],kept);self.assertEqual(audit[-1]['reason'],'explicit_source_scope')

    def test_named_entity_field_and_value_requests_preserve_even_low_rank_rows(self):
        for question in ('档案归类工具如何使用？','设备报警的处理动作是什么？','哪些工具是试用状态？','工具明细说明了什么？'):
            with self.subTest(question=question):
                sources,kept,audit=self.apply(question)
                self.assertIn(sources[2],kept);self.assertEqual(audit['row']['action'],'keep')

    def test_conditions_exceptions_references_and_numeric_prose_are_not_discarded(self):
        for background in ('只有得到批准才能执行上述操作。','该措施不适用于紧急停机。','操作前提见上表。','服务期限为15天。'):
            with self.subTest(background=background):
                sources,kept,audit=self.apply(background=background)
                self.assertIn(sources[1],kept)
        sources,kept,audit=self.apply(row_action='推荐分类与标签，不改写原件')
        self.assertNotIn(sources[2],kept)  # The unasked complete row owns its limit.
        sources,kept,audit=self.apply('哪些工具不改写原件？',row_action='推荐分类与标签，不改写原件')
        self.assertIn(sources[2],kept)
        sources,kept,audit=self.apply(outside='所有功能均须由审核人批准后使用。\n\n')
        self.assertIn(sources[2],kept);self.assertEqual(audit['row']['reason'],'table_external_context')

    def test_complete_organization_grammar_is_not_a_source_text_allowlist(self):
        for outside in ('下面为工具明细。请根据状态选取条目。\n',
                        '本页列示工具明细。应按不同处理动作选择。\n',
                        '下面为工具明细。请按照不同状态选取条目。\n'):
            with self.subTest(outside=outside):
                sources,kept,audit=self.apply(outside=outside,row_action='推荐分类，不改写原件')
                self.assertNotIn(sources[2],kept)
                self.assertEqual(audit['row']['action'],'drop')

    def test_organization_clauses_with_appended_conditions_or_unknown_text_are_kept(self):
        for outside in ('下面为工具明细。请根据状态选取条目，须经主管审批。\n',
                        '下面为工具明细。请根据状态选取条目。每次金额不超过500元。\n',
                        '下面为工具明细。请根据状态选取条目；紧急事项另有例外。\n',
                        '下面为工具明细。请根据状态选取条目。费用含税。\n',
                        '下面为工具明细。不同意按状态选择。\n',
                        '下面为工具明细。应按不同意状态选择。\n',
                        '下面为工具明细。应按不同步状态选择。\n',
                        '# 操作前须经主管审批\n下面为工具明细。\n',
                        '下面为工具明细。\n[[审批要求]]\n'):
            with self.subTest(outside=outside):
                sources,kept,audit=self.apply(outside=outside)
                self.assertIn(sources[2],kept)
                self.assertEqual(audit['row']['reason'],'table_external_context')
        sources,kept,audit=self.apply(row_action='处理边界同上')
        self.assertIn(sources[2],kept);self.assertEqual(audit['row']['reason'],'cross_row_dependency')

    def test_short_table_missing_provenance_ocr_and_unknown_scores_are_preserved(self):
        sources,notes,roles,scores=fixture()
        small='| 对象名称 | 状态 |\n| --- | --- |\n| 档案归类工具 | 试用 |\n| 术语比对工具 | 规划 |'
        head=small.splitlines()[0];row=small.splitlines()[2];start=small.index(row)
        sources[2].update(evidence=head+'\n'+row,raw_evidence=[raw(small,0,len(head)),raw(small,start,start+len(row))])
        notes[2]['content']=small
        kept,audit=filter_weak_tails(QUESTION,sources,scores=scores,roles=roles,notes=notes)
        self.assertIn(sources[2],kept);self.assertEqual(audit[-1]['reason'],'short_table_schema')
        for change in ('missing_notes','ocr','bad_span','missing_score'):
            with self.subTest(change=change):
                sources,notes,roles,scores=fixture()
                if change=='missing_notes':notes=None
                if change=='ocr':sources[2]['source_field']='ocr_text'
                if change=='bad_span':sources[2]['raw_evidence'][1]['quote']='无法对应原文'
                if change=='missing_score':scores.pop('row')
                kept,_=filter_weak_tails(QUESTION,sources,scores=scores,roles=roles,notes=notes)
                self.assertIn(sources[2],kept)

    def test_multi_source_comparison_and_condition_selection_keep_counterparts(self):
        for question in ('两个项目的输入和输出分别是什么？','甲与乙的费用对比如何？',
                         '哪些措施需要人工复核？','说明原因以及另一份资料的处理建议。',
                         '设备为什么报警，供应商如何处理退货？','甲公司如何申请，乙公司如何复核？'):
            with self.subTest(question=question):
                sources,kept,audit=self.apply(question)
                self.assertEqual(kept,sources)
                self.assertEqual(audit['row']['reason'],'multi_subject_or_selection_request')

    def test_source_objects_and_citation_identities_are_unchanged(self):
        sources,notes,roles,scores=fixture();before=copy.deepcopy(sources)
        filter_weak_tails(QUESTION,sources,scores=scores,roles=roles,notes=notes)
        self.assertEqual(sources,before)


if __name__=='__main__':unittest.main()
