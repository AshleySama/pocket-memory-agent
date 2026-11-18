# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Long-report capacity and source identity checks; no real model is loaded.

Synthetic responses exercise contracts, not report semantic quality.
"""
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pocket_memory import report_adaptive as a


def units(count=62):
    result=[]
    for i in range(count):
        body=f'第{i+1}项记录：仅在人工核对资料并获得批准后，才能执行本项操作；未批准时不得启用。'
        root=i%5+1
        result.append(dict(unit_id=f'n{root}:tab{i}:content',kind='note',source_id=root,note_id=root,
                           title=f'资料{root}',tab=f'页签{i+1}',field='content',body=body,
                           revision=hashlib.sha256(body.encode()).hexdigest()))
    return result


class Registry:
    def __init__(self):self.items=[]
    def __call__(self,unit,start,end):
        key=(unit['unit_id'],start,end)
        if key not in self.items:self.items.append(key)
        return self.items.index(key)+1


class Engine:
    n_ctx=3072
    def __init__(self,path,facts_per_block=1,omit_first=False):
        self.model_path=path
        self._llm=SimpleNamespace(metadata={'tokenizer.chat_template':'fixture'})
        self.requests=[];self.facts_per_block=facts_per_block;self.omit_first=omit_first
    def session(self):return nullcontext()
    def report_prompt_tokens(self,messages):
        # Conservative deterministic fixture count. Production uses GGUF tokens.
        return sum(len(m['content']) for m in messages)
    def fact(self,index=0):
        return dict(topic=f'执行条件{index+1}',text=f'第{index+1}个事实要求先由人工核对资料并取得批准，未批准不得启用。')
    def atomic(self):return dict(facts=[self.fact(i) for i in range(self.facts_per_block)],omitted_reason='')
    def report_completion(self,messages,max_tokens,schema=None):
        self.assert_budget(messages,max_tokens)
        self.requests.append(dict(messages=copy.deepcopy(messages),schema=copy.deepcopy(schema),max_tokens=max_tokens))
        props=schema['properties']
        if props and all(k.startswith('S') for k in props):
            obj={k:self.atomic() for k in props}
            if self.omit_first:
                self.omit_first=False
                obj[next(iter(props))]=dict(facts=[],omitted_reason='模型未提取此单元的事实')
        elif 'facts' in props:obj=self.atomic()
        elif props and all(k.startswith('F') for k in props):obj={k:props[k]['enum'][0] for k in props}
        elif 'chapters' in props:
            fields=props['chapters']['items']['properties']
            chapter=dict(title='执行条件与限制',purpose='说明资料中操作执行前必须满足的条件。')
            if 'fact_ids' in fields:chapter['fact_ids']=fields['fact_ids']['items']['enum']
            obj=dict(chapters=[chapter])
        elif 'paragraphs' in props:
            ids=props['paragraphs']['items']['properties']['fact_ids']['items']['enum']
            obj=dict(paragraphs=[dict(text=self.fact()['text'],fact_ids=[fid]) for fid in ids])
        else:raise AssertionError(f'Unexpected schema: {props}')
        return dict(text=json.dumps(obj,ensure_ascii=False),finish_reason='stop')
    def assert_budget(self,messages,output):
        if self.report_prompt_tokens(messages)+output+a.MARGIN>self.n_ctx:raise AssertionError('Unbudgeted request')


class LongReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.model=Path(self.tmp.name)/'fixture.gguf';self.model.write_bytes(b'fixture-not-a-model')
        self.focus='为业务同事介绍资料里的操作条件，完整保留否定和限制。'
    def run_report(self,engine=None,source=None,**kw):
        return a.generate(engine or Engine(self.model),'培训说明',self.focus,source or units(),Registry(),
                          cache_dir=Path(self.tmp.name)/'cache',stop_after_outline=True,
                          long_reading_protocol=a.LONG_VERSION,**kw)

    def test_62_units_use_bounded_batches_and_keep_every_source(self):
        source=units();blocks=a.source_blocks(source);engine=Engine(self.model)
        self.assertEqual(len(blocks),62)
        plan=a.grouped_reading_plan(engine,'培训说明',self.focus,blocks)
        self.assertEqual(len(plan),21)
        self.assertTrue(all(len(batch)<=3 for batch in plan))
        result,claims,receipt=self.run_report(engine,source)
        self.assertEqual(result['status'],'outline');self.assertEqual(claims,[])
        self.assertEqual(result['coverage']['read_blocks'],62)
        self.assertEqual(result['coverage']['completed_batches'],21)
        self.assertEqual(len(result['facts']),62)
        self.assertEqual({fid for c in result['outline'] for fid in c['fact_ids']},{f['id'] for f in result['facts']})
        self.assertLessEqual(receipt['calls'],a.MAX_CALLS)
        self.assertTrue(all(len(f['source_ids'])==1 for f in result['facts']))
        by_id={b['id']:b for b in blocks}
        for fact in result['facts']:
            e=fact['evidence'][0];self.assertEqual(e['quote'],by_id[e['source_id']]['text'])
            self.assertEqual(e['unit_id'],by_id[e['source_id']]['unit_id'])

    def test_group_keys_and_table_header_cannot_cross_bind(self):
        source=units(1)
        text='| 名称 | 状态 |\n| --- | --- |\n| 甲 | 未批准 |\n| 乙 | 已批准 |'
        source[0].update(body=text,revision=hashlib.sha256(text.encode()).hexdigest())
        blocks=a.source_blocks(source);independent=a.independent_reading_units(blocks)
        obj={unit[-1]['id']:Engine(self.model).atomic() for unit in independent}
        parsed,_=a.parse_grouped_read(dict(text=json.dumps(obj,ensure_ascii=False),finish_reason='stop'),independent)
        self.assertEqual(len(parsed),2)
        self.assertEqual(parsed[0]['source_ids'],[blocks[0]['id'],blocks[1]['id']])
        self.assertEqual(parsed[1]['source_ids'],[blocks[0]['id'],blocks[2]['id']])
        obj.pop(next(iter(obj)))
        with self.assertRaises(ValueError):
            a.parse_grouped_read(dict(text=json.dumps(obj),finish_reason='stop'),independent)

    def test_budget_pauses_before_outline_and_resume_only_reads_remaining(self):
        first=Engine(self.model);checkpoints=[]
        with patch.object(a,'MAX_CALLS',2):
            saved,_,receipt=self.run_report(first,reading_checkpoint=lambda state,gen:checkpoints.append(copy.deepcopy(state)))
        self.assertEqual(receipt['calls'],2);self.assertEqual(saved['status'],'reading_paused')
        self.assertEqual(saved['resume_reason'],'budget');self.assertEqual(saved['outline'],[])
        self.assertEqual(saved['coverage']['completed_batches'],2);self.assertGreaterEqual(len(checkpoints),2)
        previous_facts=copy.deepcopy(saved['facts'])
        second=Engine(self.model)
        result,_,_=self.run_report(second,resume_reading=saved)
        self.assertEqual(result['status'],'outline')
        self.assertEqual(result['facts'][:len(previous_facts)],previous_facts)
        first_keys=list(second.requests[0]['schema']['properties'])
        self.assertEqual(first_keys,['S7','S8','S9'])
        self.assertEqual(result['coverage']['read_blocks'],62)

    def test_planning_budget_retains_all_reading_and_reuses_assignment_cache(self):
        first=Engine(self.model,facts_per_block=3)
        with patch.object(a,'MAX_CALLS',25):saved,_,receipt=self.run_report(first)
        self.assertEqual(receipt['calls'],25)
        self.assertEqual(saved['status'],'reading_paused')
        self.assertEqual(saved['resume_reason'],'planning_budget')
        self.assertEqual(saved['coverage']['completed_batches'],21)
        self.assertEqual(len(saved['facts']),62)
        second=Engine(self.model,facts_per_block=3)
        result,_,receipt=self.run_report(second,resume_reading=saved)
        self.assertEqual(result['status'],'outline')
        self.assertTrue(all(all(k.startswith('F') for k in r['schema']['properties']) for r in second.requests))
        self.assertEqual(sum(len(c['fact_ids']) for c in result['outline']),62)
        self.assertGreater(receipt['cache_reused_calls'],0)

    def test_long_ledger_keeps_original_when_model_omits_its_summary(self):
        saved,_,_=self.run_report(Engine(self.model,omit_first=True))
        self.assertEqual(saved['status'],'outline')
        self.assertEqual(saved['coverage']['completed_batches'],21)
        self.assertEqual(len(saved['facts']),62)
        self.assertEqual(saved['facts'][0]['text'],units()[0]['body'])
        self.assertEqual(saved['facts'][0]['origin'],'original_excerpt')

    def test_model_cross_block_paraphrases_cannot_pollute_original_ledger(self):
        source=units();result,_,_=self.run_report(Engine(self.model,facts_per_block=3))
        for fact in result['facts']:
            self.assertEqual(fact['text'],'\n'.join(e['quote'] for e in fact['evidence']))
        changed=copy.deepcopy(result);changed['facts'][0]['text']='其他原文中的内容被移入这里，不允许继续成稿。'
        with self.assertRaisesRegex(ValueError,'账本'):self.run_report(prepared=changed)
        changed=copy.deepcopy(result);del changed['reading_contract']['ledger_version']
        with self.assertRaisesRegex(ValueError,'旧长资料'):self.run_report(prepared=changed)

    def test_resume_rejects_changed_snapshot_or_plan_without_generation(self):
        with patch.object(a,'MAX_CALLS',1):saved,_,_=self.run_report()
        engine=Engine(self.model);changed=copy.deepcopy(saved)
        changed['reading_contract']['batches'][0]=['S62']
        with self.assertRaises(ValueError):self.run_report(engine,resume_reading=changed)
        source=units();source[0]['tab']='另一个页签'
        with self.assertRaises(ValueError):self.run_report(engine,source,resume_reading=saved)
        self.assertEqual(engine.requests,[])

    def test_short_requests_and_cache_fingerprint_are_unchanged(self):
        source=units(2);engine=Engine(self.model)
        result,_,_=self.run_report(engine,source)
        blocks=a.source_blocks(source)
        self.assertEqual(engine.requests[0]['messages'],a.atomic_reading_messages('培训说明',self.focus,[blocks[0]]))
        self.assertEqual(engine.requests[0]['max_tokens'],a.ATOM_READ_OUTPUT)
        self.assertNotIn('reading_protocol',result['model_contract'])
        second=Engine(self.model)
        reused,_,receipt=self.run_report(second,source)
        self.assertEqual(receipt['calls'],0);self.assertEqual(second.requests,[])
        self.assertEqual(result['facts'],reused['facts'])

    def test_long_writer_has_compact_directory_and_single_copy_per_source(self):
        source=units(1);blocks=a.source_blocks(source);b=blocks[0]
        evidence=dict(source_id=b['id'],unit_id=b['unit_id'],start=b['start'],end=b['end'],quote=b['text'])
        facts=[dict(id=f'F{i}',text='执行操作前须经人工核对并取得批准。',source_ids=[b['id']],evidence=[evidence]) for i in (1,2)]
        chapter=dict(id='C1',title='执行条件',purpose='保留条件',fact_ids=['F1','F2'])
        scope=dict(outline=[chapter,dict(id='C2',title='其他事项',purpose='其他',fact_ids=['F999'])],
                   total_chars=800,target_chars=100,compact_directory=True)
        messages=a.writing_messages('培训说明',chapter,facts,{b['id']:b},self.focus,scope)
        text=messages[-1]['content']
        self.assertNotIn('F999',text);self.assertEqual(text.count(b['text']),1)
        self.assertIn('F1',text);self.assertIn('F2',text)


if __name__=='__main__':unittest.main()
