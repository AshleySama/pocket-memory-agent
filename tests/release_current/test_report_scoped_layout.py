# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pocket_memory.report_workspace import ReportWorkspace, digest, make_ref, render_report
from pocket_memory.report_reading_view import text_layout, attach_compiled_layout


def unit(uid, body, source=1):
    return dict(unit_id=uid, body=body, revision=digest(body), kind='document',
                source_id=source, field='document_text', note_id=None, title='资料', tab='正文')


def report(us, spans, original):
    refs=[];paras=[]
    for uid,a,b in spans:
        u=next(u for u in us if u['unit_id']==uid)
        ref=make_ref(u,a,b);ref['id']=len(refs)+1;refs.append(ref)
        paras.append(dict(text=u['body'][a:b],refs=[ref['id']],unit_id=uid,source_start=a,source_end=b))
    return dict(id='a'*32,title='测试',units=us,refs=refs,notes={},selected=[dict(kind='document',id=1)],
        adaptive=dict(status='complete',reading_mode='source_brief_v2',
            reading_layout=text_layout(original,us),chapters=[dict(id='C1',title='正文',paragraphs=paras,tables=[],
                content_order=[dict(kind='paragraph',index=i) for i in range(len(paras))])]))


class ScopedLayoutTests(unittest.TestCase):
    def test_whitespace_cut_joins_without_changing_evidence(self):
        u=unit('a','需审\n批后执行。');r=report([u],[('a',0,2),('a',3,len(u['body']))],'需审批后执行。')
        before=copy.deepcopy(r);h=render_report(r)
        self.assertIn('需审批后执行。',h);self.assertNotIn('<br',h)
        self.assertEqual(r,before);self.assertEqual(h.count('data-ref='),2)

    def test_excluded_gap_never_restored_or_joined(self):
        u=unit('a','甲\n保密条款\n乙')
        # Use the real last-character range, independent of the gap's size.
        r=report([u],[('a',0,1),('a',len(u['body'])-1,len(u['body']))],u['body'])
        h=render_report(r)
        self.assertNotIn('保密条款',h);self.assertEqual(h.count('compiled-excerpt'),2)

    def test_cross_unit_requires_verified_continuation_and_same_source(self):
        us=[unit('a','审批'),unit('b','后执行。')]
        r=report(us,[('a',0,2),('b',0,4)],'审批后执行。')
        self.assertIn('审批后执行。',render_report(r))
        r['adaptive']['reading_layout']['continuations']=[]
        self.assertEqual(render_report(r).count('compiled-excerpt'),2)
        r=report([us[0],unit('b','后执行。',2)],[('a',0,2),('b',0,4)],'审批后执行。')
        self.assertEqual(render_report(r).count('compiled-excerpt'),2)

    def test_modified_text_or_incomplete_citation_does_not_authorize_projection(self):
        u=unit('a','必须审\n批');r=report([u],[('a',0,len(u['body']))],'必须审批')
        r['adaptive']['chapters'][0]['paragraphs'][0]['text']='人工审\n核'
        self.assertIn('人工审<br>',render_report(r));self.assertNotIn('必须审批',render_report(r))
        r=report([u],[('a',0,2)],'必须审批')
        r['adaptive']['chapters'][0]['paragraphs'][0].update(text=u['body'],source_end=len(u['body']))
        self.assertIn('<br',render_report(r))

    def test_table_interrupts_join_and_keeps_rows(self):
        u=unit('a','审批后执行。');r=report([u],[('a',0,2),('a',2,len(u['body']))],u['body'])
        c=r['adaptive']['chapters'][0]
        c['tables']=[dict(columns=['条件'],rows=[dict(cells=['审批'],refs=[1])])]
        c['content_order'].insert(1,dict(kind='table',index=0))
        h=render_report(r)
        self.assertEqual(h.count('compiled-excerpt'),2);self.assertIn('<th scope="col">条件</th>',h)
        self.assertNotIn('审批后执行。',h)

    def test_out_of_order_overlap_and_stale_layout_stay_separate(self):
        u=unit('a','甲乙丙丁');r=report([u],[('a',2,4),('a',0,3)],u['body'])
        self.assertEqual(render_report(r).count('compiled-excerpt'),2)
        u=unit('a','甲\n乙');r=report([u],[('a',0,3)],'甲乙')
        r['adaptive']['reading_layout']['units'][0]['revision']='stale'
        self.assertIn('<br',render_report(r))

    def test_saved_read_is_projection_only_and_full_document_edits_untouched(self):
        u=unit('a','审批\n通过。');r=report([u],[('a',0,len(u['body']))],'审批通过。')
        layout=r['adaptive'].pop('reading_layout')
        with tempfile.TemporaryDirectory() as d:
            store=SimpleNamespace(data_dir=Path(d));w=ReportWorkspace(store,None,threading.RLock());w.write(r)
            before=w.path(r['id']).read_bytes()
            with patch('pocket_memory.report_reading_view.collect',return_value=layout) as collect:
                loaded=w.read(r['id']);self.assertIn('reading_layout',loaded['adaptive'])
                self.assertEqual(w.path(r['id']).read_bytes(),before)
                edited=copy.deepcopy(r);edited['document_html']='<p>手工内容</p>'
                attach_compiled_layout(edited,store);self.assertNotIn('reading_layout',edited['adaptive'])
                self.assertEqual(collect.call_count,1)

    def test_new_scoped_build_uses_common_layout_step(self):
        u=unit('a','审批\n通过。');r=report([u],[('a',0,len(u['body']))],'审批通过。')
        layout=r['adaptive'].pop('reading_layout')
        def generate(engine,title,focus,units,register,*args,**kwargs):
            register(units[0],0,len(units[0]['body']))
            return copy.deepcopy(r['adaptive']),[],dict(calls=0,seconds=0)
        with tempfile.TemporaryDirectory() as d:
            w=ReportWorkspace(SimpleNamespace(data_dir=Path(d)),None,threading.RLock())
            with patch('pocket_memory.report_brief.generate',side_effect=generate), patch(
                    'pocket_memory.report_reading_view.collect',return_value=layout):
                result=w.build('测试','auto',r['selected'],[u],focus='只整理审批内容',composition_mode='source_compiled')
            self.assertIn('审批通过。',render_report(result))
            self.assertEqual(result['units'],[u]);self.assertEqual(result['refs'],r['refs'])


if __name__=='__main__':unittest.main()
