# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import copy
import json
import unittest
import re
import tempfile
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from pocket_memory.report_digest import source_spans, make_plan, parse_map, applicable, messages_for, OUTPUT_TOKENS, MARGIN


class WholeReportTests(unittest.TestCase):
    def unit(self, body, id='d1:1'):
        return dict(unit_id=id, body=body, title='真实资料', tab='第1页', field='document_text',
                    source_id=1, kind='document', revision='test')

    def test_full_source_coverage_not_page_heads(self):
        units=[self.unit(('开头。重要条件：除非原文另有说明，不可扩大范围。'*50)+'末尾条件。',f'd1:{i}') for i in range(6)]
        spans, omitted=source_spans(units)
        self.assertFalse(omitted)
        for u in units:
            marked=set()
            for s in spans:
                if s['unit_id']==u['unit_id']:
                    self.assertEqual(s['text'],u['body'][s['start']:s['end']])
                    marked.update(range(s['start'],s['end']))
            self.assertFalse([i for i,c in enumerate(u['body']) if not c.isspace() and i not in marked])

    def test_watermark_requires_same_document_recurrence(self):
        footer='\n某作者知识分享\n知识分享\n某作者知识分享\n某作者'
        units=[self.unit('必须保留这一项限制：仅在条件满足时适用。'+footer,f'd1:{i}') for i in range(4)]
        spans,omitted=source_spans(units)
        self.assertEqual(len(omitted),4)
        self.assertTrue(all('仅在条件满足时' in s['text'] for s in spans))
        single=source_spans([units[0]])
        self.assertFalse(single[1])

    def test_exact_budget_and_each_span_planned_once(self):
        units=[self.unit('测试条件不可删除。'*100,f'd1:{i}') for i in range(8)]
        spans,_=source_spans(units)
        engine=SimpleNamespace(n_ctx=3072,report_prompt_tokens=lambda m:sum(len(x['content']) for x in m))
        plan=make_plan(engine,'报告','学习',spans)
        self.assertEqual([s['id'] for b in plan for s in b['spans']],[s['id'] for s in spans])
        self.assertTrue(all(b['prompt_tokens']+OUTPUT_TOKENS+MARGIN<=3072 for b in plan))

    def test_invented_id_and_truncation_not_accepted(self):
        spans,_=source_spans([self.unit('仅在资料中的条件满足时使用该机制。')])
        obj=dict(heading='要点',points=[dict(topic='条件',text='该机制只在资料中规定的条件满足时使用。',sources=['S1'])])
        response=dict(finish_reason='stop',text=json.dumps(obj))
        self.assertEqual(len(parse_map(response,{'spans':spans})['points']),1)
        with self.assertRaises(ValueError):parse_map(dict(response,finish_reason='length'),{'spans':spans})
        obj['points'][0]['sources']=['S999']
        result=parse_map(dict(response,text=json.dumps(obj)),{'spans':spans})
        self.assertEqual(result['points'],[])
        self.assertEqual(len(result['rejected']),1)

    def test_existing_short_notes_remain_old_route(self):
        self.assertFalse(applicable([self.unit('短资料。')]))
        u=self.unit('长资料。'*2000);u['kind']='note'
        self.assertFalse(applicable([u]))

    def test_directory_cannot_duplicate_or_lose_knowledge_entries(self):
        from pocket_memory.report_digest import parse_directory
        entries=[dict(id=i) for i in (2,6,11)]
        response=dict(finish_reason='stop',text=json.dumps(dict(titles=['并发','数据库'],assignments=[1,2,1])))
        result=parse_directory(response,entries)
        self.assertEqual(result,[dict(title='并发',batch_ids=[2,11]),dict(title='数据库',batch_ids=[6])])
        for invalid in ([1,2], [1,2,3], [1,True,2]):
            with self.assertRaises(ValueError):
                parse_directory(dict(response,text=json.dumps(dict(titles=['并发','数据库'],assignments=invalid))),entries)
        with self.assertRaises(ValueError):
            parse_directory(dict(response,text=json.dumps(dict(titles=['甲','乙'],assignments=[1]+[2]*29))),
                            [dict(id=i) for i in range(30)])

    def test_rejected_batch_cannot_claim_complete_and_can_resume(self):
        from pocket_memory.report_digest import generate
        with tempfile.TemporaryDirectory() as folder:
            model=Path(folder)/'model.gguf';model.write_bytes(b'fixture-only')
            calls=[]
            def completion(messages,budget):
                calls.append(messages)
                points=([dict(topic='无效引用',text='这一项没有合法引用，必须显示未完成。',sources=['S999'])]
                        if len(calls)==1 else [])
                return dict(text=json.dumps(dict(heading='条件',points=points)),finish_reason='stop',usage={},seconds=0)
            engine=SimpleNamespace(n_ctx=3072,model_path=model,session=lambda:nullcontext(),
                       report_prompt_tokens=lambda m:sum(len(x['content']) for x in m)//3,
                       report_completion=completion)
            units=[self.unit('原文明确规定只有条件满足才执行。')]
            first,_,_=generate(engine,'报告','学习',units,lambda *args:1,Path(folder)/'cache',lambda _:None)
            self.assertEqual(first['coverage']['read_units'],1)
            self.assertEqual(first['coverage']['completed_batches'],0)
            self.assertEqual(first['map_batches'][0]['status'],'incomplete')
            second,_,_=generate(engine,'报告','学习',units,lambda *args:1,Path(folder)/'cache',lambda _:None)
            self.assertEqual(len(calls),2)
            self.assertEqual(second['coverage']['completed_batches'],1)
            self.assertEqual(second['map_batches'][0]['status'],'no_points')
            self.assertEqual(len(list((Path(folder)/'cache').glob('failure-*.json'))),1)

    def test_render_real_paraphrase_all_refs_and_no_duplicate_heading(self):
        from pocket_memory.report_workspace import make_ref,digest,render_report,export_html
        u=self.unit('原文说明：仅在条件满足时使用该机制，失败后保留原记录。');u['revision']=digest(u['body']);u['note_id']=None
        ref=make_ref(u,0,len(u['body']));ref['id']=1
        point=dict(text='这项机制有使用前提：条件满足才执行，失败时保留原记录。',topic='使用边界',refs=[1])
        batch=dict(id=1,heading='使用边界',points=[point],status='complete')
        r=dict(title='学习报告',template='learning',template_label='学习总结',created='2026-11-15',version=1,
               selected=[{}],units=[u],refs=[ref],notes={},limitations=[],generation={'seconds':0},
               digest=dict(protocol='whole-document-v1',batches=[batch],map_batches=[batch],
                           sections=[dict(title='使用边界',batch_ids=[1])],
                           coverage=dict(total_units=1,read_units=1,processed_units=1,total_batches=1,completed_batches=1)))
        before=copy.deepcopy(r);out=export_html(r)
        for rendered in (render_report(r), out):
            self.assertIn('<ul class="report-toc">',rendered)
            self.assertIn('<details class="panel full-source">',rendered)
            self.assertNotIn('<details class="panel full-source" open>',rendered)
            self.assertIn('<blockquote style="white-space:pre-wrap">'+u['body']+'</blockquote>',rendered)
        self.assertIn(point['text'],out)
        self.assertEqual(r,before)
        self.assertEqual(len(re.findall('id="source-1"',out)),1)
        self.assertNotIn('<h3>使用边界</h3>',out)
        self.assertIn('href="#source-1"',out)
        self.assertNotIn('src="http',out)
        r['refs'][0]['quote']='篡改'
        with self.assertRaises(ValueError):render_report(r)

    def test_long_digest_directory_and_folded_sources_keep_every_point_and_citation(self):
        from pocket_memory.report_workspace import make_ref,digest,render_report,export_html
        u=self.unit('同一原文的条件和依据必须保留。');u['revision']=digest(u['body']);u['note_id']=None
        ref=make_ref(u,0,len(u['body']));ref['id']=1
        batches=[dict(id=i,heading=f'知识主题{i}',points=[dict(text=f'第{i}条原有归纳条件。',refs=[1])],status='complete') for i in range(1,14)]
        r=dict(title='长报告',template='learning',template_label='学习总结',created='2026-11-17',version=1,
               selected=[{}],units=[u],refs=[ref],notes={},limitations=[],generation={'seconds':0},
               digest=dict(protocol='whole-document-v1',batches=batches,map_batches=batches,
                           sections=[dict(title=b['heading'],batch_ids=[b['id']]) for b in batches],
                           coverage=dict(total_units=1,read_units=1,processed_units=1,total_batches=13,completed_batches=13)))
        before=copy.deepcopy(r)
        for rendered in (render_report(r), export_html(r)):
            self.assertIn('<ul class="report-toc">',rendered)
            for chapter in range(1,14):
                self.assertEqual(rendered.count(f'href="#section-digest-{chapter}"'),1)
            self.assertIn('<details class="panel full-source">',rendered)
            self.assertNotIn('<details class="panel full-source" open>',rendered)
            self.assertEqual(rendered.count('data-ref="1"'),13)
            self.assertEqual(rendered.count('id="source-1"'),1)
            for b in batches:
                self.assertEqual(rendered.count(b['points'][0]['text']),1)
        self.assertEqual(r,before)


if __name__=='__main__':unittest.main()
