# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Explicit request boundaries with synthetic saved data; no model generation.

These mechanism checks do not measure business factual accuracy or import/OCR
quality. Production candidate construction uses deterministic fixture vectors.
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import numpy as np

from pocket_memory.rag import RagPipeline
from pocket_memory.rag.query import QueryResolver
from pocket_memory.rag.ask_scope import (
    AskScope, ScopedNotes, conflicting_source_title,
    resolve_ask_scope, source_catalog,
)
from pocket_memory.rag.development import LiveRagPipeline
from pocket_memory.rag.retained.pilot import load_nodes
from pocket_memory.storage import NoteStore
import test_retained_development as fixture_http


BODY = '风险一：资料需要分类。\n\n风险二：扫描结果需要人工复核。\n\n风险三：首次加载较慢。'
VECTOR = np.asarray([1.0] + [0.0] * 511, dtype=np.float32)


def index_note(store, note):
    chunks = store.replace_note_chunks(note.id, [dict(title=note.title, text=note.content, kind='content')])
    for chunk in chunks:
        store.upsert_note_chunk_embedding(chunk.id, VECTOR)


class AskScopeMechanismTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / 'library')
        self.main = self.store.create_note('项目复盘', BODY)
        self.child = self.store.create_note('风险备注', '仅在人工签字后使用。', parent_note_id=self.main.id, tab_name='备注')
        self.other = self.store.create_note('其他项目', '风险一：范围外的独有风险。')
        for note in (self.main, self.child, self.other):
            index_note(self.store, note)
        encoded = base64.b64encode('文档独有内容'.encode()).decode()
        self.document = self.store.create_document_from_data_url('文档资料.txt', 'data:text/plain;base64,' + encoded)
        for chunk in self.store.replace_document_chunks(self.document.id, [dict(text='文档独有内容')]):
            self.store.upsert_document_chunk_embedding(chunk.id, VECTOR)
        self.embedding = SimpleNamespace(engine=SimpleNamespace(embed=lambda text: VECTOR),
                                         drain=lambda timeout: True, index_errors=lambda: [])
        self.native = SimpleNamespace(available=False, unavailable_reason='test forbids generation',
                                      model_path='fixture.gguf', n_ctx=3072)
        self.pipeline = LiveRagPipeline(RagPipeline(self.store, self.embedding), self.native)

    def tearDown(self):
        self.pipeline.close()
        self.store.close()
        self.temp.cleanup()

    def scope(self, mode='current', notes=None, docs=None):
        return resolve_ask_scope(self.store, dict(mode=mode,
            note_ids=[self.main.id] if notes is None else notes, document_ids=docs or []))

    def test_child_id_canonicalizes_to_whole_family(self):
        scope = self.scope(notes=[self.child.id])
        self.assertEqual(scope.note_ids, (self.main.id,))
        self.assertEqual(scope.expanded_note_ids, (self.main.id, self.child.id))
        self.assertEqual(scope.payload()['mode'], 'current')
        self.assertIn('全部页签', scope.payload()['label'])

    def test_invalid_empty_stale_and_mixed_current_never_widen(self):
        invalid = [dict(mode='selected'), dict(mode='current', note_ids=[999999]),
                   dict(mode='selected', note_ids=['1']), dict(mode='selected', note_ids=[True]),
                   dict(mode='all', note_ids=[self.main.id]), dict(mode='bogus'), dict(mode=[]), dict(mode={}),
                   dict(mode='current', note_ids=[self.main.id, self.other.id]),
                   dict(mode='current', document_ids=[self.document.id]),
                   dict(mode='selected', note_ids=[self.main.id], titles=['other'])]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                resolve_ask_scope(self.store, raw)

    def test_same_numeric_document_and_note_ids_are_distinct(self):
        self.assertEqual(self.main.id, self.document.id)
        scope = self.scope('selected', notes=[], docs=[self.document.id])
        nodes, notes, documents = load_nodes(self.store.db_path, scope)
        self.assertEqual(notes, {})
        self.assertEqual(set(documents), {self.document.id})
        self.assertTrue(nodes)
        self.assertTrue(all(n.metadata['source_type'] == 'document' for n in nodes))
        self.assertFalse(scope.permits('note', self.main.id))

    def test_scope_is_applied_before_nodes_ocr_units_and_registry(self):
        self.store.update_ocr(self.main.id, 'done', '范围内OCR', '')
        self.store.update_ocr(self.other.id, 'done', '范围外OCR', '')
        scope = self.scope()
        candidate = self.pipeline._refresh(scope)
        expected = {self.main.id, self.child.id}
        self.assertEqual(set(candidate.notes), expected)
        self.assertEqual(candidate.documents, {})
        self.assertEqual(set(candidate.bridge.ocr), expected)
        self.assertEqual(set(candidate.evidence_registry.notes), expected)
        self.assertEqual(set(candidate.evidence_registry.ocr), expected)
        self.assertEqual(set(candidate.stable.legacy.coverage_ocr_originals), expected)
        self.assertTrue(all(n.metadata['source_type'] == 'note' and
                            n.metadata['source_id'] in expected for n in candidate.nodes))
        for ident in expected:
            units = candidate.bridge.units(f'note:{ident}')
            self.assertTrue(all(n.metadata['source_id'] in expected for n in units))
        with self.assertRaises(KeyError):
            candidate.bridge.units(f'note:{self.other.id}')
        self.assertIsNone(candidate.enumerated_list.prepare('项目有哪些风险'))
        self.assertEqual(candidate.enumerated_list.audit['reason'], 'ocr_scope_requires_existing_route')
        with self.assertRaisesRegex(RuntimeError, 'escaped'):
            self.pipeline._check_scope_sources([dict(source_type='note', source_id=self.other.id)])
        with self.assertRaisesRegex(RuntimeError, 'escaped'):
            self.pipeline._check_scope_sources([dict(source_type='document', source_id=self.main.id)])

    def test_current_generic_list_is_complete_and_preserves_conditions(self):
        for question in ('项目有哪些风险', '这个项目有什么风险'):
            with self.subTest(question=question):
                result = self.pipeline.route(question, ask_scope=self.scope(notes=[self.child.id]))
                self.assertTrue(result.chunks)
                answer = self.pipeline.answer(question)
                final, sources = self.pipeline.enforce_answer_contract(question, answer, [])
                for fact in ('资料需要分类', '扫描结果需要人工复核', '首次加载较慢', '仅在人工签字后使用'):
                    self.assertIn(fact, final)
                self.assertNotIn('独有风险', final)
                self.assertTrue(all(s['source_id'] in {self.main.id, self.child.id} for s in sources))

    def test_switching_scope_reuses_bounded_snapshots_and_one_model_owner(self):
        first = self.pipeline._refresh(self.scope())
        second = self.pipeline._refresh(self.scope('selected', [self.other.id]))
        self.assertIsNot(first, second)
        self.assertIs(first.context.engine, second.context.engine)
        self.assertIs(first.context.reranker_owner, second.context.reranker_owner)
        self.assertNotEqual(first.context.cache_dir, second.context.cache_dir)
        self.assertIs(first, self.pipeline._refresh(self.scope()))
        self.pipeline._refresh(self.scope('selected', [], [self.document.id]))
        self.pipeline._refresh(AskScope())
        self.assertEqual(len(self.pipeline._candidates), 3)
        self.assertTrue(second._closed)
        self.assertIs(self.pipeline.native, self.native)

    def test_deleted_selection_fails_even_if_snapshot_cached(self):
        scope = self.scope('selected', [self.other.id])
        self.pipeline._refresh(scope)
        self.store.delete_note(self.other.id)
        with self.assertRaises(ValueError):
            self.pipeline._refresh(scope)

    def test_all_uses_full_saved_inputs_and_known_title_request_stays_direct(self):
        nodes, notes, documents = load_nodes(self.store.db_path)
        self.assertEqual(set(notes), {self.main.id, self.child.id, self.other.id})
        self.assertEqual(set(documents), {self.document.id})
        self.assertEqual(len(nodes), 4)
        q = '《项目复盘》项目有哪些风险'
        self.pipeline.route(q)
        answer = self.pipeline.answer(q)
        self.assertIn('扫描结果需要人工复核', answer)

    def test_foreign_named_source_is_not_substituted(self):
        self.assertTrue(conflicting_source_title(self.store, '《其他项目》有哪些风险？', self.scope()))
        self.assertFalse(conflicting_source_title(self.store, '《项目复盘》有哪些风险？', self.scope()))
        self.assertFalse(conflicting_source_title(self.store, '《其他项目》有哪些风险？', AskScope()))

    def test_catalog_is_metadata_only_and_main_notes_only(self):
        catalog = source_catalog(self.store)
        self.assertEqual({n['id'] for n in catalog['notes']}, {self.main.id, self.other.id})
        self.assertEqual(catalog['notes'][0]['tab_count'], 2)
        self.assertNotIn('content', json.dumps(catalog))
        self.assertEqual(catalog['documents'][0]['id'], self.document.id)

    def test_topic_search_is_not_proof_of_a_missing_referent(self):
        for q in ('项目有哪些风险', '合同的付款条件是什么', '方案需要哪些资料'):
            self.assertFalse(RagPipeline.requires_clarification(q), q)
        for q in ('甲项目有哪些风险', '项目甲有哪些风险', '比较甲项目和乙项目的风险',
                  '所有项目有哪些风险', '《项目复盘》有哪些风险', '全库项目风险概览',
                  '项目风险扫描Agent 的核心功能和适用场景是什么'):
            self.assertFalse(RagPipeline.requires_clarification(q), q)

    def test_single_selected_table_does_not_identify_a_subject_row(self):
        table = self.store.create_note('助手清单', '| Agent名称 | 核心功能 | 适用场景 |\n|---|---|---|\n| 资料检查助手 | 整理资料 | 项目复盘 |\n| 月报编写助手 | 生成报告 | 月报 |')
        index_note(self.store, table)
        for scope in (self.scope(notes=[table.id]), AskScope()):
            for q in ('核心功能和适用场景是什么', 'Agent的核心功能和适用场景是什么'):
                with self.subTest(scope=scope.mode, question=q):
                    self.pipeline.route(q, ask_scope=scope)
                    answer = self.pipeline.answer(q)
                    self.assertIn('明确对象', answer)
                    self.assertIn('助手清单', answer)
                    self.assertEqual(self.pipeline.local.response['sources'], [])
                    self.assertEqual(self.pipeline.local.response['trace']['generation_calls'], 0)
        candidate = self.pipeline._refresh(AskScope())
        self.assertIsNone(candidate.prepare_direct('所有Agent的核心功能和适用场景是什么'))
        self.assertIsNone(candidate.prepare_direct('未收录月球助手的核心功能和适用场景是什么'))
        direct = candidate.prepare_direct('资料检查助手的核心功能和适用场景是什么')
        self.assertIn('整理资料', direct['direct_record_response']['answer'])

    def test_short_table_identities_do_not_enable_guessed_direct_projection(self):
        # Retain the original unsupported three-character identity fixture.
        # It may fall back to evidence QA; this test does not claim the table
        # parser now supports shorter row identities or proves final QA quality.
        table = self.store.create_note('短名称清单', '| Agent名称 | 核心功能 | 适用场景 |\n|---|---|---|\n| 甲助手 | 整理资料 | 项目复盘 |\n| 乙助手 | 生成报告 | 月报 |')
        index_note(self.store, table)
        candidate = self.pipeline._refresh(self.scope(notes=[table.id]))
        self.assertIsNone(candidate.prepare_direct('核心功能和适用场景是什么'))


class AskScopeHttpTests(unittest.TestCase):
    setUp = fixture_http.RetainedDevelopmentHttpTests.setUp
    tearDown = fixture_http.RetainedDevelopmentHttpTests.tearDown
    request = fixture_http.RetainedDevelopmentHttpTests.request

    def test_named_entities_and_topic_variants_reach_evidence_without_rewriting(self):
        self.store.create_note('资料清单', BODY)
        questions = ('项目有哪些风险', '项目风险扫描Agent 的核心功能和适用场景是什么',
                     '请问，项目有什么风险呢？', '项目面临哪些风险？',
                     '请问项目风险扫描 Agent 的核心功能、适用场景分别是什么？',
                     '合同风险复核助手的核心功能是什么', '流程风险扫描器适合哪些场景')
        for question in questions:
            with self.subTest(question=question):
                status, payload = self.request('/api/ask', 'POST', dict(question=question))
                self.assertEqual(status, 200)
                self.assertEqual(payload['kind'], 'qa')
                self.assertTrue(payload['sources'])
                self.assertEqual(self.server.rag_pipeline.last['question'],
                                 QueryResolver.resolve(question).evidence_question)

    def test_true_deictic_and_action_missing_targets_still_clarify(self):
        self.store.create_note('甲项目', BODY)
        self.store.create_note('乙项目', '风险一：来源不同，不得默选甲。')
        for question in ('这个项目有哪些风险？', '这个Agent的核心功能和适用场景是什么？',
                         '这份合同怎么付款？', '为什么不能删除'):
            with self.subTest(question=question):
                status, payload = self.request('/api/ask', 'POST', dict(question=question))
                self.assertEqual(status, 200)
                self.assertEqual(payload['kind'], 'clarification')
                self.assertEqual(payload['sources'], [])
        self.assertEqual(self.factory.instances, [])

    def test_broad_topic_with_two_projects_is_not_silently_restricted_to_one(self):
        a=self.store.create_note('甲项目', BODY)
        b=self.store.create_note('乙项目', '风险一：乙的独有风险。')
        status,payload=self.request('/api/ask','POST',dict(question='项目有哪些风险'))
        self.assertEqual(status,200)
        self.assertEqual(payload['kind'],'qa')
        self.assertEqual({s['source_id'] for s in payload['sources']},{a.id,b.id})
        self.assertEqual(payload['data']['ask_scope']['mode'],'all')

    def test_invalid_scope_is_http_400_before_model_or_retrieval(self):
        for scope in (None, {}, {'mode': []}, {'mode': {}}, {'mode': 'selected', 'note_ids': []}, {'mode': 'current', 'note_ids': [99999]}):
            status, payload = self.request('/api/ask', 'POST', dict(question='项目有哪些风险', scope=scope))
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload['code'], 'invalid_ask_scope')
        self.assertEqual(self.factory.instances, [])

    def test_full_library_topic_reaches_evidence_and_scoped_stats_do_not_leak(self):
        note = self.store.create_note('项目复盘', BODY)
        status, payload = self.request('/api/ask', 'POST', dict(question='项目有哪些风险'))
        self.assertEqual(status, 200)
        self.assertEqual(payload['kind'], 'qa')
        self.assertEqual({s['source_id'] for s in payload['sources']}, {note.id})
        self.assertEqual(payload['data']['ask_scope']['mode'], 'all')
        self.assertEqual(len(self.factory.instances), 1)
        status, payload = self.request('/api/ask', 'POST', dict(question='一共几个笔记',
            scope=dict(mode='current', note_ids=[note.id])))
        self.assertEqual(status, 200)
        self.assertEqual(payload['kind'], 'clarification')
        self.assertIn('暂不支持', payload['answer'])
        self.assertEqual(payload['data']['ask_scope']['note_ids'], [note.id])
        self.assertEqual(len(self.factory.instances), 1)

    def test_sse_has_scope_in_initial_and_final_events(self):
        note = self.store.create_note('项目复盘', BODY)
        body = dict(question='一共几个笔记', stream=True, scope=dict(mode='current', note_ids=[note.id]))
        req = Request(self.base_url + '/api/ask', data=json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=8) as response:
            events = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith('data: ')]
        self.assertEqual([e['type'] for e in events], ['sources', 'done'])
        for event in events:
            self.assertEqual(event['data']['ask_scope']['note_ids'], [note.id])

    def test_catalog_and_outside_title_clarification(self):
        selected = self.store.create_note('所选资料', BODY)
        other = self.store.create_note('范围外资料', '不应检索')
        status, catalog = self.request('/api/ask/sources')
        self.assertEqual(status, 200)
        self.assertIn(selected.id, [n['id'] for n in catalog['notes']])
        status, payload = self.request('/api/ask', 'POST', dict(question='《范围外资料》有哪些风险',
            scope=dict(mode='current', note_ids=[selected.id])))
        self.assertEqual(status, 200)
        self.assertIn('不在本次问答范围', payload['answer'])
        self.assertEqual(payload['sources'], [])
        self.assertEqual(self.factory.instances, [])


if __name__ == '__main__':
    unittest.main()
