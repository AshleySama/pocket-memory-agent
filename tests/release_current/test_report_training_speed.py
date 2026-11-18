# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""No-generation checks for the lossless, single-read long report protocol.

Fixture counts establish scheduling and identity, not factual answer quality.
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
        body=f'第{i+1}项记录：人工核对资料并批准后才能执行；未批准不得启用。'
        result.append(dict(unit_id=f'n{i}:content',kind='note',source_id=i%5,note_id=i%5,
            title=f'资料{i%5}',tab=f'页签{i}',field='content',body=body,
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
    def __init__(self,path,fail_assignment=False):
        self.model_path=path;self.requests=[];self.fail_assignment=fail_assignment
        self._llm=SimpleNamespace(metadata={'tokenizer.chat_template':'fixture'})
    def session(self):return nullcontext()
    def report_prompt_tokens(self,messages):return sum(len(m['content']) for m in messages)
    def report_completion(self,messages,max_tokens,schema=None):
        assert self.report_prompt_tokens(messages)+max_tokens+a.MARGIN<=self.n_ctx
        self.requests.append(dict(messages=copy.deepcopy(messages),schema=copy.deepcopy(schema),max_tokens=max_tokens))
        props=schema['properties']
        if 'chapters' in props:obj=dict(chapters=[dict(title='资料使用与人工核对',purpose='说明执行条件、操作和边界')])
        elif props and all(k.startswith('F') for k in props):
            obj={fid:'C1' for fid in props}
            if self.fail_assignment:self.fail_assignment=False;obj.pop(next(iter(obj)))
        elif 'paragraphs' in props:
            ids=props['paragraphs']['items']['properties']['fact_ids']['items']['enum']
            obj=dict(paragraphs=[dict(text='依据资料逐项核对并取得人工批准，未经批准不得执行。',fact_ids=ids)])
        else:raise AssertionError('Direct mode must never request block paraphrases')
        return dict(text=json.dumps(obj,ensure_ascii=False),finish_reason='stop',usage={'completion_tokens':12})


class DirectSourceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.model=Path(self.tmp.name)/'fixture.gguf';self.model.write_bytes(b'fixture')
        self.focus='为业务同事制作培训报告，解释业务任务、所需资料和人工核对。'
    def run_report(self,engine=None,source=None,**kwargs):
        options=dict(cache_dir=Path(self.tmp.name)/'cache',stop_after_outline=True);options.update(kwargs)
        return a.generate(engine or Engine(self.model),'业务培训',self.focus,source or units(),Registry(),**options)

    def test_default_reads_full_original_only_in_assignment_without_21_summary_calls(self):
        engine=Engine(self.model);result,_,receipt=self.run_report(engine)
        self.assertEqual(result['status'],'outline')
        self.assertEqual(result['reading_mode'],'direct_source_ledger')
        self.assertEqual(result['coverage']['source_scan_blocks'],62)
        self.assertEqual(result['coverage']['model_read_blocks'],62)
        self.assertEqual(result['coverage']['read_nonspace_chars'],result['coverage']['total_nonspace_chars'])
        self.assertLess(receipt['calls'],10)
        self.assertGreater(max(len(b['fact_ids']) for b in result['reading_batches']),8)
        self.assertTrue(all(not t['phase'].startswith('read-') for t in result['trace']))
        for f in result['facts']:
            self.assertEqual(f['text'],'\n'.join(e['quote'] for e in f['evidence']))
        for batch in result['reading_batches']:
            request=next(t for t in result['trace'] if t['phase']==f'long-outline-assign-{batch["id"]}')
            for fid in batch['fact_ids']:
                fact=next(f for f in result['facts'] if f['id']==fid)
                self.assertIn(fact['text'],request['messages'][-1]['content'])
        self.assertTrue(all(t['input_tokens']+t['max_output_tokens']+a.MARGIN<=3072 for t in result['trace']))
        self.assertTrue(all(t['seconds']>=0 and t['output_tokens']==12 for t in result['trace']))

    def test_scan_and_directory_are_not_model_read_and_cannot_compose(self):
        with patch.object(a,'MAX_CALLS',1):saved,_,_=self.run_report()
        self.assertEqual(saved['status'],'reading_paused')
        self.assertEqual(len(saved['facts']),62)
        self.assertEqual(saved['coverage']['source_scan_blocks'],62)
        self.assertEqual(saved['coverage']['model_read_blocks'],0)
        self.assertEqual(saved['read_block_ids'],[])
        with self.assertRaises(ValueError):self.run_report(prepared=saved,stop_after_outline=False)
        engine=Engine(self.model);done,_,_=self.run_report(engine,resume_reading=saved)
        self.assertEqual(done['status'],'outline')
        self.assertTrue(all(all(k.startswith('F') for k in r['schema']['properties']) for r in engine.requests))

    def test_invalid_assignment_remains_resumable_without_false_completion(self):
        saved,_,_=self.run_report(Engine(self.model,fail_assignment=True))
        self.assertEqual(saved['status'],'reading_paused')
        self.assertEqual(saved['reading_batches'][0]['status'],'incomplete')
        self.assertEqual(saved['outline'],[])
        engine=Engine(self.model);done,_,_=self.run_report(engine,resume_reading=saved)
        self.assertEqual(done['status'],'outline')
        self.assertEqual(len(engine.requests),1)

    def test_invalid_directory_cache_is_retried_on_resume(self):
        class BadDirectory(Engine):
            def report_completion(self,messages,max_tokens,schema=None):
                response=super().report_completion(messages,max_tokens,schema)
                if 'chapters' in schema['properties']:
                    response['text']=json.dumps(dict(chapters=[]))
                return response
        saved,_,_=self.run_report(BadDirectory(self.model))
        self.assertEqual(saved['status'],'reading_paused')
        self.assertEqual(saved['resume_reason'],'planning_review')
        self.assertEqual(saved['coverage']['model_read_blocks'],0)
        engine=Engine(self.model);done,_,_=self.run_report(engine,resume_reading=saved)
        self.assertEqual(done['status'],'outline')
        self.assertIn('chapters',engine.requests[0]['schema']['properties'])

    def test_resume_refuses_fake_model_read_and_changed_directory(self):
        with patch.object(a,'MAX_CALLS',1):saved,_,_=self.run_report()
        changed=copy.deepcopy(saved);changed['read_block_ids']=['S1']
        engine=Engine(self.model)
        with self.assertRaises(ValueError):self.run_report(engine,resume_reading=changed)
        self.assertEqual(engine.requests,[])
        changed=copy.deepcopy(saved);changed['planning_directory'][0]['title']='篡改后的目录'
        with self.assertRaises(ValueError):self.run_report(engine,resume_reading=changed)

    def test_prepared_compose_and_cache_keep_identity_and_original_once(self):
        prepared,_,_=self.run_report()
        engine=Engine(self.model);draft,claims,_=self.run_report(engine,prepared=prepared,stop_after_outline=False)
        self.assertEqual(draft['status'],'complete');self.assertTrue(claims)
        self.assertEqual(draft['coverage']['written_facts'],62)
        self.assertTrue(all('paragraphs' in r['schema']['properties'] for r in engine.requests))
        for request in engine.requests:
            text=request['messages'][-1]['content']
            for fact in prepared['facts']:
                self.assertLessEqual(text.count(fact['text']),1)
        cached=Engine(self.model);again,_,receipt=self.run_report(cached)
        self.assertEqual(cached.requests,[]);self.assertEqual(receipt['calls'],0)
        self.assertEqual(again['facts'],prepared['facts'])

    def test_long_budget_respects_length_and_explicit_instruction(self):
        self.assertEqual(a.document_char_budget('培训说明',9253),3886)
        self.assertEqual(a.document_char_budget('约3500字',9253),3500)
        self.assertEqual(a.document_char_budget('全文200字',9253),200)
        self.assertEqual(a.document_char_budget('培训说明'),800)

    def test_direct_directory_describes_learning_structure_without_block_counts(self):
        source=units(3);facts=a.direct_source_facts(a.source_blocks(source),source,Registry())
        messages,_=a.long_outline_directory('业务培训',self.focus,facts,source,training_order=True)
        prompt='\n'.join(m['content'] for m in messages)
        self.assertNotIn('条事实',prompt)
        self.assertIn('第一任务是根据用户要求和受众',messages[0]['content'])
        self.assertIn('不可按文件逐一开章',messages[0]['content'])
        self.assertIn('本阶段只设计目录，不作实质事实陈述',messages[0]['content'])
        self.assertIn('案例数量',messages[0]['content'])
        self.assertIn('行业或政策背景集中后置',messages[0]['content'])
        self.assertIn('选材范围提示（不是章节顺序，不是完整事实）',messages[1]['content'])
        legacy,_=a.long_outline_directory('业务培训',self.focus,facts,source)
        self.assertIn('1条事实',legacy[1]['content'])
        self.assertNotIn('第一任务是根据用户要求和受众',legacy[0]['content'])


if __name__=='__main__':unittest.main()
