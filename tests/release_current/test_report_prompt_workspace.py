# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import copy,json,tempfile,threading,unittest
from pathlib import Path
from pocket_memory.storage import NoteStore
from pocket_memory.report_workspace import ReportWorkspace,digest,export_html
from pocket_memory.report_sources import discover


class PromptWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=NoteStore(Path(self.tmp.name))
        self.ws=ReportWorkspace(self.store,None,threading.RLock())
        self.root=self.store.create_note('共同主题','主文连接基础。',category='技术',tags=['制造研究'],ai_status='not_requested')
        self.child=self.store.create_note('内部页','生产异常专有词。',ai_status='not_requested')
        self.store.connection.execute('UPDATE notes SET parent_note_id=?,tab_name=?,ocr_text=? WHERE id=?',(self.root.id,'实施页','扫描专有词证据。',self.child.id));self.store.connection.commit()
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_child_body_ocr_title_and_tags_find_root(self):
        for query in ['异常专有词','扫描专有词','实施页','制造研究','连接基础 异常专有词']:
            with self.subTest(query=query):
                d=discover(self.store,query);self.assertEqual(d['total'],1);self.assertEqual(d['items'][0]['id'],self.root.id)
        d=discover(self.store,'扫描专有词')['items'][0]
        self.assertEqual(d['matched_tab'],'实施页');self.assertEqual(d['matched_field'],'OCR');self.assertEqual(d['tabs'],2)
        self.assertNotIn('content',d);self.assertLessEqual(len(d['snippet']),182)
    def test_same_title_different_roots_kept_and_pagination_beyond_200(self):
        with self.store.connection:
            self.store.connection.executemany('INSERT INTO notes(title,content,markdown_path,source_type,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                [('同名资料','分页探针','fixture'+str(i)+'.md','text','2026-11-17','2026-11-17') for i in range(225)])
        first=discover(self.store,'分页探针',page=1,page_size=30);last=discover(self.store,'分页探针',page=8,page_size=30)
        self.assertEqual(first['total'],225);self.assertEqual(len(last['items']),15)
        self.assertFalse(set(i['id'] for i in first['items'])&set(i['id'] for i in last['items']))
        self.assertEqual(discover(self.store,"' OR 1=1 --")['total'],0)
    def test_document_body_and_location_and_type_filter(self):
        with self.store.connection:
            c=self.store.connection.execute("INSERT INTO documents(title,original_name,stored_path,file_type,size_bytes,sha256,created_at,updated_at) VALUES('已导入','demo.txt','fixture.txt','txt',10,'hash','2026','2026')")
            did=c.lastrowid
            self.store.connection.execute("INSERT INTO document_chunks(document_id,title,text,location,chunk_order) VALUES(?,?,?,?,?)",(did,'已导入','文档唯一字段值。','第七页',0))
        d=discover(self.store,'唯一字段值',kind='document');self.assertEqual(d['items'][0]['id'],did)
        self.assertEqual(discover(self.store,'第七页',kind='all')['total'],1)
        self.assertEqual(discover(self.store,'唯一字段值',kind='note')['total'],0)
    def plan(self):
        selected,units=self.ws.snapshot([{'kind':'note','id':self.root.id}])
        facts=[{'id':'F1','text':'保留原文条件。','refs':[]}]
        r=dict(id='a'*32,title='固定要求报告',version=1,created='2026-11-17',updated='2026-11-17',
               template='auto',selected=selected,units=units,focus='面向负责人。',refs=[],notes={},generation={'calls':2},
               adaptive={'status':'outline','facts':facts,'outline':[{'id':'C1','title':'关键条件','purpose':'说明前提','fact_ids':['F1']}]})
        self.ws.write(r);return r
    def test_outline_saved_reopened_and_conflicts_rejected(self):
        r=self.plan();outline=copy.deepcopy(r['adaptive']['outline']);outline[0]['title']='实施前提'
        updated=self.ws.save_outline(r['id'],{'version':1,'outline':outline})
        self.assertEqual(updated['version'],2);self.assertEqual(self.ws.read(r['id'])['adaptive']['outline'][0]['title'],'实施前提')
        with self.assertRaisesRegex(ValueError,'已更新'):self.ws.save_outline(r['id'],{'version':1,'outline':outline})
        with self.assertRaises(ValueError):self.ws.save_outline(r['id'],{'version':2,'outline':[]})
        with self.assertRaises(ValueError):self.ws.save_edit(r['id'],{'version':2,'title':'改变阅读契约'})
        with self.assertRaisesRegex(ValueError,'提纲'):export_html(updated)
    def test_compose_reuses_server_prepared_snapshot_and_never_client_payload(self):
        r=self.plan();captured={}
        def build(*args,**kw):
            captured.update(kw);return dict(r,adaptive=dict(status='complete',chapters=[]),generation=dict(calls=1))
        self.ws.build=build
        job=self.ws.compose(r['id'],{'version':1,'outline':r['adaptive']['outline'],'prepared':{'forged':'ignored'}})
        self.ws.worker.join(2)
        self.assertEqual(self.ws.job(job['id'])['status'],'done')
        self.assertEqual(captured['prepared']['facts'],r['adaptive']['facts'])
        self.assertNotIn('forged',captured['prepared']);self.assertEqual(self.ws.read(r['id'])['version'],3)
        from pocket_memory.report_adaptive_view import render_adaptive
        saved=self.ws.read(r['id'])
        self.assertNotIn('次新调用',render_adaptive(saved))
        self.assertEqual(saved['generation']['calls'],1)
        self.assertEqual(saved['planning_generation']['calls'],2)
        with self.assertRaises(ValueError):self.ws.compose(r['id'],{'version':3})
    def test_failed_compose_keeps_saved_plan(self):
        r=self.plan()
        def fail(*args,**kw):raise ValueError('预算已到')
        self.ws.build=fail;job=self.ws.compose(r['id'],{'version':1,'outline':r['adaptive']['outline']});self.ws.worker.join(2)
        self.assertEqual(self.ws.job(job['id'])['status'],'error');self.assertEqual(self.ws.read(r['id'])['adaptive']['status'],'outline')
        self.assertIsNone(self.ws.composing_report)
    def test_old_template_does_not_silently_ignore_custom_instructions(self):
        with self.assertRaisesRegex(ValueError,'自定义'):self.ws.create_job({'title':'旧模板','template':'project','focus':'只列风险','sources':[]})


if __name__=='__main__':unittest.main()
