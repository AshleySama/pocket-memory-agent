# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Exact-section selection contracts with a deterministic, non-semantic engine.

These fixtures select schema group IDs by position. They verify boundaries,
dependencies, persistence, and citations; they do not measure model relevance.
"""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from pocket_memory import report_brief as brief
from pocket_memory import report_compiled
from pocket_memory.report_workspace import ReportWorkspace, export_html, render_report, valid_ref
from test_report_brief import Engine, Registry, unit


class SelectingEngine(Engine):
    """Choose only known group IDs, except in the explicit invalid-ID case."""
    n_ctx = 8192

    def __init__(self, path, selection=(0,)):
        super().__init__(path)
        self.selection = selection

    def report_completion(self, messages, max_tokens, schema=None):
        response = super().report_completion(messages, max_tokens, schema)
        ids = schema['properties']['chapters']['items']['properties']['groups']['items']['enum']
        if self.selection == 'none':
            chapters = []
        else:
            selected = ids if self.selection == 'all' else ['G9999'] if self.selection == 'unknown' else [ids[i] for i in self.selection]
            chapters = [dict(title='选定资料', groups=selected)]
        chosen={gid for ch in chapters for gid in ch['groups']}
        response['text'] = json.dumps(dict(scope=dict(include='指定内容',exclude='其他内容'), decisions=[dict(group=gid,decision='include' if gid in chosen else 'exclude',reason='夹具选择') for gid in ids],chapters=chapters), ensure_ascii=False)
        return response


def mixed_source():
    return [unit('mixed', '## 设备保养\n1. 断开电源。\n2. 清理通风口。\n未冷却不得拆开。\n\n'
                 '## 室内育苗\n1. 检查基质。\n2. 保持适量光照。\n出现积水时停止浇水。\n\n'
                 '## 费用报销\n保留原始凭据。\n审批完成后方可登记。\n')]


def claim_text(claims):
    return '\n'.join(claim['text'] for claim in claims)


class ReportSectionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.model = self.path / 'fixture.gguf'
        self.model.write_bytes(b'fixture')
        self.focus = '仅整理指定章节，保留其步骤和适用条件。'

    def generate(self, originals, selection=(0,), **kwargs):
        engine = kwargs.pop('engine', SelectingEngine(self.model, selection))
        register = kwargs.pop('register', Registry())
        result = brief.generate(engine, '分节资料', self.focus, originals, register,
                                cache_dir=kwargs.pop('cache_dir', None), **kwargs)
        return (*result, engine, register)

    def workspace(self, engine):
        return ReportWorkspace(SimpleNamespace(data_dir=self.path), engine, threading.RLock())

    def build_report(self, workspace, originals):
        selected = [dict(kind=originals[0]['kind'], id=originals[0]['source_id'])]
        return workspace.build('分节资料', 'auto', selected, originals,
                               focus=self.focus, composition_mode='source_compiled')

    def assert_exact_selected_ranges(self, originals, adaptive, ranges):
        by_unit = {u['unit_id']: u for u in originals}
        actual = set()
        for fact in adaptive['facts']:
            self.assertTrue(fact['refs'])
            for evidence in fact['evidence']:
                body = by_unit[evidence['unit_id']]['body']
                start, end = evidence['start'], evidence['end']
                self.assertEqual(evidence['quote'], body[start:end])
                actual.update((evidence['unit_id'], i) for i in range(start, end)
                              if not body[i].isspace())
        expected = {(uid, i) for uid, start, end in ranges for i in range(start, end)
                    if not by_unit[uid]['body'][i].isspace()}
        self.assertEqual(actual, expected)
        self.assertTrue(adaptive['selected_coverage']['complete'])

    def test_one_mixed_source_selects_exact_sections_without_neighbor_content(self):
        originals = mixed_source()
        body = originals[0]['body']
        headings = ['## 设备保养', '## 室内育苗', '## 费用报销']
        starts = [body.index(heading) for heading in headings] + [len(body)]
        for index, heading in enumerate(headings):
            with self.subTest(section=index):
                adaptive, claims, _, _, _ = self.generate(originals, (index,))
                self.assertEqual(len(adaptive['groups']), 3)
                self.assertTrue(all(g['source_sections'] for g in adaptive['groups']))
                self.assertIn(heading, claim_text(claims))
                for other in set(headings) - {heading}:
                    self.assertNotIn(other, claim_text(claims))
                self.assert_exact_selected_ranges(originals, adaptive,
                                                  [('mixed', starts[index], starts[index + 1])])

    def test_cross_page_tail_condition_stays_with_the_preceding_section(self):
        originals = [
            unit('p1', '# 工作手册\n## 接收阶段\n1. 核对记录。\n未获复核批准时，\n',
                 kind='document', field='document_text', tab='第1页'),
            unit('p2', '# 工作手册\n不得发布结果。\n\n## 归档阶段\n2. 归档凭据。\n',
                 kind='document', field='document_text', tab='第2页'),
        ]
        adaptive, claims, _, _, _ = self.generate(originals)
        text = claim_text(claims)
        self.assertIn('未获复核批准时', text)
        self.assertIn('不得发布结果', text)
        self.assertNotIn('归档凭据', text)
        self.assertNotIn('# 工作手册', text)
        self.assert_exact_selected_ranges(originals, adaptive, [
            ('p1', originals[0]['body'].index('## 接收阶段'), len(originals[0]['body'])),
            ('p2', originals[1]['body'].index('不得发布结果'), originals[1]['body'].index('## 归档阶段')),
        ])

    def test_selected_section_keeps_substantive_preamble_once_as_a_dependency(self):
        originals = [unit('guide', '适用范围：内部记录。\n未经批准不得对外发布。\n\n'
                          '## 接收阶段\n核对输入记录。\n\n## 归档阶段\n保存审批凭据。\n')]
        adaptive, claims, _, _, _ = self.generate(originals, (0, -1))
        selected_group = next(g for g in adaptive['groups'] if g['title'] == '归档阶段')
        dependencies = selected_group['required_group_ids']
        self.assertTrue(dependencies)
        self.assertTrue(set(dependencies) <= set(adaptive['selection']['included_group_ids']))
        text = claim_text(claims)
        self.assertEqual(text.count('未经批准不得对外发布。'), 1)
        self.assertIn('保存审批凭据。', text)
        self.assertNotIn('核对输入记录。', text)
        body = originals[0]['body']
        self.assert_exact_selected_ranges(originals, adaptive, [
            ('guide', 0, body.index('## 接收阶段')),
            ('guide', body.index('## 归档阶段'), len(body)),
        ])
        blocked, blocked_claims, _, _, _ = self.generate(originals, (-1,))
        self.assertEqual(blocked['status'], 'partial')
        self.assertEqual(blocked_claims, [])
        self.assertTrue(any('共同前提被排除' in issue['message'] for issue in blocked['issues']))

    def test_selected_whole_section_keeps_exact_citations_and_original_snapshot(self):
        originals = [unit('long', '## 处理流程\n1. 留存输入。\n2. 核对数值2.5与原件一致。\n'
                          + ''.join(f'第{i}项说明必须保留完整条件。' for i in range(20))
                          + '\n3. 完成人工复核。\n否则保留草稿，不得执行。\n\n'
                          '## 另外流程\n只处理另一类请求。\n')]
        before = copy.deepcopy(originals)
        workspace = self.workspace(SelectingEngine(self.model))
        report = self.build_report(workspace, originals)
        self.assertEqual(originals, before)
        self.assertEqual(report['units'], before)
        self.assert_exact_selected_ranges(originals, report['adaptive'], [
            ('long', 0, originals[0]['body'].index('## 另外流程')),
        ])
        used = {ref for fact in report['adaptive']['facts'] for ref in fact['refs']}
        refs = {ref['id']: ref for ref in report['refs']}
        self.assertTrue(used)
        for ref_id in used:
            self.assertTrue(valid_ref(refs[ref_id], report['units']))
        output = export_html(report)
        self.assertIn('否则保留草稿，不得执行。', output)
        self.assertIn('核对数值2.5与原件一致。', output)
        self.assertNotIn('只处理另一类请求', output)
        for ref_id in used:
            self.assertIn(f'id="source-{ref_id}"', output)

    def test_no_match_can_be_saved_reopened_rendered_exported_and_cached(self):
        originals = mixed_source()
        before = copy.deepcopy(originals)
        workspace = self.workspace(SelectingEngine(self.model, 'none'))
        report = self.build_report(workspace, originals)
        adaptive = report['adaptive']
        self.assertEqual(adaptive['status'], 'complete')
        self.assertEqual(adaptive['state'], 'no_match')
        self.assertEqual(adaptive['selection']['status'], 'no_match')
        self.assertEqual(report['claims'], [])
        self.assertEqual(adaptive['chapters'], [])
        workspace.write(report)
        saved = workspace.read(report['id'])
        self.assertEqual(saved['units'], before)
        for output in (render_report(saved), export_html(saved)):
            self.assertIn('未找到符合整理要求的内容', output)
            self.assertNotIn('断开电源', output)
            self.assertNotIn('id="source-', output)
        cached_engine = SelectingEngine(self.model, 'none')
        cached = self.build_report(self.workspace(cached_engine), originals)
        self.assertEqual(cached['adaptive']['state'], 'no_match')
        self.assertEqual(cached_engine.requests, [])
        self.assertEqual(cached['generation']['cache_reused_calls'], 1)
        self.assertEqual(originals, before)

    def test_unknown_group_remains_a_failure_instead_of_a_valid_no_match(self):
        workspace = self.workspace(SelectingEngine(self.model, 'unknown'))
        report = self.build_report(workspace, mixed_source())
        self.assertEqual(report['adaptive']['status'], 'partial')
        self.assertEqual(report['adaptive']['selection']['status'], 'failed')
        self.assertNotEqual(report['adaptive']['state'], 'no_match')
        self.assertEqual(report['claims'], [])
        self.assertTrue(report['adaptive']['issues'])
        self.assertNotIn('未找到符合整理要求的内容', render_report(report))

    def test_all_sections_preserve_contents_and_account_for_only_structural_omissions(self):
        originals = [unit('all', '---\ntitle: "操作资料"\ntags: [记录, 核对]\n---\n'
                          '# 操作资料\n\n## 第一流程\n原始条件甲。\n[[目录导航]]\n'
                          '## 第二流程\n原始条件乙，内嵌[[业务术语]]应保留。\n')]
        before = copy.deepcopy(originals)
        adaptive, claims, _, _, _ = self.generate(originals, 'all')
        text = claim_text(claims)
        self.assertIn('原始条件甲。', text)
        self.assertIn('原始条件乙，内嵌[[业务术语]]应保留。', text)
        self.assertNotIn('title:', text)
        self.assertNotIn('[[目录导航]]', text)
        self.assertNotIn('# 操作资料', text)
        self.assertEqual(adaptive['selection']['excluded_group_ids'], [])
        reasons = {span['reason'] for span in adaptive['structural_omissions']}
        self.assertTrue({'yaml_frontmatter', 'standalone_wiki_navigation', 'document_title'} <= reasons)
        body = originals[0]['body']
        accounted = set()
        for fact in adaptive['facts']:
            for span in fact['evidence']:
                self.assertEqual(span['quote'], body[span['start']:span['end']])
                accounted.update(range(span['start'], span['end']))
        for span in adaptive['structural_omissions']:
            self.assertEqual(span['text'], body[span['start']:span['end']])
            accounted.update(range(span['start'], span['end']))
        self.assertTrue(all(i in accounted for i, char in enumerate(body) if not char.isspace()))
        self.assertEqual(originals, before)

    def test_saved_section_outline_rebinds_citations_without_a_new_model_call(self):
        originals = mixed_source()
        prepared, _, _, _, _ = self.generate(originals, (1,), stop_after_outline=True)
        self.assertEqual(prepared['status'], 'outline')
        outline = copy.deepcopy(prepared['outline'])
        outline[0]['title'] = '人工确认的资料章节'
        engine = SelectingEngine(self.model, (1,))
        adaptive, claims, receipt, _, register = self.generate(
            originals, engine=engine, register=Registry(100),
            prepared=prepared, outline_override=outline)
        self.assertEqual(engine.requests, [])
        self.assertEqual(receipt['calls'], 0)
        self.assertEqual(receipt['prepared_reused_calls'], 1)
        self.assertEqual(adaptive['selection'], prepared['selection'])
        self.assertEqual(adaptive['chapters'][0]['title'], '人工确认的资料章节')
        self.assertTrue(claims)
        self.assertTrue(all(ref > register.offset for claim in claims for ref in claim['refs']))
        self.assertNotIn('断开电源', claim_text(claims))
        self.assertNotIn('审批完成后方可登记', claim_text(claims))

    def test_chapter_limit_does_not_limit_selected_source_sections(self):
        originals = [unit('many', '\n'.join(
            f'## 流程{i}\n第{i}项记录必须完整保留。\n' for i in range(1, 10)))]
        adaptive, _, _, _, _ = self.generate(originals, 'all')
        groups = adaptive['groups']; facts = adaptive['all_inventory_facts']
        self.assertEqual(len(groups), 9)
        response = {'text': json.dumps({'scope':dict(include='全部',exclude=''),'decisions':[dict(group=g['id'],decision='include',reason='全部流程') for g in groups],'chapters': [
            {'title': f'流程{i+1}', 'groups': [g['id']]} for i, g in enumerate(groups[:7])
        ] + [{'title': '后续流程', 'groups': [g['id'] for g in groups[7:]]}]}, ensure_ascii=False),
                    'finish_reason': 'stop'}
        outline, selected, selection = brief.parse_organization(response, groups, facts)
        self.assertEqual(len(outline), 8)
        self.assertEqual(len(selection['included_group_ids']), 9)
        self.assertEqual(len(selected), len(facts))
        _, schema = brief.organization_request('流程', '全部流程', groups, originals, [])
        self.assertEqual(schema['properties']['chapters']['maxItems'], 8)
        self.assertEqual(schema['properties']['chapters']['items']['properties']['groups']['maxItems'], 9)

    def test_scope_decisions_must_cover_all_groups_and_bind_to_chapters(self):
        adaptive, _, _, _, _ = self.generate(mixed_source(), (1,))
        groups=adaptive['groups']; facts=adaptive['all_inventory_facts']
        response=adaptive['organization_record']['response']
        obj=json.loads(response['text'])
        for fault in ('missing', 'contradiction', 'duplicate', 'uncertain'):
            changed=copy.deepcopy(obj)
            if fault=='missing':changed['decisions'].pop()
            elif fault=='contradiction':changed['decisions'][0]['decision']='include'
            elif fault=='duplicate':changed['decisions'][0]=copy.deepcopy(changed['decisions'][1])
            else:changed['decisions'][0]['decision']='uncertain'
            with self.subTest(fault=fault),self.assertRaises(ValueError):
                brief.parse_organization(dict(response,text=json.dumps(changed)),groups,facts)

    def test_cross_chapter_duplicates_with_new_groups_preserve_all_facts_and_refs(self):
        originals = mixed_source()
        adaptive, _, _, _, _ = self.generate(originals, 'all')
        groups = adaptive['groups']; facts = adaptive['all_inventory_facts']
        ids = [g['id'] for g in groups]
        response = copy.deepcopy(adaptive['organization_record']['response'])
        obj = json.loads(response['text'])
        obj['chapters'] = [
            dict(title='第一组资料', groups=[ids[0]]),
            dict(title='接续资料', groups=[ids[0], ids[1]]),
            dict(title='重复视角', groups=[ids[0], ids[1]]),
            dict(title='最后资料', groups=[ids[1], ids[2]]),
        ]
        response['text'] = json.dumps(obj, ensure_ascii=False)
        before = copy.deepcopy((response, groups, facts))
        outline, selected, selection = brief.parse_organization(response, groups, facts)
        self.assertEqual([ch['id'] for ch in outline], ['C1', 'C2', 'C4'])
        self.assertEqual([ch['title'] for ch in outline], ['第一组资料', '接续资料', '最后资料'])
        self.assertEqual(selected, facts)
        self.assertEqual(selection['included_group_ids'], ids)
        self.assertCountEqual([fid for ch in outline for fid in ch['fact_ids']], [f['id'] for f in facts])
        normalized = selection['chapter_normalization']
        self.assertEqual(normalized['original_chapters'], obj['chapters'])
        self.assertEqual(normalized['removed_chapter_ids'], ['C3'])
        self.assertEqual([ch['groups'] for ch in normalized['normalized_chapters']], [[gid] for gid in ids])
        self.assertEqual(normalized['duplicate_references'], [
            dict(group_id=ids[0], kept_in_chapter_id='C1', removed_from_chapter_id='C2'),
            dict(group_id=ids[0], kept_in_chapter_id='C1', removed_from_chapter_id='C3'),
            dict(group_id=ids[1], kept_in_chapter_id='C2', removed_from_chapter_id='C3'),
            dict(group_id=ids[1], kept_in_chapter_id='C2', removed_from_chapter_id='C4'),
        ])
        chapters = report_compiled.compile_chapters(outline, selected, adaptive['source_blocks'], originals, self.focus)
        paragraphs = [p for ch in chapters for p in ch['paragraphs']]
        self.assertCountEqual([fid for p in paragraphs for fid in p['fact_ids']], [f['id'] for f in facts])
        for fact in facts:
            paragraph = next(p for p in paragraphs if fact['id'] in p['fact_ids'])
            self.assertEqual(paragraph['text'], fact['text'])
            self.assertEqual(paragraph['refs'], fact['refs'])
        self.assertEqual((response, groups, facts), before)

    def test_cross_chapter_recovery_does_not_swallow_invalid_groups_or_scope(self):
        adaptive, _, _, _, _ = self.generate(mixed_source(), 'all')
        groups = adaptive['groups']; facts = adaptive['all_inventory_facts']
        ids = [g['id'] for g in groups]
        response = adaptive['organization_record']['response']
        obj = json.loads(response['text'])
        obj['chapters'].append(dict(title='重复的资料', groups=[ids[0]]))
        for fault in ('unknown', 'excluded', 'missing', 'uncertain', 'within_chapter', 'invalid_title', 'dependency'):
            changed = copy.deepcopy(obj); candidate_groups = copy.deepcopy(groups)
            if fault == 'unknown':
                changed['chapters'][1]['groups'].append('G9999')
            elif fault == 'excluded':
                changed['decisions'][0]['decision'] = 'exclude'
            elif fault == 'missing':
                changed['chapters'][0]['groups'].remove(ids[2])
            elif fault == 'uncertain':
                changed['decisions'][2]['decision'] = 'uncertain'
                changed['chapters'][0]['groups'].remove(ids[2])
            elif fault == 'within_chapter':
                changed['chapters'][1]['groups'].append(ids[0])
            elif fault == 'invalid_title':
                changed['chapters'][1]['title'] = None
            else:
                candidate_groups[2]['required_group_ids'] = [ids[0]]
                changed['decisions'][0]['decision'] = 'exclude'
                changed['chapters'][0]['groups'] = ids[1:]
                changed['chapters'][1]['groups'] = [ids[2]]
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                brief.parse_organization(dict(response, text=json.dumps(changed)), candidate_groups, facts)

    def test_valid_unique_chapters_keep_structure_without_normalization_metadata(self):
        adaptive, _, _, _, _ = self.generate(mixed_source(), 'all')
        groups = adaptive['groups']; facts = adaptive['all_inventory_facts']
        response = copy.deepcopy(adaptive['organization_record']['response'])
        obj = json.loads(response['text'])
        obj['chapters'] = [dict(title='章节' + str(i), groups=[g['id']]) for i, g in enumerate(groups, 1)]
        response['text'] = json.dumps(obj, ensure_ascii=False)
        before = copy.deepcopy(response)
        outline, selected, selection = brief.parse_organization(response, groups, facts)
        self.assertEqual(outline, [dict(id=f'C{i}', title=f'章节{i}', purpose='', fact_ids=g['fact_ids'])
                                   for i, g in enumerate(groups, 1)])
        self.assertEqual(selected, facts)
        self.assertNotIn('chapter_normalization', selection)
        self.assertEqual(selection['group_decisions'], obj['decisions'])
        self.assertEqual(response, before)


if __name__ == '__main__':
    unittest.main()
