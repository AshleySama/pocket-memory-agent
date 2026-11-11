# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Frozen behavioral expectations, authored before this batch's implementation.

These are exposed development/property checks, not a held-out accuracy dataset.
"""
from pathlib import Path
import sys,copy,json,sqlite3,unittest
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
sys.path.insert(0,str(HERE.parents[1]))
from pocket_memory.rag.retained.field_projection import FieldProjection,resolve_scope
from pocket_memory.rag.retained.projection_guard import validate_projection

BODY='| 对象名称 | 输入资料 | 输出 | 状态 | 适用场景 |\n| --- | --- | --- | --- | --- |\n| 资料检查助手 | 共同内容 | 风险报告 | 试用 | 项目复盘 |\n| 文档归档助手 | 原始文件 | 共同内容 | 试用 | 资料整理 |'
def note(i,title,body):return dict(id=i,title=title,content=body,parent_note_id=None,tab_name='')

class Completion(unittest.TestCase):
    def setUp(self):self.notes={1:note(1,'助手资料台账',BODY)}
    def ask(self,q,notes=None,ocr=None):
        r=FieldProjection(notes or self.notes,ocr or {}).prepare(q)
        return r['direct_record_response'] if r else None
    def test_cross_pair_equal_not_cartesian(self):
        q='资料检查助手的输入资料和文档归档助手的输出是否相同？'
        r=self.ask(q);self.assertEqual(r['answer_status'],'answered');self.assertIn('相同',r['answer'])
        self.assertNotIn('不完全相同',r['answer']);self.assertNotIn('风险报告',r['answer']);self.assertNotIn('原始文件',r['answer'])
        self.assertEqual([list(x['values']) for x in r['trace']['projection']['records']],[['输入资料'],['输出']])
    def test_swap_pair_equal(self):
        r=self.ask('文档归档助手的输出和资料检查助手的输入资料是否相同？')
        self.assertEqual(r['answer_status'],'answered');self.assertIn('相同',r['answer']);self.assertNotIn('不完全相同',r['answer'])
    def test_pair_values_without_compare(self):
        r=self.ask('资料检查助手的输入资料和文档归档助手的输出分别是什么？')
        self.assertEqual(r['answer_status'],'answered');self.assertNotIn('比较：',r['answer']);self.assertNotIn('风险报告',r['answer'])
    def test_renamed_reordered_different(self):
        body='| 输出 | 对象名称 | 状态 | 输入资料 |\n|---|---|---|---|\n| 乙结果 | 北岸检索器 | 就绪 | 甲输入 |\n| 丙结果 | 山谷归档器 | 待验 | 丁输入 |'
        r=self.ask('北岸检索器的输入资料与山谷归档器的输出是否相同？',{1:note(1,'新台账',body)})
        self.assertEqual(r['answer_status'],'answered');self.assertIn('不同',r['answer']);self.assertIn('甲输入',r['answer']);self.assertIn('丙结果',r['answer']);self.assertNotIn('乙结果',r['answer'])
    def test_empty_is_not_equal_or_different(self):
        self.notes[1]['content']=BODY.replace('原始文件 | 共同内容','原始文件 | ')
        r=self.ask('资料检查助手的输入资料和文档归档助手的输出是否相同？')
        self.assertIn('无法',r['answer']);self.assertNotIn('比较：相同',r['answer']);self.assertNotIn('比较：不同',r['answer'])
    def test_conditions_both_retained(self):
        condition='仅在复核签字后使用；未签字不生效。'
        self.notes[1]['content']=condition+'\n'+BODY
        r=self.ask('资料检查助手的输入资料和文档归档助手的输出是否相同？')
        self.assertIn(condition,r['answer'])
        for ref in r['sources']:self.assertIn(condition,ref['evidence'])
    def test_partial_known_field_unconfirmed_name(self):
        r=self.ask('资料检查助手的状态和适用范围是什么？')
        self.assertIn('状态：试用',r['answer']);self.assertIn('适用范围',r['answer']);self.assertIn('未确认',r['answer'])
        self.assertNotIn('项目复盘',r['answer']);self.assertNotEqual(r['answer_status'],'request_limited_excerpt')
    def test_partial_order_reversed(self):
        r=self.ask('资料检查助手的适用范围与状态分别是什么？')
        self.assertIn('状态：试用',r['answer']);self.assertIn('适用范围',r['answer']);self.assertIn('未确认',r['answer'])
    def test_partial_title_invariance(self):
        q='资料检查助手的状态和适用范围是什么？';a=self.ask(q)
        self.notes[2]=note(2,'适用范围','无关资料')
        b=self.ask(q);self.assertEqual((a['answer'],a['sources']),(b['answer'],b['sources']))
    def test_shared_column_keeps_old_comparison(self):
        r=self.ask('资料检查助手和文档归档助手的状态是否相同？')
        self.assertIn('状态比较：相同',r['answer']);self.assertEqual(len(r['sources']),2)
    def test_unknown_relationship_not_reinterpreted(self):
        r=self.ask('资料检查助手的适用场景和状态是否一致？')
        self.assertNotEqual(r['answer_status'],'answered')
    def test_shared_fields_with_endpoint_mix_ambiguous(self):
        r=self.ask('资料检查助手和文档归档助手的输入资料和文档归档助手的输出是否相同？')
        self.assertNotEqual(r['answer_status'],'answered')
    def test_duplicate_endpoint_not_arbitrarily_chosen(self):
        child=note(2,'页签2',BODY.replace('共同内容','另一内容'));child.update(parent_note_id=1,tab_name='页签2');self.notes[2]=child
        r=self.ask('只按《助手资料台账》的首页和页签2，资料检查助手的输入资料和文档归档助手的输出是否相同？')
        self.assertTrue(r is None or r['answer_status']!='answered')
    def test_generic_ocr_questions_keep_old_route(self):
        for q in ['怎么处理 OCR文本？','图片 OCR里常见的错误有哪些','OCR是什么，和全文搜索有哪些区别？']:
            with self.subTest(q=q):self.assertIsNone(self.ask(q))
    def test_explicit_ocr_stays_restricted(self):
        for q in ['只读OCR文本，资料检查助手的状态是什么？','已保存的OCR文本里是什么？']:
            r=self.ask(q);self.assertEqual(r['answer_status'],'scope_unresolved');self.assertEqual(r['sources'],[])
    def test_source_exclusion_variants(self):
        for verb in ['别用','不看','忽略','不要引用','不要援引','不要检索','不要查']:
            q=f'只按《助手资料台账》，{verb}OCR文本，资料检查助手的状态是什么？'
            r=self.ask(q,ocr={1:'1资料检查助手\n停用'})
            with self.subTest(verb=verb):
                self.assertIn('状态：试用',r['answer']);self.assertEqual({x['source_field'] for x in r['sources']},{'content'})
    def test_no_body_citation_even_in_excerpt(self):
        for q in ['资料检查助手的状态，不要引用正文','只按《助手资料台账》，不要援引正文，资料检查助手的状态是什么？']:
            r=self.ask(q);self.assertIsNotNone(r);self.assertNotIn('试用',r['answer']);self.assertEqual(r['sources'],[])
    def test_do_not_omit_condition_is_not_exclusion(self):
        q='只读《助手资料台账》的OCR文本，不要省略OCR中的条件，资料检查助手的状态是什么？'
        r=self.ask(q,ocr={1:'1资料检查助手\n停用\n须复核'})
        self.assertIn('须复核',r['answer']);self.assertEqual({x['source_field'] for x in r['sources']},{'ocr_text'})
    def test_unknown_negative_cannot_become_positive(self):
        r=self.ask('只按《助手资料台账》，不要借助OCR文本，资料检查助手的状态是什么？',ocr={1:'1资料检查助手\n停用'})
        self.assertNotIn('停用',r['answer']);self.assertFalse(any(x['source_field']=='ocr_text' for x in r['sources']))
    def test_soft_unnecessary_not_ocr_requirement(self):
        r=self.ask('只按《助手资料台账》，无需OCR文本，资料检查助手的状态是什么？',ocr={1:'1资料检查助手\n停用'})
        self.assertNotIn('停用',r['answer'])
    def test_guard_endpoint_tampering(self):
        q='资料检查助手的输入资料和文档归档助手的输出是否相同？'
        payload=self.ask(q)['trace']['projection'];payload=copy.deepcopy(payload)
        payload['records'][0]['values']={'输出':'风险报告'}
        self.assertTrue(validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}))
    def test_guard_serialized_roundtrip_and_location(self):
        q='资料检查助手的输入资料和文档归档助手的输出是否相同？'
        payload=json.loads(json.dumps(self.ask(q)['trace']['projection']))
        self.assertEqual(validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}),[])
        payload['refs'][0]['location']='已保存OCR'
        self.assertIn('field_location_mismatch',validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}))

# Private-library RealScope tests are intentionally not distributed.
if __name__=='__main__': unittest.main(verbosity=2)
