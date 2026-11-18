# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Prompt/outline editing contracts; all completions are explicit test doubles."""
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from pocket_memory import report_adaptive as a


def unit(text,number):
    return dict(unit_id=f'n{number}:content',kind='note',source_id=number,note_id=number,
                title=f'资料{number}',tab='页签1',field='content',body=text,
                revision=hashlib.sha256(text.encode()).hexdigest())


class Engine:
    n_ctx=10000
    def __init__(self,path,responses):
        self.model_path=path
        self._llm=SimpleNamespace(metadata={'tokenizer.chat_template':'fixture-template'})
        self.responses=list(responses);self.requests=[]
    def session(self):return nullcontext()
    def report_prompt_tokens(self,messages):return 100
    def report_completion(self,messages,max_tokens,schema=None):
        self.requests.append(dict(messages=copy.deepcopy(messages),schema=copy.deepcopy(schema),max_tokens=max_tokens))
        if not self.responses:raise AssertionError('Unexpected extra generation')
        return dict(text=json.dumps(self.responses.pop(0),ensure_ascii=False),finish_reason='stop',seconds=0)


class Registry:
    def __init__(self,start=1):self.items=[];self.start=start
    def __call__(self,u,start,end):
        key=(u['unit_id'],start,end)
        if key not in self.items:self.items.append(key)
        return self.start+self.items.index(key)


class ReportPromptTests(unittest.TestCase):
    def setUp(self):
        self.units=[unit('方案甲仅在人工批准后启用，未批准前不得启动。',1),
                    unit('方案乙要求保留原始资料，核对结果后再执行。',2)]
        self.focus='为业务同事比较两种方案，重点说明启用条件。\n保留限制，不写营销套话。'
        self.reading={'facts':[
            {'topic':'甲方案启用','text':'方案甲启用前需要人工批准，未经批准不得启动。','source_ids':['S1']},
            {'topic':'乙方案核对','text':'方案乙需要保留原始资料，核对后才能执行。','source_ids':['S2']}],
            'omitted':[]}
        self.atomic_readings=[dict(facts=[{k:f[k] for k in ('topic','text')}],omitted_reason='') for f in self.reading['facts']]
        self.plan={'chapters':[{'title':'两种方案的执行条件','purpose':'比较两种方案进入执行环节前的条件。','fact_ids':['F1','F2']}]}
        self.written={'paragraphs':[
            {'text':'方案甲须先经人工批准，不能在批准前启用。','fact_ids':['F1']},
            {'text':'方案乙须保留原始资料，在完成核对后执行。','fact_ids':['F2']}]}

    def test_prepare_persist_edit_compose_reuses_reading_and_preserves_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            model=Path(directory)/'model.gguf';model.write_bytes(b'fixture')
            engine=Engine(model,[*self.atomic_readings,self.plan,self.written]);registry=Registry()
            prepared,claims,receipt=a.generate(engine,'方案对照',self.focus,self.units,registry,stop_after_outline=True)
            self.assertEqual(prepared['status'],'outline');self.assertEqual(prepared['state'],'outline_ready')
            self.assertEqual(claims,[]);self.assertEqual(receipt['calls'],3)
            self.assertEqual(prepared['outline'][0]['fact_ids'],['F1','F2'])
            self.assertEqual(prepared['outline_origin'],'task')
            self.assertEqual(len(engine.requests),3)
            for request in engine.requests:
                self.assertIn(self.focus,request['messages'][-1]['content'])
            enum=engine.requests[2]['schema']['properties']['chapters']['items']['properties']['fact_ids']['items']['enum']
            self.assertEqual(enum,['F1','F2'])
            persisted=json.loads(json.dumps(prepared,ensure_ascii=False))
            override=[{'id':'C1763377200000','title':'启动前逐项核对','purpose':'供现场人员核对，不增加事实。','fact_ids':['F2','F1']}]
            after,claims,receipt=a.generate(engine,'改名后的方案报告',self.focus,self.units,registry,
                                           prepared=persisted,outline_override=override)
            self.assertEqual(len(engine.requests),4,'Continuation must generate only the chapter, not reread or replan')
            self.assertEqual(receipt['calls'],1);self.assertEqual(receipt['prepared_reused_calls'],3)
            self.assertEqual(receipt['cache_reused_calls'],0)
            self.assertEqual(after['original_outline'],prepared['outline'])
            self.assertEqual(after['outline'],override);self.assertTrue(after['outline_edited'])
            self.assertIn(self.focus,engine.requests[-1]['messages'][-1]['content'])
            self.assertIn(override[0]['purpose'],engine.requests[-1]['messages'][-1]['content'])
            self.assertEqual([f['refs'] for f in after['facts']],[f['refs'] for f in prepared['facts']])
            self.assertEqual({rid for c in claims for rid in c['refs']},{1,2})
            self.assertEqual(after['status'],'complete')
            self.assertEqual(after['reading_mode'],'single_source_bound_facts')

    def test_edit_rejects_missing_duplicate_unknown_or_nested_fact_ids(self):
        facts=[{'id':'F1'},{'id':'F2'}]
        bad=[
            [{'title':'条件','fact_ids':['F1']}],
            [{'title':'条件甲','fact_ids':['F1']},{'title':'条件乙','fact_ids':['F1','F2']}],
            [{'title':'条件','fact_ids':['F1','F99']}],
            [{'title':'条件','fact_ids':[{'id':'F1'},'F2']}]
        ]
        for outline in bad:
            with self.subTest(outline=outline),self.assertRaises(ValueError):a.validate_outline(outline,facts)

    def test_changed_prompt_snapshot_and_injected_source_rejected_without_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            model=Path(directory)/'model.gguf';model.write_bytes(b'fixture')
            engine=Engine(model,[*self.atomic_readings,self.plan]);registry=Registry()
            prepared,_,_=a.generate(engine,'方案对照',self.focus,self.units,registry,stop_after_outline=True)
            with self.assertRaises(ValueError):
                a.generate(engine,'方案对照',self.focus+'新增要求',self.units,registry,prepared=prepared)
            changed=copy.deepcopy(self.units);changed[0]['tab']='另一页签'
            with self.assertRaises(ValueError):a.generate(engine,'方案对照',self.focus,changed,registry,prepared=prepared)
            injected=copy.deepcopy(prepared);injected['facts'][0]['source_ids']=['S999']
            with self.assertRaises(ValueError):a.generate(engine,'方案对照',self.focus,self.units,registry,prepared=injected)
            changed_quote=copy.deepcopy(prepared);changed_quote['facts'][0]['evidence'][0]['quote']='其他内容'
            with self.assertRaises(ValueError):a.generate(engine,'方案对照',self.focus,self.units,registry,prepared=changed_quote)
            self.assertEqual(len(engine.requests),3)

    def test_empty_focus_keeps_legacy_source_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            model=Path(directory)/'model.gguf';model.write_bytes(b'fixture')
            engine=Engine(model,[self.reading,{'C1':'甲方案的条件','C2':'乙方案的条件'}])
            prepared,_,_=a.generate(engine,'方案记录','',self.units,Registry(),stop_after_outline=True)
            self.assertEqual(prepared['outline_origin'],'source_compatibility')
            self.assertEqual([c['fact_ids'] for c in prepared['outline']],[['F1'],['F2']])
            self.assertEqual(prepared['snapshot_contract']['focus'],'')

    def test_resume_reregisters_exact_spans_in_current_registry(self):
        blocks=a.source_blocks(self.units);generated,_=a._parse_read(
            dict(text=json.dumps(self.reading,ensure_ascii=False),finish_reason='stop'),blocks)
        facts=[]
        for index,f in enumerate(generated,1):
            facts.append(dict(id=f'F{index}',text=f['text'],topic=f['topic'],source_ids=f['source_ids'],
                              evidence=f['evidence_spans'],refs=[999]))
        contract=a.snapshot_contract(self.units,self.focus)
        prepared=dict(snapshot_contract=contract,source_blocks=blocks,facts=facts,
                      outline=[dict(title='执行条件',purpose='',fact_ids=['F1','F2'])],read_block_ids=['S1','S2'])
        registry=Registry(start=50)
        restored,read_ids=a.restore_prepared(prepared,contract,blocks,self.units,registry)
        self.assertEqual([f['refs'] for f in restored],[[50],[51]])
        self.assertEqual(registry.items,[(b['unit_id'],b['start'],b['end']) for b in blocks])
        self.assertEqual(read_ids,{'S1','S2'})

    def test_each_reading_has_one_source_and_application_binds_its_reference(self):
        blocks=a.source_blocks(self.units)
        engine=SimpleNamespace(n_ctx=10000,report_prompt_tokens=lambda _:100)
        batches=a.atomic_reading_plan(engine,'核对',self.focus,blocks)
        self.assertEqual(len(batches),2)
        self.assertEqual([b['id'] for b in batches[0]],['S1'])
        self.assertEqual([b['id'] for b in batches[1]],['S2'])
        messages=a.atomic_reading_messages('核对',self.focus,batches[0])
        self.assertIn(self.units[0]['body'],messages[-1]['content'])
        self.assertNotIn(self.units[1]['body'],messages[-1]['content'])
        response=dict(text=json.dumps(self.atomic_readings[0],ensure_ascii=False),finish_reason='stop')
        facts,omitted=a.parse_atomic_read(response,batches[0])
        self.assertEqual(facts[0]['source_ids'],['S1']);self.assertEqual(omitted,[])
        self.assertEqual(facts[0]['evidence_spans'][0]['quote'],self.units[0]['body'])
        injected=copy.deepcopy(self.atomic_readings[0]);injected['facts'][0]['source_ids']=['S2']
        with self.assertRaises(ValueError):a.parse_atomic_read(dict(text=json.dumps(injected),finish_reason='stop'),batches[0])

    def test_table_reading_includes_header_with_each_whole_row(self):
        units=[unit('| 名称 | 条件 |\n| --- | --- |\n| 甲 | 人工批准 |\n| 乙 | 复核后执行 |',1)]
        blocks=a.source_blocks(units);engine=SimpleNamespace(n_ctx=10000,report_prompt_tokens=lambda _:100)
        batches=a.atomic_reading_plan(engine,'条件','整理为核对清单',blocks)
        self.assertEqual(len(batches),2)
        for batch in batches:
            self.assertEqual([b['kind'] for b in batch],['table_header','table_row'])
            response=dict(text=json.dumps({'facts':[{'topic':'执行条件','text':'应按原表中对应条件和名称核对后执行。'}],'omitted_reason':''},ensure_ascii=False),finish_reason='stop')
            facts,_=a.parse_atomic_read(response,batch)
            self.assertEqual(facts[0]['source_ids'],[b['id'] for b in batch])

    def test_per_source_reading_limit_rejects_entire_plan_not_first_n_blocks(self):
        units=[unit('这一条资料说明必须先核对原文再执行。',i+1) for i in range(a.MAX_BATCHES+1)]
        blocks=a.source_blocks(units);engine=SimpleNamespace(n_ctx=10000,report_prompt_tokens=lambda _:100)
        with self.assertRaisesRegex(ValueError,'上限'):a.atomic_reading_plan(engine,'资料','生成报告',blocks)

    def test_staged_writer_rejects_fact_repetition_and_allocates_whole_document_budget(self):
        facts=[dict(id='F1',refs=[1]),dict(id='F2',refs=[2])]
        response=dict(text=json.dumps({'paragraphs':[
            {'text':'首先介绍两个事实中共同需要核对的条件。','fact_ids':['F1','F2']},
            {'text':'随后再次重复第一条事实的条件与说明。','fact_ids':['F1']}]}),finish_reason='stop')
        with self.assertRaisesRegex(ValueError,'重复'):a._parse_written(response,facts,strict=True)
        # Empty-focus compatibility does not change its previous parser policy.
        paragraphs,_=a._parse_written(response,facts)
        self.assertEqual(len(paragraphs),2)
        self.assertEqual(a.document_char_budget('全稿约450至650字，每章按重点分配。'),650)
        self.assertEqual(a.document_char_budget('控制在500字。'),500)


if __name__=='__main__':unittest.main()
