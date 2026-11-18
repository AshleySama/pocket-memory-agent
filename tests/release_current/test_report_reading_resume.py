# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Persistence / application boundaries, with no local generation."""
import copy
import threading
import unittest
from test_report_prompt_workspace import PromptWorkspaceTests
from pocket_memory.report_workspace import ReportWorkspace, export_html
from pocket_memory.report_adaptive_view import render_adaptive


class ReadingResumeTests(unittest.TestCase):
    setUp=PromptWorkspaceTests.setUp
    tearDown=PromptWorkspaceTests.tearDown
    plan=PromptWorkspaceTests.plan
    def paused(self):
        r=self.plan()
        r['adaptive'].update(status='reading_paused',outline=[],chapters=[],issues=[],
            coverage=dict(completed_batches=2,total_batches=21),resume_reason='budget')
        r['generation']=dict(calls=2,seconds=5.0)
        self.ws.write(r);return r

    def test_paused_cannot_compose_export_or_rewrite_task(self):
        r=self.paused()
        with self.assertRaises(ValueError):self.ws.compose(r['id'],{'version':1})
        with self.assertRaises(ValueError):export_html(r)
        with self.assertRaises(ValueError):self.ws.save_edit(r['id'],{'version':1,'title':'改要求'})
        html=render_adaptive(r)
        self.assertIn('2 / 21',html);self.assertIn('继续阅读',html)
        self.assertNotIn('分章初稿',html)

    def test_resume_uses_saved_snapshot_never_new_library_or_client_evidence(self):
        r=self.paused();seen={}
        def build(*args,**kw):
            seen['units']=args[3];seen.update(kw)
            result=copy.deepcopy(r);result['adaptive']['status']='outline'
            result['generation']=dict(calls=3,seconds=7.5)
            return result
        self.ws.build=build
        job=self.ws.resume_reading(r['id'],{'version':1,'units':[{'body':'forged'}],'resume_reading':{'fake':True}})
        self.ws.worker.join(2)
        saved=self.ws.read(r['id'])
        self.assertEqual(self.ws.job(job['id'])['status'],'done')
        self.assertEqual(seen['units'],r['units']);self.assertEqual(seen['resume_reading'],r['adaptive'])
        self.assertEqual(saved['id'],r['id']);self.assertEqual(saved['version'],2)
        self.assertEqual(saved['generation']['calls'],5);self.assertEqual(saved['generation']['seconds'],12.5)
        self.assertEqual(saved['generation']['current_session_calls'],3)

    def test_each_checkpoint_survives_exception_and_workspace_recreation(self):
        r=self.paused()
        def build(*args,**kw):
            checkpoint=copy.deepcopy(r);checkpoint['adaptive']['coverage']['completed_batches']=3
            checkpoint['generation']=dict(calls=1,seconds=2)
            kw['on_checkpoint'](checkpoint)
            raise RuntimeError('interrupted after successful batch')
        self.ws.build=build
        job=self.ws.resume_reading(r['id'],{'version':1});self.ws.worker.join(2)
        recreated=ReportWorkspace(self.store,None,threading.RLock())
        saved=recreated.read(r['id'])
        self.assertEqual(saved['adaptive']['coverage']['completed_batches'],3)
        self.assertEqual(saved['adaptive']['status'],'reading_paused')
        self.assertEqual(saved['generation']['calls'],3)
        self.assertEqual(self.ws.job(job['id'])['status'],'error')
        self.assertIsNone(self.ws.composing_report)
        with self.assertRaises(ValueError):self.ws.resume_reading(r['id'],{'version':1})

    def test_initial_task_keeps_checkpoint_on_unexpected_failure(self):
        r=self.paused();self.ws.path(r['id']).unlink()
        def build(*args,**kw):
            kw['on_checkpoint'](copy.deepcopy(r))
            raise RuntimeError('interrupted initial reading')
        self.ws.build=build
        job=self.ws.create_job({'title':r['title'],'template':'auto','focus':'要求','phase':'outline',
            'sources':[{'kind':'note','id':self.root.id}]})
        self.ws.worker.join(2)
        self.assertEqual(self.ws.read(r['id'])['adaptive']['status'],'reading_paused')
        self.assertEqual(self.ws.job(job['id'])['report_id'],r['id'])
        self.assertIsNone(self.ws.composing_report)

    def test_reopening_active_snapshot_returns_existing_job_not_a_second_task(self):
        r=self.paused()
        self.ws.jobs['current']=dict(id='current',status='running',report_id=r['id'])
        self.assertEqual(self.ws.active_job(r['id'])['id'],'current')
        self.assertIsNone(self.ws.active_job('b'*32))
        self.ws.jobs['current']['status']='done'
        self.assertIsNone(self.ws.active_job(r['id']))


if __name__=='__main__':unittest.main()
