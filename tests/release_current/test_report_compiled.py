# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Source compilation contracts without a model or a report-quality proxy."""
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from pocket_memory import report_compiled as c


def unit(uid,body,source_id=1,field='content',tab='正文'):
    return dict(unit_id=uid,kind='note',source_id=source_id,note_id=source_id,title=f'资料{source_id}',
        tab=tab,field=field,body=body,revision=hashlib.sha256(body.encode()).hexdigest())


def sources():
    return [unit('n1:content','适用范围：人工核对后才能执行。\n'+''.join(f'第{i}项说明：应保留来源、条件与例外，未获批准不得执行。' for i in range(80))),
        unit('n2:content','操作清单：\n- 核对来源。\n- 由责任人确认。\n\n| 名称 | 状态 |\n| --- | --- |\n| 甲 | 已验证 |\n| 乙 | 规划 |',2),
        unit('n2:ocr','甲 已验证\n乙 规划\nOCR额外识别字符不得用于猜测同义事实。',2,'ocr_text','识别文字')]


class Registry:
    def __init__(self,start=0):self.items=[];self.start=start
    def __call__(self,unit,start,end):
        key=(unit['unit_id'],start,end)
        if key not in self.items:self.items.append(key)
        return self.start+self.items.index(key)+1


class Engine:
    n_ctx=3072
    def __init__(self,path,mode='valid'):
        self.model_path=path;self.mode=mode;self.requests=[];self.sessions=0
    def session(self):self.sessions+=1;return nullcontext()
    def report_prompt_tokens(self,messages):return sum(len(m['content']) for m in messages)
    def report_completion(self,messages,max_tokens,schema=None):
        assert self.report_prompt_tokens(messages)+max_tokens+c.MARGIN<=self.n_ctx
        self.requests.append(dict(messages=messages,max_tokens=max_tokens,schema=schema))
        ids=list(schema['properties']['assignments']['properties'])
        if self.mode=='missing':ids=ids[:1]
        obj=dict(chapters=[dict(id='C1',title='准备资料与核对输出',purpose='围绕资料来源和责任边界开展核对练习。')],
            assignments={gid:'C1' for gid in ids})
        if self.mode=='empty':obj['chapters'].append(dict(id='C2',title='待选练习',purpose='围绕读者需要选择练习方式。'))
        if self.mode=='duplicate_chapter':obj['chapters'].append(copy.deepcopy(obj['chapters'][0]))
        if self.mode=='unknown_chapter':obj['assignments'][ids[0]]='C5'
        text=json.dumps(obj,ensure_ascii=False)
        if self.mode=='duplicate_assignment':text=text.replace('"G1": "C1"','"G1": "C1", "G1": "C2"')
        return dict(text=text if self.mode!='malformed' else text[:-2],
            finish_reason='length' if self.mode=='truncated' else 'stop',usage={'completion_tokens':100})


class SourceCompiledTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.model=self.path/'fixture.gguf';self.model.write_bytes(b'fixture')
        self.focus='为业务同事编排培训资料，保留完整原文与表格。'
    def generate(self,engine=None,source=None,register=None,**kwargs):
        return c.generate(engine or Engine(self.model),'业务培训',kwargs.pop('focus',self.focus),source or sources(),
            register or Registry(),cache_dir=self.path/'cache',**kwargs)

    def test_single_organization_call_compiles_all_sources_without_claiming_full_model_reading(self):
        engine=Engine(self.model);report,claims,receipt=self.generate(engine)
        self.assertEqual(len(engine.requests),1);self.assertEqual(receipt['calls'],1)
        self.assertEqual(engine.requests[0]['max_tokens'],480)
        self.assertEqual(report['status'],'complete');self.assertEqual(report['reading_mode'],'source_compiled_v1')
        cover=report['coverage']
        self.assertEqual(cover['compiled_facts'],cover['total_facts'])
        self.assertEqual(cover['source_scan_nonspace_chars'],cover['total_nonspace_chars'])
        self.assertLess(cover['model_read_nonspace_chars'],cover['total_nonspace_chars'])
        self.assertFalse(cover['model_read_full_sources'])
        for p in report['model_preview_spans']:
            u=next(u for u in sources() if u['unit_id']==p['unit_id'])
            self.assertEqual(u['body'][p['start']:p['end']],p['quote'])
        self.assertTrue(claims)
        self.assertIn('待人工评审',report['review_status'])

    def test_tables_lists_ocr_and_adjacent_prose_remain_complete(self):
        report,_,_=self.generate();chapter=report['chapters'][0]
        table=chapter['tables'][0]
        self.assertEqual(table['columns'],['名称','状态'])
        self.assertEqual([r['cells'] for r in table['rows']],[['甲','已验证'],['乙','规划']])
        self.assertTrue(all(len(r['refs'])==2 and len(r['fact_ids'])==1 for r in table['rows']))
        self.assertTrue(any('操作清单' in p['text'] for p in chapter['paragraphs']))
        ocr=next(p for p in chapter['paragraphs'] if p['unit_id']=='n2:ocr')
        self.assertEqual(ocr['text'],sources()[2]['body']);self.assertEqual(ocr['placement'],'supplement')
        prose=[p for p in chapter['paragraphs'] if p['unit_id']=='n1:content']
        self.assertEqual(len(prose),1);self.assertEqual(prose[0]['text'],sources()[0]['body'])
        self.assertEqual(prose[0]['placement'],'supplement')
        self.assertTrue(all(g['kind']=='editorial' and g['text'].startswith('编写建议：') for g in chapter['guidance']))
        covered=[fid for item in chapter['paragraphs']+chapter['tables'] for fid in item['fact_ids']]
        self.assertCountEqual(covered,[f['id'] for f in report['facts']])

    def test_source_cache_is_prompt_independent_unbound_and_rebinds_current_citations(self):
        first,_,_=self.generate()
        engine=Engine(self.model);second,_,receipt=self.generate(engine,focus='整理项目资料',register=Registry(100))
        self.assertTrue(receipt['source_cache']['hit'])
        self.assertEqual(first['source_contract'],second['source_contract'])
        self.assertTrue(all(r>100 for f in second['facts'] for r in f['refs']))
        cache_files=list((self.path/'cache'/c.PROTOCOL/'sources').glob('*.json'))
        self.assertEqual(len(cache_files),1)
        self.assertNotIn('"refs"',cache_files[0].read_text('utf-8'))

    def test_source_cache_corruption_is_rebuilt_and_false_revision_is_rejected(self):
        self.generate()
        path=next((self.path/'cache'/c.PROTOCOL/'sources').glob('*.json'))
        saved=json.loads(path.read_text('utf-8'));saved['inventory']['facts'][0]['evidence'][0]['quote']='错误引句'
        path.write_text(json.dumps(saved,ensure_ascii=False),encoding='utf-8')
        report,_,receipt=self.generate()
        self.assertFalse(receipt['source_cache']['hit'])
        self.assertEqual(receipt['source_cache']['invalidated'],'source_cache_invalidated')
        self.assertNotEqual(report['facts'][0]['evidence'][0]['quote'],'错误引句')
        source=sources();source[0]['body']+='更改正文但保留旧revision'
        engine=Engine(self.model)
        with self.assertRaises(ValueError):self.generate(engine,source=source)
        self.assertEqual(engine.requests,[])

    def test_prepared_outline_compiles_with_zero_model_sessions_and_allows_valid_editing(self):
        prepared,_,_=self.generate(stop_after_outline=True)
        self.assertEqual(prepared['status'],'outline');self.assertEqual(prepared['chapters'],[])
        edited=copy.deepcopy(prepared['outline']);edited[0]['title']='人工调整的学习顺序'
        engine=Engine(self.model);report,_,receipt=self.generate(engine,prepared=prepared,outline_override=edited,register=Registry(50))
        self.assertEqual(engine.requests,[]);self.assertEqual(engine.sessions,0);self.assertEqual(receipt['calls'],0)
        self.assertEqual(report['chapters'][0]['title'],'人工调整的学习顺序')
        self.assertTrue(all(r>50 for f in report['facts'] for r in f['refs']))
        engine.n_ctx=4096
        with self.assertRaises(ValueError):self.generate(engine,prepared=prepared)
        bad=copy.deepcopy(prepared);bad['facts'][0]['text']='篡改事实不能通过'
        with self.assertRaises(ValueError):self.generate(prepared=bad)

    def test_cached_organization_does_not_start_model_and_corrupt_cache_is_not_reused(self):
        self.generate();engine=Engine(self.model);_,_,receipt=self.generate(engine)
        self.assertEqual(engine.sessions,0);self.assertEqual(receipt['cache_reused_calls'],1)
        path=next((self.path/'cache'/c.PROTOCOL/'organization').glob('*.json'))
        path.write_text('{broken',encoding='utf-8')
        engine=Engine(self.model);report,_,_=self.generate(engine)
        self.assertEqual(len(engine.requests),1);self.assertEqual(report['status'],'complete')
        self.assertTrue(any(i['stage']=='cache' for i in report['issues']))

    def test_runtime_threads_and_gpu_configuration_invalidate_saved_organization(self):
        initial=Engine(self.model);initial.n_threads=4;initial.n_gpu_layers=0
        with patch.object(c.importlib.metadata,'version',return_value='fixture-runtime-1'):
            prepared,_,_=self.generate(initial,stop_after_outline=True)
        self.assertEqual(prepared['model_contract']['runtime'],'fixture-runtime-1')
        self.assertEqual(prepared['model_contract']['chat_protocol'],c.CHAT_PROTOCOL)
        cases=[('threads',8,0,'fixture-runtime-1'),('gpu',4,12,'fixture-runtime-1'),('runtime',4,0,'fixture-runtime-2')]
        for name,threads,gpu,runtime in cases:
            with self.subTest(change=name),patch.object(c.importlib.metadata,'version',return_value=runtime):
                changed=Engine(self.model);changed.n_threads=threads;changed.n_gpu_layers=gpu
                with self.assertRaises(ValueError):self.generate(changed,prepared=prepared)
                self.assertEqual(changed.sessions,0);self.assertEqual(changed.requests,[])
                _,_,receipt=self.generate(changed)
                self.assertEqual(len(changed.requests),1)
                self.assertEqual(receipt['cache_reused_calls'],0)
                self.assertTrue(receipt['source_cache']['hit'])

    def test_missing_required_assignment_is_partial_and_preserves_all_originals(self):
        report,_,_=self.generate(Engine(self.model,'missing'))
        self.assertEqual(report['organization_status'],'needs_review')
        self.assertEqual(report['status'],'partial')
        self.assertEqual(report['outline_origin'],'source_fallback')
        self.assertEqual(report['coverage']['compiled_facts'],report['coverage']['total_facts'])

    def test_assignments_schema_has_one_required_key_per_source_and_rejects_ambiguous_identity(self):
        engine=Engine(self.model);self.generate(engine)
        assignments=engine.requests[0]['schema']['properties']['assignments']
        self.assertEqual(set(assignments['required']),{'G1','G2'})
        self.assertTrue(all(v['type']=='string' and v['enum']==['C1','C2','C3','C4','C5'] for v in assignments['properties'].values()))
        self.assertNotIn('group_ids',engine.requests[0]['schema']['properties']['chapters']['items']['properties'])
        for mode in ('duplicate_chapter','unknown_chapter','duplicate_assignment'):
            with self.subTest(mode=mode):
                report,_,_=self.generate(Engine(self.model,mode),focus=self.focus+mode)
                self.assertEqual(report['status'],'partial')
                self.assertEqual(report['organization_status'],'needs_review')
                self.assertEqual(report['coverage']['compiled_facts'],report['coverage']['total_facts'])
                self.assertEqual(len(report['trace']),1)

    def test_empty_chapter_is_removed_with_receipt_without_losing_source(self):
        report,_,_=self.generate(Engine(self.model,'empty'))
        self.assertEqual(report['status'],'complete')
        self.assertEqual([chapter['id'] for chapter in report['outline']],['C1'])
        note=next(note for note in report['issues'] if note.get('kind')=='empty_chapters')
        self.assertEqual(note['chapter_ids'],['C2'])
        self.assertEqual(report['coverage']['compiled_facts'],report['coverage']['total_facts'])

    def test_v1_saved_outline_remains_zero_generation_while_new_cache_contract_is_v2(self):
        prepared,_,_=self.generate(stop_after_outline=True)
        self.assertEqual(prepared['planning_contract']['protocol'],c.ORGANIZATION_PROTOCOL)
        legacy=copy.deepcopy(prepared)
        legacy['planning_contract']['protocol']=c.LEGACY_ORGANIZATION_PROTOCOL
        legacy['planning_contract']['model']['protocol']=c.LEGACY_ORGANIZATION_PROTOCOL
        legacy['model_contract']['protocol']=c.LEGACY_ORGANIZATION_PROTOCOL
        legacy['trace'][0]['response']['text']=json.dumps(dict(chapters=[dict(
            title='准备资料与核对输出',purpose='围绕资料来源和责任边界开展核对练习。',group_ids=['G1','G2'])]),ensure_ascii=False)
        self.assertNotEqual(c._hash(legacy['planning_contract']),c._hash(prepared['planning_contract']))
        engine=Engine(self.model);report,_,receipt=self.generate(engine,prepared=legacy)
        self.assertEqual(engine.requests,[]);self.assertEqual(engine.sessions,0);self.assertEqual(receipt['calls'],0)
        self.assertEqual(report['coverage']['compiled_facts'],report['coverage']['total_facts'])

    def test_truncated_or_malformed_output_is_partial_with_raw_trace_and_all_originals(self):
        for mode in ('truncated','malformed'):
            with self.subTest(mode=mode):
                report,_,receipt=self.generate(Engine(self.model,mode),focus=self.focus+mode)
                self.assertEqual(receipt['calls'],1);self.assertEqual(report['status'],'partial')
                self.assertEqual(report['state'],'organization_review')
                self.assertEqual(report['coverage']['compiled_facts'],report['coverage']['total_facts'])
                self.assertTrue(report['trace'][0]['response']['text'])
                self.assertEqual(report['outline_origin'],'source_fallback')
                with self.assertRaises(ValueError):self.generate(prepared=report,focus=self.focus+mode)


if __name__=='__main__':unittest.main()
