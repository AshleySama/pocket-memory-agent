# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

HOME=Path(__file__).resolve().parent
sys.path.insert(0,str(HOME.parents[1]))
from pocket_memory.report_workspace import ReportWorkspace,TEMPLATES,valid_ref,markdown_tables,export_html,render_report
from pocket_memory.storage import NoteStore

class FakeEngine:
    def generate_report_claims(self,title,template,blocks):
        return [{'topic':'事实摘要','claim':b['text'],'quote':b['text'],'evidence':[b['key']]} for b in blocks[:2]]

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=NoteStore(Path(self.tmp.name))
        self.ws=ReportWorkspace(self.store,FakeEngine(),threading.RLock())
        self.note=self.store.create_note('项目记录','本周完成资料整理，并保留所有原文引用。\n\n风险一：资料过杂。处理动作：检查主题；负责人：演示人；完成时间：演示前。\n\n风险二：原文难读。处理动作：保留页码；负责人：演示人；完成时间：导入后。\n\n风险三：冷启动慢。处理动作：提前预热；负责人：演示人；完成时间：开始前。',ai_status='not_requested')
        self.table=self.store.create_note('场景清单','| 名称 | 状态 |\n| --- | --- |\n| A | 已验证 |\n| B | 试用 |\n| C | 规划 |\n| D | 已验证 |',ai_status='not_requested')
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def build(self,template='project'):
        selected,units=self.ws.snapshot([{'kind':'note','id':self.note.id},{'kind':'note','id':self.table.id}])
        return self.ws.build('测试项目',template,selected,units)
    def test_all_risks_and_table_denominator(self):
        r=self.build(); self.assertEqual(len(r['actions']),3)
        self.assertEqual({x['label']:x['value'] for x in r['charts'][0]['series']},{'已验证':2,'试用':1,'规划':1})
        self.assertEqual(r['charts'][0]['total'],4)
        self.assertTrue(all(valid_ref(x,r['units']) for x in r['refs']))
    def test_multitab_and_ocr_identity_not_counted_twice(self):
        self.store.connection.execute('UPDATE notes SET parent_note_id=?,tab_name=?,ocr_text=? WHERE id=?',(self.note.id,'状态页','OCR 独立原文，保留页签与字段位置。',self.table.id));self.store.connection.commit()
        r=self.build();self.assertEqual(len(r['selected']),1)
        self.assertEqual(len(r['charts']),1)
        ocr=next(x for x in r['refs'] if x['field']=='ocr_text')
        self.assertEqual(ocr['tab'],'状态页');self.assertEqual(ocr['note_id'],self.table.id)
    def test_tampered_identity_and_span_rejected(self):
        r=self.build()
        for key,value in [('field','ocr_text'),('tab','假页签'),('quote','伪造'),('revision','0'),('start',-1)]:
            ref=copy.deepcopy(r['refs'][0]);ref[key]=value;self.assertFalse(valid_ref(ref,r['units']),key)
    def test_stale_source_keeps_snapshot(self):
        r=self.build();self.ws.write(r);ref=r['refs'][0]
        self.store.connection.execute('UPDATE notes SET content=? WHERE id=?',('修改后的原文',self.note.id));self.store.connection.commit()
        result=self.ws.source(r['id'],ref['id'])
        self.assertTrue(result['changed']);self.assertEqual(result['ref']['quote'],ref['quote']);self.assertIn(ref['quote'],result['snapshot'])
    def test_atomic_save_revision_and_restart(self):
        r=self.build();self.ws.write(r)
        r2=self.ws.save_edit(r['id'],{'version':1,'notes':{'summary':'人工补充'}})
        self.assertEqual(r2['version'],2)
        with self.assertRaises(ValueError):self.ws.save_edit(r['id'],{'version':1,'title':'过期覆盖'})
        new=ReportWorkspace(self.store,FakeEngine(),threading.RLock());self.assertEqual(new.read(r['id'])['notes']['summary'],'人工补充')
    def test_export_full_data_escape_no_external_assets(self):
        r=self.build();r['title']='<script>alert(1)</script>';out=export_html(r)
        self.assertNotIn('<script',out);self.assertIn('&lt;script&gt;',out)
        self.assertIn('<svg',out);self.assertIn('D</td>',out);self.assertIn('source-1',out)
        self.assertNotIn('src="http',out);self.assertNotIn('127.0.0.1',out)
    def test_six_types_valid_and_distinct(self):
        titles=set()
        for t in TEMPLATES:
            r=self.build(t);out=render_report(r);self.assertIn('section-summary',out);titles.add(r['template_label'])
        self.assertEqual(len(titles),6)
    def test_reject_fabricated_quote(self):
        self.ws.engine=SimpleNamespace(generate_report_claims=lambda *args:[{'topic':'虚构','claim':'完成百分之百','quote':'从未存在的原文内容','evidence':['E1']}])
        r=self.build();self.assertEqual(r['claims'],[]);self.assertEqual(len(r['generation']['rejected']),1);self.assertEqual(len(r['actions']),3)
    def test_malformed_table_no_chart(self):
        self.assertEqual(markdown_tables('| 名称 | 状态 |\n| --- | --- |\n| A | 好 | 多一列 |'),[])
        self.assertEqual(markdown_tables('OCR 名称 状态 A好'),[])
    def test_invalid_scope_and_paths(self):
        for sources in ([],[{'kind':'evil','id':1}]):
            with self.assertRaises(ValueError):self.ws.snapshot(sources)
        with self.assertRaises(ValueError):self.ws.read('../../config')

if __name__=='__main__':unittest.main(verbosity=2)
