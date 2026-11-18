# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Zero-generation contracts for exact-source briefs, not quality scores."""
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

from pocket_memory import report_brief as b
from pocket_memory import report_compiled as c


def unit(uid, body, source_id=1, kind='note', tab='正文', field='content'):
    return dict(unit_id=uid, kind=kind, source_id=source_id, note_id=source_id if kind == 'note' else None,
        title=f'资料{source_id}', tab=tab, field=field, body=body,
        revision=hashlib.sha256(body.encode()).hexdigest())


def sources():
    return [unit('n1', '执行清单：\n- 核对来源。\n- 仅在负责人批准后执行。\n- 未批准时保留草稿，不得发送。'),
        unit('n2', '| 名称 | 状态 |\n| --- | --- |\n| 方案甲 | 已验证 |\n| 方案乙 | 规划 |', 2),
        unit('n3', ''.join(f'背景第{i}段介绍历史材料的来源，保留此段供检索。' for i in range(70)), 3)]


class Registry:
    def __init__(self, offset=0):
        self.items = []; self.offset = offset
    def __call__(self, u, start, end):
        key = (u['unit_id'], start, end)
        if key not in self.items:
            self.items.append(key)
        return self.offset+self.items.index(key)+1


class Engine:
    n_ctx = 3072
    n_threads = 4
    n_gpu_layers = 0
    def __init__(self, path, mode='valid', select_index=0):
        self.model_path = path; self.mode = mode; self.select_index = select_index
        self.requests = []; self.sessions = 0
    def session(self):
        self.sessions += 1
        return nullcontext()
    def report_prompt_tokens(self, messages):
        return sum(len(m['content']) for m in messages)
    def report_completion(self, messages, max_tokens, schema=None):
        assert self.report_prompt_tokens(messages)+max_tokens+b.MARGIN <= self.n_ctx
        self.requests.append(dict(messages=messages, max_tokens=max_tokens, schema=schema))
        ids = schema['properties']['chapters']['items']['properties']['groups']['items']['enum']
        selected = [ids[self.select_index]]
        obj = dict(chapters=[dict(title='执行前的核对', groups=selected)])
        if self.mode == 'empty':
            obj['chapters'] = []
        if self.mode == 'unknown':
            obj['chapters'][0]['groups'] = ['G9999']
        if self.mode == 'duplicate':
            obj['chapters'].append(dict(title='重复的选择', groups=selected))
        if self.mode == 'duplicate_in_chapter':
            obj['chapters'][0]['groups'] = selected * 2
        chosen={gid for ch in obj['chapters'] for gid in ch['groups']}
        obj.update(scope=dict(include='指定内容',exclude='其他内容'),decisions=[dict(group=gid,decision='include' if gid in chosen else 'exclude',reason='夹具选择') for gid in ids])
        text = json.dumps(obj, ensure_ascii=False)
        if self.mode == 'duplicate_key':
            text = text.replace('"title":', '"title": "重复", "title":', 1)
        return dict(text=text, finish_reason='length' if self.mode == 'truncated' else 'stop',
            usage={'completion_tokens': 42})


class ReportBriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name); self.model = self.path/'fixture.gguf'
        self.model.write_bytes(b'fixture')
        self.focus = '给业务同事准备核对清单，保留条件，不需要无关背景。'

    def generate(self, engine=None, source=None, **kwargs):
        return b.generate(engine or Engine(self.model), '重点资料', kwargs.pop('focus', self.focus),
            source or sources(), kwargs.pop('register', Registry()),
            cache_dir=kwargs.pop('cache_dir', self.path/'cache'), **kwargs)

    def test_one_call_selects_subset_and_retains_full_snapshot_without_whole_read_claim(self):
        engine = Engine(self.model); report, claims, receipt = self.generate(engine)
        self.assertEqual(receipt['calls'], 1)
        self.assertEqual(len(engine.requests), 1)
        self.assertEqual(engine.requests[0]['max_tokens'], b.OUTPUT_TOKENS)
        self.assertEqual(report['reading_mode'], 'source_brief_v2')
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['selection']['excluded'])
        self.assertLess(len(report['facts']), len(report['all_inventory_facts']))
        self.assertTrue(report['selected_coverage']['complete'])
        self.assertFalse(report['all_coverage']['model_read_full_sources'])
        self.assertEqual(report['all_coverage']['source_scan_nonspace_chars'], report['all_coverage']['total_nonspace_chars'])
        self.assertTrue(claims)
        self.assertTrue(all('背景第' not in claim['text'] for claim in claims))
        self.assertIn(self.focus, engine.requests[0]['messages'][1]['content'])
        for group in report['groups']:
            self.assertIn(group['id']+'｜', engine.requests[0]['messages'][1]['content'])
            self.assertIn(group['id']+'｜'+group['title']+'｜', engine.requests[0]['messages'][1]['content'])
        for chapter in report['chapters']:
            self.assertEqual(chapter['guidance'], [])
            self.assertTrue(all(item['placement'] == 'main' for item in chapter['paragraphs']+chapter['tables']))
        self.assertIn('重点摘编', report['review_status'])

    def test_frames_preserve_cross_page_conditions_and_all_rows_of_a_header(self):
        original = [unit('d1p1', '如果申请尚未获批准，', 4, 'document', '第1页'),
            unit('d1p2', '不得发送给客户。\n否则必须撤回并复核。', 4, 'document', '第2页'), sources()[1]]
        blocks, facts, _, _ = c.load_inventory(original, Registry())
        groups = b.atomic_groups(original, blocks, facts)
        condition = next(g for g in groups if 'd1p1' in g['unit_ids'])
        self.assertIn('d1p2', condition['unit_ids'])
        rows = [fact['id'] for fact in facts if fact['unit_id'] == 'n2']
        table = next(g for g in groups if 'n2' in g['unit_ids'])
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(set(rows), set(table['fact_ids']))
        self.assertNotIn('n2', condition['unit_ids'])
        self.assertNotIn('d1p1', table['unit_ids'])
        self.assertEqual(sum(len(g['fact_ids']) for g in groups), len(facts))

    def test_table_selection_compiles_every_row_without_supplement_or_guidance(self):
        original = [sources()[1], sources()[2]]
        report, claims, _ = self.generate(source=original)
        table = report['chapters'][0]['tables'][0]
        self.assertEqual(table['columns'], ['名称', '状态'])
        self.assertEqual([r['cells'] for r in table['rows']], [['方案甲', '已验证'], ['方案乙', '规划']])
        self.assertTrue(all(len(row['refs']) == 2 for row in table['rows']))
        self.assertEqual(table['placement'], 'main')
        self.assertEqual(report['chapters'][0]['guidance'], [])
        self.assertEqual(len(claims), 2)

    def test_editing_cannot_split_table_or_drop_one_condition_fact(self):
        original = [sources()[1]]
        report, _, _ = self.generate(source=original, stop_after_outline=True)
        facts = report['facts']; self.assertGreaterEqual(len(facts), 2)
        split = [dict(title='第一部分', fact_ids=[facts[0]['id']]),
            dict(title='第二部分', fact_ids=[f['id'] for f in facts[1:]])]
        with self.assertRaisesRegex(ValueError, '跨章拆分'):
            b.validate_outline(split, facts, report['groups'])
        with self.assertRaises(ValueError):
            b.validate_outline([dict(title='只保留一行', fact_ids=[facts[0]['id']])], facts[:1], report['groups'])

    def test_exact_previews_disclose_long_group_middle_not_read(self):
        original = sources(); blocks, facts, _, _ = c.load_inventory(original, Registry())
        groups = b.atomic_groups(original, blocks, facts)
        previews = b.preview_spans(groups, blocks, 64)
        by_unit = {u['unit_id']: u for u in original}
        c._validate_previews(previews, blocks)
        for p in previews:
            self.assertEqual(p['quote'], by_unit[p['unit_id']]['body'][p['start']:p['end']])
        messages, _ = b.organization_request('标题', self.focus, groups, original, previews)
        self.assertIn('未读中段', messages[1]['content'])
        self.assertTrue(all(any(p['group_id'] == g['id'] for p in previews) for g in groups))

    def test_invalid_outputs_and_budget_return_partial_without_full_original_fallback(self):
        for mode in ('unknown', 'duplicate_in_chapter', 'duplicate_key', 'truncated'):
            with self.subTest(mode=mode):
                report, claims, receipt = self.generate(Engine(self.model, mode), cache_dir=self.path/mode)
                self.assertEqual(report['status'], 'partial')
                self.assertEqual(report['facts'], [])
                self.assertEqual(report['chapters'], [])
                self.assertEqual(claims, [])
                self.assertEqual(report['selection']['status'], 'failed')
                self.assertTrue(report['all_inventory_facts'])
                self.assertEqual(receipt['calls'], 1)
                self.assertTrue(report['trace'][0]['response'])
        engine = Engine(self.model); engine.n_ctx = 100
        report, claims, receipt = self.generate(engine)
        self.assertEqual(report['status'], 'partial'); self.assertEqual(claims, [])
        self.assertEqual(receipt['calls'], 0); self.assertEqual(engine.requests, [])

    def test_cross_chapter_repeat_keeps_first_owner_and_raw_model_response(self):
        original = [sources()[1], sources()[2]]
        before = copy.deepcopy(original)
        baseline, baseline_claims, _ = self.generate(source=original, cache_dir=None)
        report, claims, _ = self.generate(Engine(self.model, 'duplicate'), source=original, cache_dir=None)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['facts'], baseline['facts'])
        self.assertEqual(report['outline'], baseline['outline'])
        self.assertEqual(report['chapters'], baseline['chapters'])
        self.assertEqual(claims, baseline_claims)
        self.assertNotIn('chapter_normalization', baseline['selection'])
        normalized = report['selection']['chapter_normalization']
        gid = report['selection']['included_group_ids'][0]
        self.assertEqual(normalized['removed_chapter_ids'], ['C2'])
        self.assertEqual(normalized['duplicate_references'], [dict(
            group_id=gid, kept_in_chapter_id='C1', removed_from_chapter_id='C2')])
        response = report['trace'][0]['response']
        self.assertEqual(len(json.loads(response['text'])['chapters']), 2)
        self.assertEqual(normalized['original_chapters'], json.loads(response['text'])['chapters'])
        self.assertEqual(report['organization_record']['response'], response)
        self.assertEqual(original, before)

    def test_normalized_selection_survives_cache_and_prepared_rebinding(self):
        prepared, _, _ = self.generate(Engine(self.model, 'duplicate'), stop_after_outline=True)
        self.assertEqual(prepared['status'], 'outline')
        expected_selection = copy.deepcopy(prepared['selection'])
        expected_response = copy.deepcopy(prepared['organization_record']['response'])
        cached_engine = Engine(self.model, 'duplicate')
        cached, _, cached_receipt = self.generate(cached_engine, stop_after_outline=True)
        self.assertEqual(cached_engine.requests, [])
        self.assertEqual(cached_receipt['cache_reused_calls'], 1)
        self.assertEqual(cached['selection'], expected_selection)
        engine = Engine(self.model, 'duplicate')
        report, claims, receipt = self.generate(engine, prepared=prepared, register=Registry(100))
        self.assertEqual(engine.requests, [])
        self.assertEqual(receipt['calls'], 0)
        self.assertEqual(receipt['prepared_reused_calls'], 1)
        self.assertEqual(report['selection'], expected_selection)
        self.assertEqual(report['organization_record']['response'], expected_response)
        self.assertTrue(claims)
        self.assertTrue(all(ref > 100 for claim in claims for ref in claim['refs']))

    def test_prepared_rebinds_refs_and_edits_title_without_new_generation(self):
        prepared, _, _ = self.generate(stop_after_outline=True)
        self.assertEqual(prepared['status'], 'outline'); self.assertEqual(prepared['chapters'], [])
        edited = copy.deepcopy(prepared['outline']); edited[0]['title'] = '人工确认的章节'
        engine = Engine(self.model)
        report, claims, receipt = self.generate(engine, prepared=prepared, outline_override=edited, register=Registry(100))
        self.assertEqual(engine.sessions, 0); self.assertEqual(engine.requests, [])
        self.assertEqual(receipt['calls'], 0); self.assertEqual(receipt['prepared_reused_calls'], 1)
        self.assertEqual(report['chapters'][0]['title'], '人工确认的章节')
        self.assertTrue(all(ref > 100 for claim in claims for ref in claim['refs']))
        self.assertEqual(report['selection'], prepared['selection'])

    def test_prepared_rejects_source_focus_config_selection_group_and_fact_tampering(self):
        prepared, _, _ = self.generate(stop_after_outline=True)
        for field in ('facts', 'all_inventory_facts', 'groups', 'selection', 'original_outline', 'organization_record'):
            changed = copy.deepcopy(prepared)
            if field in ('facts', 'all_inventory_facts'):
                changed[field][0]['text'] += '未经原文支持的扩写'
            elif field == 'groups':
                changed[field][0]['fact_ids'] = []
            elif field == 'selection':
                changed[field]['included'] = ['G999']
            elif field == 'original_outline':
                changed[field][0]['title'] = '篡改原始模型输出'
            else:
                changed[field]['response']['text'] = '{}'
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.generate(prepared=changed)
        with self.assertRaises(ValueError):
            self.generate(prepared=prepared, focus='新的完整要求')
        engine = Engine(self.model); engine.n_threads = 8
        with self.assertRaises(ValueError):
            self.generate(engine, prepared=prepared)
        original = sources(); original[0]['body'] += '未更新修订号'
        with self.assertRaises(ValueError):
            self.generate(source=original, prepared=prepared)

    def test_cache_reuses_only_sealed_matching_request_and_model_configuration(self):
        self.generate()
        engine = Engine(self.model); _, _, receipt = self.generate(engine)
        self.assertEqual(engine.sessions, 0); self.assertEqual(receipt['cache_reused_calls'], 1)
        path = next((self.path/'cache'/b.PROTOCOL/'organization').glob('*.json'))
        saved = json.loads(path.read_text('utf-8')); saved['previews'][0]['quote'] = '被改写的原句'
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        engine = Engine(self.model); report, _, receipt = self.generate(engine)
        self.assertEqual(receipt['calls'], 1)
        self.assertTrue(any(i['stage'] == 'cache' for i in report['issues']))
        engine = Engine(self.model); engine.n_threads = 8
        _, _, receipt = self.generate(engine)
        self.assertEqual(receipt['calls'], 1)


if __name__ == '__main__':
    unittest.main()
