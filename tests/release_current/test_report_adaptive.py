# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import copy,json,tempfile,threading,unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from pocket_memory import report_adaptive as a
from pocket_memory.report_workspace import ReportWorkspace,digest,valid_ref,render_report,export_html

def response(obj,reason='stop'):return dict(text=json.dumps(obj,ensure_ascii=False),finish_reason=reason)
def unit(text,uid='n1:content',tab='正文',field='content'):
    return dict(unit_id=uid,kind='note',source_id=1,note_id=1,title='原记录',tab=tab,field=field,revision=digest(text),body=text)

class AdaptiveTests(unittest.TestCase):
    def test_lossless_chinese_long_table_ocr_and_tabs(self):
        units=[unit(('第一项：保留条件，未经批准不得启用。\n\n'*65)+'尾项：状态尚未完成。'),unit('| 功能 | 条件 |\n| --- | --- |\n| A | 仅试用 |','n2:content','页签2'),unit('图中文字：批准后方可执行；负责人未提供。','n2:ocr_text','页签2','ocr_text')]
        blocks=a.source_blocks(units)
        import re
        for u in units:
            chunks=[b for b in blocks if b['unit_id']==u['unit_id']]
            self.assertEqual(re.sub(r'\s','',''.join(b['text'] for b in chunks)),re.sub(r'\s','',u['body']))
            for b in chunks:self.assertEqual(u['body'][b['start']:b['end']],b['text'])
    def test_quote_must_belong_to_selected_page(self):
        blocks=a.source_blocks([unit('审批前不得启动服务，必须人工复核。')])
        obj={'facts':[{'topic':'启用条件','text':'启用服务前须经审批并由人工复核。','source_ids':['S1']}],'omitted':[]}
        facts,_=a._parse_read(response(obj),blocks)
        span=facts[0]['evidence_spans'][0]
        self.assertEqual(span['start'],0);self.assertEqual(span['quote'],blocks[0]['text'])
        bad=copy.deepcopy(obj);bad['facts'][0]['source_ids']=['S2']
        with self.assertRaises(ValueError):a._parse_read(response(bad),blocks)
        with self.assertRaises(ValueError):a._parse_read(response(obj,'length'),blocks)
    def test_table_row_reference_keeps_column_header(self):
        blocks=a.source_blocks([unit('| 名称 | 状态 |\n| --- | --- |\n| 服务A | 未批准 |')])
        row=next(b for b in blocks if b['kind']=='table_row')
        obj={'facts':[{'topic':'服务状态','text':'原文中服务A当前状态是未批准。','source_ids':[row['id']]}],'omitted':[]}
        facts,_=a._parse_read(response(obj),blocks)
        self.assertEqual(set(facts[0]['source_ids']),{row['id'],*row['required_ids']})
    def test_outline_membership_deterministic_and_title_slots_fixed(self):
        units=[unit('审批前不得启用，需人工审核。'),unit('另一来源的流程完全独立。','n2:content')]
        units[1]['source_id']=2
        blocks=a.source_blocks(units)
        facts=[{'id':'F1','source_ids':['S1'],'source_label':'来源一','topic':'启用条件'}, {'id':'F2','source_ids':['S2'],'source_label':'来源二','topic':'独立流程'}]
        groups=a.source_outline(facts,blocks,units)
        self.assertEqual([g['fact_ids'] for g in groups],[['F1'],['F2']])
        _,schema=a.chapter_title_request('资料报告',groups,facts)
        self.assertEqual(schema['required'],['C1','C2'])
    def test_bare_url_can_be_structurally_excluded_not_substantive_text(self):
        blocks=a.source_blocks([unit('审批前不得启动服务，必须人工复核。'),unit('https://example.org/ref','n2:content')])
        obj={'facts':[{'topic':'启用条件','text':'启用服务前须经审批并由人工复核。','source_ids':['S1']}],'omitted':[]}
        facts,omitted=a._parse_read(response(obj),blocks)
        self.assertTrue(omitted[0]['structural'])
        blocks[1]['text']='审批之前任何人均不得启动服务。'
        facts,omitted=a._parse_read(response(obj),blocks)
        self.assertEqual(len(facts),1);self.assertFalse(omitted[0]['structural'])
    def test_writer_missing_fact_is_exposed(self):
        facts=[dict(id='F1',refs=[1]),dict(id='F2',refs=[2])]
        paragraphs,missing=a._parse_written(response({'paragraphs':[{'text':'这段只归纳第一项的资料记录。','fact_ids':['F1']}]}),facts)
        self.assertEqual(missing,['F2']);self.assertEqual(paragraphs[0]['refs'],[1])
    def test_adaptive_render_save_export_full_snapshots(self):
        u=unit('审批前不得启动服务，必须人工复核。尾部补充仍须保留。')
        ref={k:v for k,v in u.items() if k!='body'}|dict(id=1,start=0,end=19,quote=u['body'][:19])
        self.assertTrue(valid_ref(ref,[u]))
        r=dict(id='a'*32,title='<script>x</script>',created='2026-11-17',updated='2026-11-17',version=1,selected=[{'title':'原记录'}],template='auto',units=[u],refs=[ref],notes={},generation={'calls':3,'seconds':1},limitations=[],adaptive={'status':'complete','coverage':{},'chapters':[{'id':'C1','title':'启用条件','status':'complete','paragraphs':[{'text':'服务启用前必须审批并人工复核。','refs':[1]}]}]})
        html=export_html(r);self.assertIn(ref['quote'],html);self.assertNotIn('尾部补充仍须保留。',html);self.assertIn('source-1',html);self.assertNotIn('<script>',html);self.assertNotIn('没有可按同一口径',html)
        with tempfile.TemporaryDirectory() as d:
            ws=ReportWorkspace(SimpleNamespace(data_dir=Path(d)),None,threading.RLock());ws.write(r)
            changed=ws.save_edit(r['id'],{'version':1,'notes':{'chapter-C1':'补充说明'}})
            self.assertEqual(ws.read(r['id'])['notes']['chapter-C1'],'补充说明')
            self.assertEqual(ws.read(r['id'])['units'],[u])
            self.assertIn('补充说明',render_report(changed))
    def test_readable_quote_preserves_raw_source_offsets(self):
        raw='<span style="color:red">审批前不得启用。</span>'
        u=unit(raw);ref={k:v for k,v in u.items() if k!='body'}|dict(id=1,start=0,end=len(raw),quote=raw)
        ws=ReportWorkspace(SimpleNamespace(get_note=lambda _:SimpleNamespace(content=raw)),None,threading.RLock())
        ws.read=lambda _:dict(refs=[ref],units=[u])
        d=ws.source('a'*32,1)
        self.assertEqual(d['quote_text'],'审批前不得启用。')
        self.assertEqual(d['ref']['quote'],raw);self.assertEqual(d['snapshot'],raw)
        self.assertFalse(d['changed']);self.assertTrue(valid_ref(d['ref'],[u]))

    def test_input_limit_rejects_before_generation(self):
        with self.assertRaises(ValueError):a.source_blocks([unit('长'*(a.MAX_CHARS+1))])
    def test_auto_writer_does_not_block_note_mutation_lock(self):
        entered=threading.Event();release=threading.Event();lock=threading.RLock()
        ws=ReportWorkspace(None,None,lock);ws.jobs['job']={'started':0}
        def build(*args,**kwargs):
            entered.set();release.wait(2);return {'id':'a'*32,'adaptive':{'status':'complete'}}
        ws.build=build;ws.write=lambda _:None
        worker=threading.Thread(target=ws._run,args=('job','title','auto',[],[]));worker.start()
        try:
            self.assertTrue(entered.wait(1))
            acquired=lock.acquire(blocking=False);self.assertTrue(acquired)
            if acquired:lock.release()
        finally:release.set();worker.join(2)
        self.assertEqual(ws.jobs['job']['status'],'done')
    def test_library_switch_rejected_before_side_effects_during_report(self):
        from pocket_memory.server import PocketMemoryServer
        server=SimpleNamespace(report_workspace=SimpleNamespace(worker=SimpleNamespace(is_alive=lambda:True)))
        with self.assertRaisesRegex(ValueError,'资料汇编'):
            PocketMemoryServer._prepare_library_change(server)
    def test_control_id_display_only_and_real_field_names_retained(self):
        from pocket_memory.report_adaptive_view import display_paragraph
        p={'text':'资料中的启用条件已经明确。F1,F2','fact_ids':['F1','F2']}
        self.assertEqual(display_paragraph(p,[unit('审批前不得启用。')]),'资料中的启用条件已经明确。')
        self.assertEqual(p['text'],'资料中的启用条件已经明确。F1,F2')
        self.assertEqual(display_paragraph(p,[unit('字段为F1,F2。')]),p['text'])

if __name__=='__main__':unittest.main()
