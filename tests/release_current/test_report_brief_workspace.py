# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""New report selection routing, persistence, and minimal offline evidence."""
import tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
from pocket_memory.storage import NoteStore
from pocket_memory.report_workspace import ReportWorkspace, export_html, render_report


class BriefWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=NoteStore(Path(self.tmp.name));self.addCleanup(self.store.close)
        self.ws=ReportWorkspace(self.store,None,threading.RLock())
        self.note=self.store.create_note('办理规则','未获人工批准不得执行。\n\n与培训无关的完整文件尾部。',ai_status='not_requested')
        self.selected,self.units=self.ws.snapshot([{'kind':'note','id':self.note.id}])

    def fixture(self,engine,title,focus,units,register,*args,**kwargs):
        u=units[0];chosen='未获人工批准不得执行。'
        first=register(u,0,len(chosen));register(u,len(chosen)+2,len(u['body']))
        fact=dict(id='F1',text=chosen,refs=[first])
        adaptive=dict(reading_mode='source_brief_v2',status='complete',facts=[fact],groups=[],
            selection=dict(included_fact_ids=['F1'],excluded=[dict(fact_id='F2',reason='not_selected')]),
            coverage=dict(total_blocks=2,source_scan_blocks=2),issues=[],chapters=[dict(
                id='C1',title='执行边界',purpose='',guidance=[],tables=[],
                paragraphs=[dict(text=chosen,refs=[first],fact_ids=['F1'],placement='main')],
                content_order=[dict(kind='paragraph',index=0)])])
        return adaptive,[dict(text=chosen,ref=first)],dict(calls=1,seconds=2)

    def build(self):
        with patch('pocket_memory.report_brief.generate',side_effect=self.fixture) as brief,patch('pocket_memory.report_compiled.generate') as legacy:
            report=self.ws.build('培训要点','auto',self.selected,self.units,focus='说明执行条件',composition_mode='source_compiled')
        brief.assert_called_once();legacy.assert_not_called()
        return report

    def test_default_fast_job_selects_while_original_snapshot_stays_complete(self):
        report=self.build()
        self.assertEqual(report['units'],self.units)
        self.assertEqual(len(report['refs']),2)
        self.assertEqual(report['focus'],'说明执行条件')
        self.assertIn('未获人工批准不得执行',render_report(report))

    def test_export_contains_only_cited_evidence_not_full_sources_or_unused_references(self):
        report=self.build();output=export_html(report)
        self.assertIn('href="#source-1"',output);self.assertIn('id="source-1"',output)
        self.assertNotIn('id="source-2"',output)
        self.assertNotIn('与培训无关的完整文件尾部',output)
        self.assertNotIn('背景与补充阅读',output)
        self.assertNotIn('完整快照</summary>',output)
        self.assertIn('未获人工批准不得执行',output)

    def test_saved_notes_and_full_snapshot_remain_available_after_edit(self):
        report=self.build();self.ws.write(report)
        self.ws.save_edit(report['id'],dict(version=1,notes={'chapter-C1':'培训时让学员核对批准记录'}))
        reopened=self.ws.read(report['id'])
        self.assertEqual(reopened['units'],self.units)
        self.assertIn('培训时让学员核对批准记录',export_html(reopened))
        self.assertNotIn('与培训无关的完整文件尾部',export_html(reopened))

    def test_legacy_prepared_compilation_retains_its_generator(self):
        with patch('pocket_memory.report_compiled.generate',side_effect=self.fixture) as old,patch('pocket_memory.report_brief.generate') as new:
            self.ws.build('旧提纲','auto',self.selected,self.units,prepared={'reading_mode':'source_compiled_v1'})
        old.assert_called_once();new.assert_not_called()

    def test_export_rejects_invalid_selected_reference(self):
        report=self.build();report['adaptive']['chapters'][0]['paragraphs'][0]['refs']=[999]
        with self.assertRaises(ValueError):export_html(report)

    def test_empty_failure_is_not_exported_as_a_complete_document(self):
        report=self.build();report['adaptive'].update(status='partial',chapters=[])
        with self.assertRaisesRegex(ValueError,'文稿尚未生成'):export_html(report)
