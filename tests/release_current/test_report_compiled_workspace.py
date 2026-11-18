# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
from pocket_memory.storage import NoteStore
from pocket_memory.report_workspace import ReportWorkspace, export_html, render_report


class CompiledWorkspaceTests(unittest.TestCase):
    def test_known_editor_wrappers_are_readable_but_unknown_markup_remains_literal(self):
        from pocket_memory.report_compiled_view import _prose
        original='<strong><span style="color:red">资料方面</span></strong><br><script>alert(1)</script> 不改变条件'
        output=_prose(original)
        self.assertEqual(output,'资料方面<br>&lt;script&gt;alert(1)&lt;/script&gt; 不改变条件')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=NoteStore(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.close)
        self.ws=ReportWorkspace(self.store,None,threading.RLock())
        self.note=self.store.create_note('资料表','| 名称 | 状态 |\n| --- | --- |\n| 示例 <script> | 试用，审批后才能使用 |',ai_status='not_requested')
        self.selected,self.units=self.ws.snapshot([{'kind':'note','id':self.note.id}])

    def generate_fixture(self,engine,title,focus,units,register,*args,**kwargs):
        u=units[0];rid=register(u,0,len(u['body']))
        a=dict(reading_mode='source_compiled_v1',status='complete',coverage=dict(source_scan_blocks=1,total_blocks=1),
            issues=[],facts=[],chapters=[dict(id='C1',title='使用前提',purpose='核对限制',status='complete',
            guidance=[dict(kind='editorial',text='编写建议：对照资料核对审批条件。')],paragraphs=[],
            tables=[dict(caption='原文表格',columns=['名称','状态'],placement='main',rows=[dict(cells=['示例 <script>','试用，审批后才能使用'],refs=[rid],fact_ids=[])])],
            content_order=[dict(kind='table',index=0)])])
        return a,[],dict(calls=1,seconds=1.25)

    def test_compiled_uses_new_generator_and_keeps_tables_citations_and_safe_export(self):
        with patch('pocket_memory.report_brief.generate',side_effect=self.generate_fixture) as fast,patch('pocket_memory.report_adaptive.generate') as old:
            r=self.ws.build('示例培训','auto',self.selected,self.units,focus='解释输入与审批',composition_mode='source_compiled')
        self.assertEqual(fast.call_count,1);old.assert_not_called()
        self.assertEqual(len(r['tables']),1);self.assertEqual(r['composition_mode'],'source_compiled')
        output=export_html(r)
        self.assertIn('试用，审批后才能使用',output);self.assertIn('&lt;script&gt;',output)
        self.assertIn('编写建议 · 非原文规定',output)
        self.assertNotIn('不代表通读全文',output);self.assertNotIn('完整送入模型：',output)
        self.assertIn('id="source-1"',output);self.assertIn('href="#source-1"',output)
        self.ws.write(r);self.ws.save_edit(r['id'],{'version':1,'notes':{'chapter-C1':'用户补充核对项'}})
        reopened=self.ws.read(r['id']);self.assertIn('用户补充核对项',render_report(reopened))

    def test_saved_legacy_preparation_wins_over_new_mode(self):
        with patch('pocket_memory.report_adaptive.generate',return_value=({'status':'outline','outline':[]},[],{'calls':0})) as old,patch('pocket_memory.report_compiled.generate') as fast:
            r=self.ws.build('旧提纲','auto',self.selected,self.units,prepared={'reading_mode':'direct_source_ledger'},composition_mode='source_compiled')
        old.assert_called_once();fast.assert_not_called();self.assertEqual(r['composition_mode'],'model_written')

    def test_export_keeps_source_order_when_background_precedes_main(self):
        with patch('pocket_memory.report_brief.generate',side_effect=self.generate_fixture):
            r=self.ws.build('原文顺序','auto',self.selected,self.units,composition_mode='source_compiled')
        c=r['adaptive']['chapters'][0]
        c['paragraphs']=[dict(text='原文开头需要先读',refs=[1],placement='supplement'),dict(text='接着读取条件与结论',refs=[1],placement='main')]
        c['content_order']=[dict(kind='paragraph',index=0),dict(kind='paragraph',index=1)]
        output=export_html(r)
        self.assertLess(output.index('原文开头需要先读'),output.index('接着读取条件与结论'))

    def test_default_api_and_default_build_keep_prior_path(self):
        with patch('pocket_memory.report_adaptive.generate',return_value=({'status':'outline','outline':[]},[],{'calls':0})) as old:
            r=self.ws.build('原路径','auto',self.selected,self.units)
        old.assert_called_once();self.assertEqual(r['composition_mode'],'model_written')

    def test_invalid_mode_rejected_before_snapshot_or_worker(self):
        with self.assertRaisesRegex(ValueError,'写作方式'):
            self.ws.create_job({'title':'模式验证','template':'auto','composition_mode':'unknown','sources':[]})
        with self.assertRaisesRegex(ValueError,'快速编排'):
            self.ws.create_job({'title':'模式验证','template':'project','composition_mode':'source_compiled','sources':[]})

    def test_new_job_propagates_mode_and_preserves_snapshot(self):
        captured={}
        def fake_build(*args,**kwargs):
            captured.update(kwargs)
            # A local fixture only; no completion is performed.
            with patch('pocket_memory.report_brief.generate',side_effect=self.generate_fixture):
                return ReportWorkspace.build(self.ws,*args,**kwargs)
        with patch.object(self.ws,'build',side_effect=fake_build):
            job=self.ws.create_job(dict(title='快速培训',template='auto',composition_mode='source_compiled',sources=[dict(kind='note',id=self.note.id)]))
            self.ws.worker.join(10)
        self.assertEqual(captured['composition_mode'],'source_compiled')
        j=self.ws.job(job['id']);self.assertEqual(j['status'],'done',j)
        self.assertEqual(self.ws.read(j['report_id'])['units'],self.units)


if __name__=='__main__':unittest.main()
