# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Complete-source routing and exact provenance; no model required."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pocket_memory import report_complete
from pocket_memory.storage import NoteStore
from pocket_memory.report_workspace import ReportWorkspace, digest, valid_ref, render_report, export_html


def unit(uid, body, sid=1, kind='document', field='document_text', tab='正文'):
    return dict(unit_id=uid, kind=kind, source_id=sid, note_id=sid if kind=='note' else None,
                title='办公资料'+str(sid), tab=tab, field=field, revision=digest(body), body=body)


class ForbiddenEngine:
    def __getattr__(self, key):
        raise AssertionError('Complete-source compilation must not load or call the model: '+key)


class CompleteSourceTests(unittest.TestCase):
    def assert_covered(self, units, result):
        spans=[s for c in result['chapters'] for s in c['segments']]+result['omissions']
        for u in units:
            owned=[0]*len(u['body'])
            for s in spans:
                if s['unit_id']!=u['unit_id']:continue
                self.assertEqual(s['text'],u['body'][s['start']:s['end']])
                for i in range(s['start'],s['end']):owned[i]+=1
            for i,char in enumerate(u['body']):
                self.assertLessEqual(owned[i],1)
                if not char.isspace():self.assertEqual(owned[i],1)

    def test_multiple_sources_plain_text_and_conditions_never_need_model_selection(self):
        units=[unit('d1','条件：审批通过后才可执行。\n金额：12.5万元。'),
               unit('d2','另一份记录：审批通过，执行未开始。',sid=2)]
        before=copy.deepcopy(units);p=report_complete.plan(units)
        self.assertEqual(units,before);self.assertEqual(len(p['chapters']),2)
        self.assert_covered(units,p)

    def test_headings_are_read_independently_not_across_note_tabs_or_ocr(self):
        units=[unit('n1','## 第一项\n记录甲。\n## 第二项\n未批准不得执行。',kind='note',field='content',tab='方案'),
               unit('n2','此页不应接到上一个页签章节。',kind='note',field='content',tab='执行'),
               unit('n2ocr','识别记录：执行状态未知。',kind='note',field='ocr_text',tab='执行')]
        p=report_complete.plan(units)
        self.assert_covered(units,p)
        self.assertEqual(len(p['chapters']),4)
        self.assertIn('图片识别原文',p['chapters'][-1]['title'])
        for chapter in p['chapters']:
            self.assertEqual(len({s['unit_id'] for s in chapter['segments']}),1)

    def test_document_page_continuation_and_original_heading_behavior_remain(self):
        units=[unit('d1','# 手册\n## 1.\n准备\n准备原件。\n'),
               unit('d2','# 手册\n> 未批准不得提交。\n## 2. 核对\n核对原文。\n')]
        p=report_complete.plan(units)
        self.assert_covered(units,p)
        self.assertEqual([c['title'] for c in p['chapters']],['1. 准备','2. 核对'])
        self.assertIn('未批准不得提交',''.join(s['text'] for s in p['chapters'][0]['segments']))

    def test_focus_does_not_enter_whole_source_path(self):
        units=[unit('d1','全部原文')]
        for focus in ['只要已审批且未执行的项','包含甲或乙但排除丙','总结比较两种方案']:
            self.assertIsNone(report_complete.plan(units,focus))
        self.assertIsNotNone(report_complete.plan(units,' \t'))

    def test_duplicate_identity_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'身份'):
            report_complete.plan([unit('d1','甲'),unit('d1','乙',sid=2)])

    def test_standalone_links_are_not_silently_removed_from_complete_content(self):
        units=[unit('n1','## 材料\n需要以下文件。\n[[审批单]] [[合同]]\n## 步骤\n先核对。',kind='note',field='content')]
        p=report_complete.plan(units)
        self.assertIn('[[审批单]] [[合同]]',''.join(s['text'] for c in p['chapters'] for s in c['segments']))
        self.assertEqual(p['omissions'],[])
        self.assert_covered(units,p)


class CompleteWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=NoteStore(Path(self.tmp.name));self.addCleanup(self.store.close)
        self.ws=ReportWorkspace(self.store,ForbiddenEngine(),threading.RLock())
        self.units=[unit('a','## 方案甲\n条件：需书面批准。\n## 方案乙\n费用：1.5万元。'),
                    unit('b','第二来源必须保留，执行尚未开始。',sid=2)]
        self.selected=[dict(kind='document',id=i,title='资料'+str(i)) for i in (1,2)]

    def test_title_is_not_a_filter_and_source_identity_survives_save_export(self):
        before=copy.deepcopy(self.units)
        r=self.ws.build('只要方案甲','auto',self.selected,self.units,composition_mode='source_compiled')
        self.assertEqual(r['generation']['calls'],0)
        self.assertEqual(r['adaptive']['status'],'complete')
        self.assertEqual(r['units'],before)
        self.assertTrue(all(valid_ref(ref,r['units']) for ref in r['refs']))
        self.assertEqual({ref['source_id'] for ref in r['refs']},{1,2})
        self.ws.write(r);saved=self.ws.read(r['id'])
        for text in ('需书面批准','1.5万元','第二来源必须保留'):
            self.assertIn(text,render_report(saved));self.assertIn(text,export_html(saved))
        self.assertNotIn('>[' ,render_report(saved).split('report-source-appendix')[0])
        self.ws.save_edit(r['id'],dict(version=1,notes={'chapter-C1':'人工核对原件'}))
        reopened=self.ws.read(r['id'])
        self.assertEqual(reopened['refs'],r['refs']);self.assertEqual(reopened['units'],before)
        self.assertIn('人工核对原件',export_html(reopened))

    def test_explicit_range_uses_unchanged_selector_even_with_model_disabled(self):
        sentinel=({'status':'partial','issues':[]},[],{'calls':0})
        with patch('pocket_memory.report_brief.generate',return_value=sentinel) as select:
            r=self.ws.build('资料汇编','auto',self.selected,self.units,focus='仅方案甲',composition_mode='source_compiled')
        select.assert_called_once()
        self.assertEqual(select.call_args.args[2],'仅方案甲')
        self.assertEqual(select.call_args.args[3],self.units)
        self.assertEqual(r['adaptive']['status'],'partial')

    def test_model_written_path_not_bypassed(self):
        with patch('pocket_memory.report_adaptive.generate',return_value=({'status':'partial'},[],{'calls':0})) as write:
            self.ws.build('文稿','auto',self.selected,self.units,composition_mode='model_written')
        write.assert_called_once()


if __name__=='__main__':unittest.main()
