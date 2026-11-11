# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys,copy,json,unittest
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]))
from pocket_memory.rag.retained.field_projection import FieldProjection,resolve_scope
from pocket_memory.rag.retained.projection_guard import validate_projection,render_projection

def note(i,title,body):return dict(id=i,title=title,content=body,parent_note_id=None,tab_name='')
BODY='| 对象名称 | 输入资料 | 输出 | 状态 | 适用场景 |\n| --- | --- | --- | --- | --- |\n| 资料检查助手 | 共同内容 | 风险报告 | 试用 | 项目复盘 |\n| 文档归档助手 | 原始文件 | 共同内容 | 试用 | 资料整理 |'
OCR='编号\n名称\n状态\n1资料检查助手\nOCR保存状态\n停用\n2文档归档助手\n其他记录\n规划'
class Boundaries(unittest.TestCase):
    def setUp(self):self.notes={1:note(1,'助手资料台账',BODY)}
    def ask(self,q,notes=None,ocr=None):
        p=FieldProjection(notes or self.notes,ocr or {})
        result=p.prepare(q)
        return result['direct_record_response'] if result else None
    def test_unrelated_title_invariance(self):
        q='资料检查助手的状态和适用范围是什么？'
        for title in ('适用范围','状态和适用范围','任何其他标题'):
            ns=copy.deepcopy(self.notes);ns[2]=note(2,title,'无关正文')
            before,after=self.ask(q),self.ask(q,ns)
            self.assertEqual((before['answer'],before['sources']),(after['answer'],after['sources']))
    def test_bound_title_must_be_unique(self):
        ns=copy.deepcopy(self.notes);ns[2]=note(2,'助手资料台账','其他')
        self.assertIsNone(self.ask('只按《助手资料台账》，资料检查助手的状态是什么？',ns))
    def test_cross_endpoints_not_cartesian(self):
        for q in ('资料检查助手的输入资料和文档归档助手的输出是否相同？',
                  '文档归档助手的输出和资料检查助手的输入资料是否相同？',
                  '资料检查助手的输入资料和文档归档助手的输出分别是什么？'):
            # v3 explicitly binds the requested endpoints; v2's partial boundary
            # is intentionally replaced, while forbidding the Cartesian answer.
            r=self.ask(q)
            self.assertEqual(r['answer_status'],'answered')
            self.assertNotIn('风险报告',r['answer']);self.assertNotIn('原始文件',r['answer'])
    def test_comparison_operand_scope(self):
        q='资料检查助手和文档归档助手的输入资料、输出和状态分别是什么，状态是否相同？'
        r=self.ask(q);self.assertEqual(r['answer'].count('比较：'),1)
        self.assertIn('状态比较：相同',r['answer'])
        self.assertEqual(r['trace']['projection']['requested']['compare'],['状态'])
    def test_explicit_same_column_and_swapped_entities(self):
        for a,b in [('资料检查助手','文档归档助手'),('文档归档助手','资料检查助手')]:
            r=self.ask(f'{a}和{b}的状态是否相同？')
            self.assertIn('状态比较：相同',r['answer'])
    def test_unbound_compare_predicate_falls_back(self):
        self.assertEqual(self.ask('资料检查助手和文档归档助手的状态是什么，是否相同？')['answer_status'],'request_limited_excerpt')
    def test_one_entity_cross_field_not_projected(self):
        self.assertEqual(self.ask('资料检查助手的适用场景和状态是否一致？')['answer_status'],'request_limited_excerpt')
    def test_negative_read_ocr_uses_body(self):
        ns={1:note(1,'接口ID：818',BODY)}
        for verb in ('不要使用','不能读取','禁止参考','不采用'):
            q=f'接口818{verb}已保存的OCR文本，只按正文，资料检查助手的状态是什么？'
            r=self.ask(q,ns,{1:OCR});self.assertIn('状态：试用',r['answer']);self.assertNotIn('停用',r['answer'])
            self.assertEqual({s['source_field'] for s in r['sources']},{'content'})
    def test_negative_omission_not_negative_read(self):
        ns={1:note(1,'接口ID：818',BODY)}
        r=self.ask('接口818只读已保存的OCR文本，不要省略OCR中的限制条件，资料检查助手的状态是什么？',ns,{1:OCR})
        self.assertIn('停用',r['answer']);self.assertEqual({s['source_field'] for s in r['sources']},{'ocr_text'})
    def test_unproved_constrained_request_cannot_escape(self):
        ns={1:note(1,'接口ID：818','正文没有可证明的结构')}
        r=self.ask('接口818不要使用已保存的OCR文本，只按正文，资料检查助手的状态是什么？',ns,{1:OCR})
        self.assertEqual(r['answer_status'],'scope_unresolved');self.assertEqual(r['sources'],[])
    def test_contradiction_not_arbitrarily_resolved(self):
        ns={1:note(1,'接口ID：818',BODY)}
        r=self.ask('接口818只读OCR文本，不要使用OCR文本，资料检查助手的状态是什么？',ns,{1:OCR})
        self.assertEqual(r['answer_status'],'scope_unresolved');self.assertEqual(r['sources'],[])
    def test_per_source_exclusion(self):
        ns={1:note(1,'第一份资料',BODY),2:note(2,'第二份资料',BODY)}
        q='只读《第一份资料》的正文，不要使用OCR文本；只读《第二份资料》的OCR文本，资料检查助手的状态是什么？'
        s=resolve_scope(q,ns)
        self.assertEqual(s.pairs,{1:{'content'},2:{'ocr_text'}})
        self.assertIn((1,'ocr_text'),s.excluded);self.assertNotIn((2,'ocr_text'),s.excluded)
        self.assertEqual(self.ask(q,ns,{2:OCR})['answer_status'],'scope_unresolved')
    def test_original_positions_in_plan(self):
        q='只按《助手资料台账》，资料检查助手 和 文档归档助手的状态是否相同？'
        plan=self.ask(q)['trace']['projection']['requested']
        for m in plan['mentions']['entities']+plan['mentions']['fields']:
            self.assertEqual(q[slice(*m['span'])],m.get('entity',m.get('field')))
        op=plan['operations'][0];self.assertEqual(q[slice(*op['predicate_span'])],'相同')
    def test_display_injection_rejected_and_not_rendered(self):
        ns={1:note(1,'接口ID：818',BODY)};q='接口818只读已保存的OCR文本，资料检查助手的状态是什么？'
        payload=self.ask(q,ns,{1:OCR})['trace']['projection'];payload['refs'][0]['evidence']='注入假原文'
        self.assertIn('display_not_original_serialization',validate_projection(q,resolve_scope(q,ns),payload,ns,{1:OCR}))
        shown=render_projection(payload);self.assertNotIn('注入假原文',json.dumps(shown,ensure_ascii=False))
    def test_unknown_named_ocr_source_stays_constrained(self):
        r=self.ask('只读《不存在的来源》的OCR文本，资料检查助手的状态是什么？')
        self.assertEqual(r['answer_status'],'scope_unresolved');self.assertEqual(r['sources'],[])
    def test_payload_comparison_not_self_certified(self):
        q='资料检查助手和文档归档助手的状态是否相同？'
        payload=self.ask(q)['trace']['projection'];payload['requested']['operations'][0]['entities']=['资料检查助手']
        self.assertIn('unbound_comparison',validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}))

if __name__=='__main__':unittest.main(verbosity=2)
