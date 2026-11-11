# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys,unittest,copy,json,sqlite3
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
sys.path.insert(0,str(HERE.parents[1]))
from pocket_memory.rag.retained.field_projection import FieldProjection,resolve_scope
from pocket_memory.rag.retained.projection_guard import validate_projection

def note(i,title,body,**kw):return dict(id=i,title=title,content=body,parent_note_id=None,tab_name='',**kw)
BODY='| 对象名称 | 输入资料 | 输出 | 状态 |\n| --- | --- | --- | --- |\n| 设备甲乙 | 图纸 | 检查报告 | 就绪 |\n| 设备丙丁 | 清单 | | 待验 |'
class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.notes={1:note(1,'设备台账',BODY)};self.p=FieldProjection(self.notes,{})
    def response(self,q,p=None):return (p or self.p).prepare(q)['direct_record_response']
    def test_full_projection(self):
        r=self.response('设备甲乙的输入资料、输出和状态分别是什么？')
        self.assertTrue(r['citation_validation']['passed']);self.assertIn('检查报告',r['answer']);self.assertEqual(len(r['sources'][0]['raw_evidence']),2)
    def test_unknown_ask_falls_back(self):self.assertEqual(self.response('设备甲乙的输出是什么，为什么有业务风险？')['answer_status'],'request_limited_excerpt')
    def test_absent_entity_full_table(self):
        r=self.response('只按《设备台账》，设备戊己的输出和状态分别是什么？没有这一项就说明找不到。')
        self.assertIn('全部 2 条',r['answer']);self.assertEqual(r['sources'][0]['raw_evidence'][0]['char_end_exclusive'],len(BODY))
    def test_truncated_table_no_absence(self):
        self.notes[1]['content']=BODY+'\n| 3 | ...'
        p=FieldProjection(self.notes,{})
        self.assertIsNone(p.prepare('只按《设备台账》，设备戊己的输出是什么？'))
    def test_empty_cell(self):self.assertIn('该单元格为空',self.response('设备丙丁的输出是什么？')['answer'])
    def test_missing_column_preserves_known(self):
        r=self.response('设备甲乙的输入资料、输出和“预算”字段分别是什么？')
        self.assertIn('图纸',r['answer']);self.assertIn('检查报告',r['answer']);self.assertIn('未提供该名称的列',r['answer'])
    def test_subject_ambiguous(self):self.assertIsNone(self.p.prepare('按《设备台账》，它的输出是什么？'))
    def test_scope_pairs(self):
        self.notes[2]=note(2,'另一台账',BODY)
        s=resolve_scope('结合《设备台账》的正文和《另一台账》的OCR，设备甲乙的输出是什么？',self.notes)
        self.assertEqual(s.pairs,{1:{'content'},2:{'ocr_text'}})
        p=FieldProjection(self.notes,{2:BODY})
        self.assertEqual(self.response('结合《设备台账》的正文和《另一台账》的OCR，设备甲乙的输出是什么？',p)['answer_status'],'scope_unresolved')
    def test_tabs_conflict_preserved(self):
        child=note(2,'页签2',BODY.replace('就绪','停用'));child.update(parent_note_id=1,tab_name='页签2');self.notes[2]=child
        p=FieldProjection(self.notes,{})
        r=self.response('只按《设备台账》的首页和页签2，设备甲乙的状态是什么？',p)
        self.assertIn('就绪',r['answer']);self.assertIn('停用',r['answer']);self.assertEqual({s['source_id'] for s in r['sources']},{1,2})
    def test_modified_span_rejected(self):
        q='设备甲乙的输出是什么？';r=self.response(q);payload=r['trace']['projection'];payload['refs'][0]['raw_evidence'][1]['char_start']+=1
        self.assertIn('original_span_mismatch',validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}))
    def test_wrong_value_rejected(self):
        q='设备甲乙的输出是什么？';payload=self.response(q)['trace']['projection'];payload['records'][0]['values']['输出']='虚构'
        self.assertIn('cell_mismatch',validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}))
    def test_omitted_field_rejected(self):
        q='设备甲乙的输出和状态是什么？';payload=self.response(q)['trace']['projection'];del payload['records'][0]['values']['状态']
        self.assertIn('field_omitted_or_added',validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}))
    def test_revision_rejected(self):
        q='设备甲乙的输出是什么？';payload=self.response(q)['trace']['projection'];self.notes[1]['content']+='\n'
        self.assertIn('stale_source',validate_projection(q,resolve_scope(q,self.notes),payload,self.notes,{}))
    def test_ocr_cannot_use_body(self):
        p=FieldProjection(self.notes,{})
        r=self.response('只读《设备台账》的OCR，设备甲乙的输出是什么？',p)
        self.assertEqual(r['sources'],[]);self.assertNotIn('检查报告',r['answer'])
    def test_interface_ocr_conflict(self):
        n={1:note(1,'接口ID：42','type String 正文值')};ocr={1:'字段\n类型\n说明\ntype\nNumber\nint(1)\n记录类型，1甲2乙\nother\nString\n其他值'}
        p=FieldProjection(n,ocr);q='接口42保存的OCR里，记录类型看哪个字段，返回类型是什么，值1和2分别表示什么？'
        r=self.response(q,p);self.assertIn('Number',r['answer']);self.assertNotIn('正文值',r['answer']);self.assertEqual(r['sources'][0]['source_field'],'ocr_text')
        payload=r['trace']['projection'];payload['refs'][0]['source_field']='content'
        self.assertIn('source_field_outside_scope',validate_projection(q,resolve_scope(q,n),payload,n,ocr))
    def test_interface_extra_question_not_swallowed(self):
        n={1:note(1,'接口ID：42','')};ocr={1:'type\nNumber\n记录类型，1甲2乙\nother\nString\n其他值'}
        p=FieldProjection(n,ocr)
        r=self.response('接口42保存的OCR里，记录类型是什么，为什么会造成业务风险？',p)
        self.assertNotEqual(r['answer_status'],'answered')
    def test_ocr_topic_is_not_field_scope(self):
        self.assertIsNone(self.p.prepare('OCR是什么，和全文搜索有哪些区别？'))
    def test_unprovided_enum_not_complete(self):
        n={1:note(1,'接口ID：42','')};ocr={1:'type\nNumber\n记录类型，1甲2乙\nother\nString\n其他值'}
        r=self.response('接口42保存的OCR里，记录类型值1和3分别表示什么？',FieldProjection(n,ocr))
        self.assertNotEqual(r['answer_status'],'answered')
    def test_external_condition_is_visible_and_bound(self):
        text='仅在复核通过后适用；未签字不得使用。'
        self.notes[1]['content']=text+'\n\n'+BODY
        p=FieldProjection(self.notes,{})
        r=self.response('设备甲乙的输出是什么？',p)
        self.assertIn(text,r['answer']);self.assertIn(text,[s['quote'] for s in r['sources'][0]['raw_evidence']])
        payload=r['trace']['projection'];payload['records'][0]['context']=[]
        self.assertIn('external_conditions_missing',validate_projection('设备甲乙的输出是什么？',resolve_scope('设备甲乙的输出是什么？',self.notes),payload,self.notes,{}))
    def test_long_external_prose_keeps_old_route(self):
        self.notes[1]['content']='业务说明。'*100+'\n\n'+BODY
        self.assertIsNone(FieldProjection(self.notes,{}).prepare('设备甲乙的输出是什么？'))

if __name__=='__main__':unittest.main(verbosity=2)
