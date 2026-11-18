# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Multi-source selection envelopes; no local model generation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from pocket_memory import report_brief as b
from tests.release_current.test_report_brief import Engine, Registry, unit


class CompactEngine(Engine):
    n_ctx = 20000

    def report_completion(self, messages, max_tokens, schema=None):
        self.requests.append(dict(messages=messages, max_tokens=max_tokens, schema=schema))
        ids = list(schema['properties']['decisions']['properties'])
        obj = dict(decisions={gid: 'include' if i % 2 == 0 else 'exclude' for i, gid in enumerate(ids)},
                   chapters=[dict(title='执行材料', groups=ids[::2])])
        return dict(text=json.dumps(obj, ensure_ascii=False), finish_reason='stop', usage={'completion_tokens': 130})


class SelectionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name); self.model = self.path/'model.gguf'; self.model.write_bytes(b'fixture')
        self.units = [unit(f'n{i}', f'材料{i}：必须先获批准，未经核验不得发送。', i) for i in range(1, 11)]
        self.engine = CompactEngine(self.model)

    def build(self, engine=None, **kwargs):
        return b.generate(engine or self.engine, '执行材料', '只整理相关材料，保留条件。', self.units,
                          kwargs.pop('register', Registry()), cache_dir=self.path/'cache', **kwargs)

    def test_keyed_decisions_compile_only_selected_whole_groups_and_reuse_cache(self):
        result, claims, receipt = self.build()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['selection']['response_protocol'], b.COMPACT_PROTOCOL)
        self.assertEqual(len(result['selection']['group_decisions']), 10)
        self.assertEqual(len(result['selection']['included']), 5)
        self.assertTrue(result['selected_coverage']['complete'])
        self.assertTrue(all('未经核验不得发送' in c['text'] for c in claims))
        self.assertEqual(self.engine.requests[0]['max_tokens'], b.selection_output_tokens(result['groups']))
        self.assertLess(len(json.loads(result['trace'][0]['response']['text'])), 3)
        again, new_claims, cached = self.build(CompactEngine(self.model))
        self.assertEqual(cached['calls'], 0)
        self.assertEqual(again['chapters'], result['chapters'])
        self.assertEqual(new_claims, claims)

    def test_missing_unknown_uncertain_and_truncated_decisions_still_fail(self):
        result, _, _ = self.build()
        response=result['trace'][0]['response']; obj=json.loads(response['text'])
        variants=[]
        missing=copy.deepcopy(obj); missing['decisions'].pop('G10'); variants.append(missing)
        unknown=copy.deepcopy(obj); unknown['decisions']['G99']='exclude'; variants.append(unknown)
        uncertain=copy.deepcopy(obj); uncertain['decisions']['G10']='uncertain'; variants.append(uncertain)
        conflict=copy.deepcopy(obj); conflict['chapters'][0]['groups'].append('G2'); variants.append(conflict)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                b.parse_organization(dict(response,text=json.dumps(value)),result['groups'],result['all_inventory_facts'])
        with self.assertRaises(ValueError):
            b.parse_organization(dict(response,finish_reason='length'),result['groups'],result['all_inventory_facts'])

    def test_decision_object_order_does_not_change_source_binding(self):
        result, _, _ = self.build(); response=result['trace'][0]['response']; obj=json.loads(response['text'])
        obj['decisions']=dict(reversed(list(obj['decisions'].items())))
        outline,facts,selection=b.parse_organization(dict(response,text=json.dumps(obj)),result['groups'],result['all_inventory_facts'])
        self.assertEqual(outline,result['original_outline']); self.assertEqual(facts,result['facts'])
        self.assertEqual(selection,result['selection'])

    def test_large_inventory_reserves_output_before_preview_and_never_drops_directory(self):
        result, _, _ = self.build(); groups=result['groups']; blocks=result['source_blocks']
        engine=CompactEngine(self.model); engine.n_ctx=100
        with self.assertRaises(ValueError): b.organization_plan(engine,'标题','完整要求',groups,self.units,blocks)
        self.assertEqual(engine.requests,[])
        self.assertEqual(b.selection_output_tokens(groups[:7]),b.OUTPUT_TOKENS)
        self.assertGreater(b.selection_output_tokens(groups),b.OUTPUT_TOKENS)

    def test_prepared_rebinds_exact_references_without_another_generation(self):
        prepared,_,_=self.build(stop_after_outline=True)
        engine=CompactEngine(self.model)
        result,claims,receipt=self.build(engine,prepared=prepared,register=Registry(offset=50))
        self.assertEqual(result['status'],'complete'); self.assertEqual(receipt['calls'],0)
        self.assertFalse(engine.requests)
        self.assertTrue(all(ref>50 for claim in claims for ref in claim['refs']))


if __name__=='__main__': unittest.main()
