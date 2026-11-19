# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Generic source-partition contracts; model relevance is tested separately."""
import copy
import unittest
from pocket_memory.report_section_inventory import build, _single_stream
from pocket_memory import report_structured, report_brief
from test_report_brief import unit, Registry


def guide(uid, sid, prefix=''):
    return unit(uid, prefix+'## 接收\n核对记录；未批准不得发布。\n\n## 归档\n保存审批凭据。\n', sid)


class MultiSourceSectionsTests(unittest.TestCase):
    def assert_inventory(self, units, result):
        self.assertTrue(result['coverage']['complete'])
        by_unit={u['unit_id']:u for u in units}
        for key in ('blocks','facts','groups','frames'):
            self.assertEqual(len(result[key]),len({x['id'] for x in result[key]}))
        blocks={b['id']:b for b in result['blocks']}
        facts={f['id']:f for f in result['facts']}
        for f in facts.values():
            for e in f['evidence']:
                self.assertEqual(e['quote'],by_unit[e['unit_id']]['body'][e['start']:e['end']])
                self.assertEqual(e['quote'],blocks[e['source_id']]['text'])
        for g in result['groups']:
            self.assertTrue(g['fact_ids'])
            for fid in g['fact_ids']:
                self.assertTrue(set(facts[fid]['source_ids'])<=set(g['source_ids']))

    def test_multiple_sources_reuse_sections_without_source_mutation(self):
        units=[guide('n1',1),guide('n2',2)];before=copy.deepcopy(units)
        self.assertIsNone(report_structured.plan(units))
        r=build(units,Registry());self.assert_inventory(units,r)
        self.assertEqual([g['title'] for g in r['groups']],['接收','归档']*2)
        self.assertEqual([g['unit_ids'] for g in r['groups']],[['n1'],['n1'],['n2'],['n2']])
        self.assertEqual(units,before)

    def test_note_tabs_with_same_source_do_not_merge_preambles(self):
        units=[guide('tab1',1,'仅限内部资料。\n'),guide('tab2',1,'仅限公开资料。\n')]
        r=build(units,Registry());self.assert_inventory(units,r)
        for g in r['groups']:
            for dep in g.get('required_group_ids',[]):
                owner=next(x for x in r['groups'] if x['id']==dep)
                self.assertEqual(g['unit_ids'],owner['unit_ids'])
        self.assertEqual([g['title'] for g in r['groups']].count('引言'),2)

    def test_document_continuation_with_another_source_preserves_tail_condition(self):
        units=[unit('p1','# 工作手册\n## 接收\n未取得批准时，\n',1,'document',field='document_text'),
               unit('p2','# 工作手册\n不得发布。\n## 归档\n保存凭据。\n',1,'document',field='document_text'),guide('n2',2)]
        r=build(units,Registry());self.assert_inventory(units,r)
        group=r['groups'][0];self.assertEqual(group['unit_ids'],['p1','p2'])
        text=''.join(b['text'] for b in r['blocks'] if b['id'] in group['source_ids'])
        self.assertIn('不得发布',text);self.assertNotIn('保存凭据',text)
        self.assertTrue(any(o['reason']=='repeated_document_header' for o in r['omissions']))

    def test_unstructured_table_fallback_does_not_coarsen_other_sources(self):
        units=[guide('n1',1),unit('table','| 名称 | 条件 |\n| --- | --- |\n| 甲 | 批准后 |\n| 乙 | 未批准禁止 |',2)]
        r=build(units,Registry());self.assert_inventory(units,r)
        self.assertEqual(len([g for g in r['groups'] if g.get('source_sections')]),2)
        table_group=next(g for g in r['groups'] if 'table' in g['unit_ids'])
        self.assertGreaterEqual(len(table_group['fact_ids']),2)
        header=next(b['id'] for b in r['blocks'] if b['kind']=='table_header')
        for b in r['blocks']:
            if b['kind']=='table_row':self.assertEqual(b['required_ids'],[header])

    def test_ocr_and_content_stay_separate(self):
        units=[guide('body',1),unit('image','识别结果：执行前必须复核图片。',1,field='ocr_text')]
        r=build(units,Registry());self.assert_inventory(units,r)
        self.assertEqual(len(r['groups']),3)
        self.assertFalse(r['groups'][-1].get('source_sections',False))
        self.assertEqual(r['groups'][-1]['unit_ids'],['image'])

    def test_kind_prevents_numeric_source_id_collision(self):
        units=[guide('note',1),unit('doc','## 审批\n核对签字。\n## 发布\n登记日期。',1,'document',field='document_text')]
        r=build(units,Registry());self.assert_inventory(units,r)
        self.assertEqual(len(r['groups']),4)

    def test_single_stream_is_unchanged_and_old_cache_identity_preserved(self):
        units=[guide('n1',1)]
        self.assertEqual(build(units,Registry()),_single_stream(units,Registry()))

    def test_all_unsupported_stays_on_old_conservative_path(self):
        self.assertIsNone(build([unit('a','普通段落。',1),unit('b','另一个段落。',2)],Registry()))

    def test_rebinding_uses_new_registry_not_probe_numbers(self):
        units=[guide('a',1),guide('b',2)]
        reg=Registry(100);r=build(units,reg);self.assert_inventory(units,r)
        self.assertTrue(all(ref>100 for f in r['facts'] for ref in f['refs']))
        self.assertEqual(len(reg.items),len(r['blocks']))

    def test_duplicate_unit_ids_rejected(self):
        with self.assertRaises(ValueError):build([guide('a',1),guide('a',2)],Registry())

    def test_same_named_sections_keep_distinct_decisions_and_exact_refs(self):
        units=[guide('a',1),guide('b',2)];r=build(units,Registry())
        groups=r['groups'];facts=r['facts'];chosen={groups[0]['id'],groups[3]['id']}
        import json
        response=dict(finish_reason='stop',text=json.dumps(dict(scope=dict(include='指定章节',exclude='其他'),
            decisions=[dict(group=g['id'],decision='include' if g['id'] in chosen else 'exclude',reason='夹具') for g in groups],
            chapters=[dict(title='两篇选段',groups=[g['id'] for g in groups if g['id'] in chosen])]),ensure_ascii=False))
        _,selected,selection=report_brief.parse_organization(response,groups,facts)
        self.assertEqual(set(selection['included_group_ids']),chosen)
        self.assertEqual([f['unit_id'] for f in selected],['a','b'])
        self.assertIn('未批准不得发布',selected[0]['text'])
        self.assertIn('保存审批凭据',selected[1]['text'])


if __name__=='__main__':unittest.main()
