# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Zero-model development wiring checks; fixture answers are not RAG quality evidence."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from pocket_memory.config import AppConfig
from pocket_memory.embedding import EmbeddingService
from pocket_memory.intelligence import IntelligenceService
from pocket_memory.ocr import OcrService
from pocket_memory.rag import RagPipeline
from pocket_memory.rag.development import LiveCandidateEngine, LiveRagPipeline
from pocket_memory.server import PocketMemoryServer
from pocket_memory.storage import NoteStore


class _NoModel:
    available = False
    unavailable_reason = "Zero-model integration fixture"


class _FixtureEmbedding:
    available = True
    unavailable_reason = ""

    def embed(self, text):
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float32)


class _FixtureTextEngine:
    available = True
    unavailable_reason = ""
    model_loaded = False

    def session(self):
        return nullcontext()

    def answer(self, question, contexts, history=None):
        raise AssertionError("Fixture request bypassed LiveCandidateEngine")

    def _clean_answer(self, text):
        return text


class _FixtureRegistry:
    def stage(self, prepared):
        return copy.deepcopy(prepared["sources"])


class _FixtureFactory:
    def __init__(self):
        self.instances = []
        self.after_snapshot = None
        self.before_answer = None
        self.answer_entered = threading.Event()
        self.overlapping_stage = threading.Event()

    def __call__(self, context):
        candidate = _FixtureCandidate(context, self)
        self.instances.append(candidate)
        if self.after_snapshot is not None:
            self.after_snapshot(context)
        return candidate


class _FixtureCandidate:
    """A deliberately shared single-stage slot, like the retained candidate.

    The fake only echoes saved fixture text. It does not claim retrieval quality,
    generate a factual answer, load a model, or perform an external request.
    """

    def __init__(self, context, factory):
        self.snapshot = [copy.deepcopy(note.as_dict())
                         for note in context.store.list_notes(limit=10000)]
        self.factory = factory
        self.evidence_registry = _FixtureRegistry()
        self.closed = False
        self.staged = None
        self.answer_active = threading.Event()

    def stage(self, question):
        if self.closed:
            raise AssertionError("Closed snapshot reused")
        if self.answer_active.is_set():
            self.factory.overlapping_stage.set()
        selected = [note for note in self.snapshot if note["title"] in question]
        selected = selected or self.snapshot
        sources = [{"source_type": "note", "source_id": note["id"],
                    "title": note["title"], "evidence": note["content"],
                    "excerpt": note["content"], "location": "正文", "index": index}
                   for index, note in enumerate(selected, 1)]
        self.staged = (question, sources)
        return {"mode": "zero-model-wiring-fixture", "sources": sources}

    def ask(self, question):
        self.answer_active.set()
        self.factory.answer_entered.set()
        try:
            if self.factory.before_answer is not None:
                self.factory.before_answer(question)
            if self.staged is None or self.staged[0] != question:
                raise AssertionError("Concurrent request overwrote staged evidence")
            sources = copy.deepcopy(self.staged[1])
            self.staged = None
            content = " | ".join(source["evidence"] for source in sources)
            return {"answer": f"接线夹具：{question} | {content}", "sources": sources}
        finally:
            self.answer_active.clear()

    def close(self):
        self.closed = True
        self.staged = None


class RetainedDevelopmentRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = NoteStore(self.root / "first-library")
        self.embedding = EmbeddingService(self.store, _NoModel())
        self.factory = _FixtureFactory()
        self.native = _FixtureTextEngine()
        self.pipeline = LiveRagPipeline(
            RagPipeline(self.store, self.embedding), self.native, self.factory)
        self.engine = LiveCandidateEngine(self.native, self.pipeline)

    def tearDown(self):
        self.pipeline.close()
        self.embedding.shutdown(wait=True)
        self.store.close()
        self.temp.cleanup()

    def ask(self, question="项目甲的测试标记是什么？"):
        result = self.pipeline.route(question)
        sources = self.pipeline.source_payloads(question, result.chunks)
        contexts = self.pipeline.contexts(result.chunks, sources)
        answer = self.engine.answer(question, contexts)
        return self.pipeline.enforce_answer_contract(question, answer, sources)

    def test_same_second_edit_changes_revision_and_rebuilds_snapshot(self):
        with patch.object(NoteStore, "_now", return_value="2026-10-28T12:00:00+00:00"):
            original = self.store.create_note("项目甲", "旧标记")
            initial_revision = self.store.source_revision()
            first_answer, _ = self.ask()
            self.ask()
            self.assertEqual(len(self.factory.instances), 1, "Unchanged input must reuse the snapshot")
            old_candidate = self.factory.instances[0]
            edited = self.store.update_note(original.id, content="新标记")
            self.assertEqual(original.updated_at, edited.updated_at)
            self.assertNotEqual(initial_revision, self.store.source_revision())
            answer, sources = self.ask()
        self.assertIn("旧标记", first_answer)
        self.assertIn("新标记", answer)
        self.assertNotIn("旧标记", answer)
        self.assertEqual(sources[0]["source_id"], original.id)
        self.assertEqual(len(self.factory.instances), 2)
        self.assertTrue(old_candidate.closed)

    def test_delete_invalidates_snapshot_and_removed_note_cannot_be_cited(self):
        deleted = self.store.create_note("项目甲", "即将删除的标记")
        kept = self.store.create_note("项目乙", "保留标记")
        self.ask("项目甲的测试标记是什么？")
        old_candidate = self.factory.instances[-1]
        previous = self.store.source_revision()
        self.store.delete_note(deleted.id)
        self.assertNotEqual(previous, self.store.source_revision())
        answer, sources = self.ask("项目乙的测试标记是什么？")
        self.assertTrue(old_candidate.closed)
        self.assertEqual([source["source_id"] for source in sources], [kept.id])
        self.assertNotIn("即将删除", answer)

    def test_model_identity_change_rebuilds_formatter_owner(self):
        self.store.create_note("项目甲", "保留标记")
        self.native.model_path = Path('old.gguf')
        self.ask()
        previous = self.factory.instances[-1]
        self.native.model_path = Path('new.gguf')
        answer, _ = self.ask()
        self.assertTrue(previous.closed)
        self.assertEqual(len(self.factory.instances), 2)
        self.assertIn('保留标记', answer)

    def test_empty_index_returns_no_context_without_preparing(self):
        self.store.create_note("项目甲", "保留标记")
        self.ask()
        self.factory.instances[-1].nodes = []
        result = self.pipeline.route('什么资料可以用？')
        self.assertEqual(result.chunks, [])
        self.assertIn('没有可检索', result.empty_answer)

    def test_switching_library_rebuilds_even_when_note_ids_are_reused(self):
        original_dir = self.store.data_dir
        original = self.store.create_note("项目甲", "原库标记")
        self.ask()
        before = self.store.source_revision()
        self.store.switch_data_dir(self.root / "second-library")
        replacement = self.store.create_note("项目甲", "新库标记")
        self.assertEqual(original.id, replacement.id, "Fixture must exercise reused source IDs")
        self.assertNotEqual(before[0], self.store.source_revision()[0])
        answer, sources = self.ask()
        self.assertIn("新库标记", answer)
        self.assertNotIn("原库标记", answer)
        self.assertEqual([source["source_id"] for source in sources], [replacement.id])
        self.store.switch_data_dir(original_dir)
        restored_answer, _ = self.ask()
        self.assertIn("原库标记", restored_answer)
        self.assertNotIn("新库标记", restored_answer)
        self.assertEqual(len(self.factory.instances), 3)
        self.assertTrue(all(candidate.closed for candidate in self.factory.instances[:-1]))

    def test_source_revision_observes_external_sqlite_commit(self):
        note = self.store.create_note("项目甲", "提交前")
        before = self.store.source_revision()
        other_connection = sqlite3.connect(self.store.db_path)
        try:
            with other_connection:
                other_connection.execute("UPDATE notes SET content = ? WHERE id = ?", ("提交后", note.id))
        finally:
            other_connection.close()
        after = self.store.source_revision()
        self.assertNotEqual(before, after)
        self.assertEqual(before[:2], after[:2], "Other connections must be detected by data_version")

    def test_background_change_during_snapshot_requires_a_fresh_question(self):
        note = self.store.create_note("项目甲", "快照前")
        self.factory.after_snapshot = lambda context: context.store.update_note(note.id, content="快照后")
        with self.assertRaisesRegex(ValueError, "重新提问"):
            self.pipeline.route("项目甲的测试标记是什么？")
        self.assertTrue(self.factory.instances[0].closed)
        self.factory.after_snapshot = None
        answer, _ = self.ask()
        self.assertIn("快照后", answer)
        self.assertNotIn("快照前", answer)


class RetainedDevelopmentHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = NoteStore(self.root / "data")
        config = AppConfig(self.root / "config.json", self.store.data_dir,
                           ai_auto_apply=False, ai_warmup_on_start=False)
        self.embedding = EmbeddingService(self.store, _FixtureEmbedding())
        self.native = _FixtureTextEngine()
        intelligence = IntelligenceService(self.store, self.native, auto_apply=False)
        self.server = PocketMemoryServer(
            ("127.0.0.1", 0), self.store, config, lambda initial: None,
            OcrService(self.store, _NoModel()), self.embedding, intelligence)
        self.unwrapped_pipeline = self.server.rag_pipeline
        self.factory = _FixtureFactory()
        self.pipeline = LiveRagPipeline(self.unwrapped_pipeline, self.native, self.factory)
        self.server.rag_pipeline = self.pipeline
        intelligence.engine = LiveCandidateEngine(self.native, self.pipeline)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server_thread.join(timeout=5)
        self.embedding.drain(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, method="GET", payload=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + path, data=body, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_custom_engine_is_legacy_until_explicitly_wrapped_for_the_test(self):
        self.assertIsInstance(self.unwrapped_pipeline, RagPipeline)
        self.assertNotIsInstance(self.unwrapped_pipeline, LiveRagPipeline)
        self.assertIsInstance(self.server.intelligence_service.engine, LiveCandidateEngine)

    def test_http_create_edit_delete_and_ask_use_the_live_library(self):
        status, created = self.request("/api/notes", "POST", {"title": "项目甲", "content": "初始接线标记"})
        self.assertEqual(status, 201, created)
        note_id = created["id"]
        status, answer = self.request("/api/ask", "POST", {"question": "项目甲的测试标记是什么？"})
        self.assertEqual(status, 200, answer)
        self.assertIn("初始接线标记", answer["answer"])
        self.assertEqual(answer["sources"][0]["source_id"], note_id)
        self.assertEqual(answer["sources"][0]["url"], f"/api/notes/{note_id}")
        status, edited = self.request(f"/api/notes/{note_id}", "PUT", {"content": "修改后接线标记"})
        self.assertEqual(status, 200, edited)
        status, answer = self.request("/api/ask", "POST", {"question": "项目甲的测试标记是什么？"})
        self.assertEqual(status, 200, answer)
        self.assertIn("修改后接线标记", answer["answer"])
        self.assertNotIn("初始接线标记", answer["answer"])
        status, deleted = self.request(f"/api/notes/{note_id}", "DELETE")
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted, {"deleted": note_id})
        status, answer = self.request("/api/ask", "POST", {"question": "项目甲的测试标记是什么？"})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["sources"], [])
        self.assertNotIn("修改后接线标记", answer["answer"])
        self.assertEqual(len(self.factory.instances), 3)

    def test_concurrent_http_asks_do_not_overwrite_the_shared_stage(self):
        first = self.store.create_note("项目甲", "甲专属标记")
        second = self.store.create_note("项目乙", "乙专属标记")
        release_first = threading.Event()
        second_started = threading.Event()

        def before_answer(question):
            if "项目甲" in question and not release_first.wait(timeout=5):
                raise AssertionError("First fixture answer was never released")

        def second_request():
            second_started.set()
            return self.request("/api/ask", "POST", {"question": "项目乙的测试标记是什么？"})

        self.factory.before_answer = before_answer
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_result = executor.submit(self.request, "/api/ask", "POST", {"question": "项目甲的测试标记是什么？"})
            try:
                self.assertTrue(self.factory.answer_entered.wait(timeout=5))
                second_result = executor.submit(second_request)
                self.assertTrue(second_started.wait(timeout=5))
                # A second handler may be waiting for operation_lock; if it gets
                # into the shared candidate during the first answer, detect it.
                self.factory.overlapping_stage.wait(timeout=0.2)
            finally:
                release_first.set()
            first_status, first_answer = first_result.result(timeout=8)
            second_status, second_answer = second_result.result(timeout=8)
        self.assertFalse(self.factory.overlapping_stage.is_set())
        self.assertEqual(first_status, 200, first_answer)
        self.assertEqual(second_status, 200, second_answer)
        self.assertEqual([item["source_id"] for item in first_answer["sources"]], [first.id])
        self.assertEqual([item["source_id"] for item in second_answer["sources"]], [second.id])
        self.assertIn("甲专属标记", first_answer["answer"])
        self.assertNotIn("乙专属标记", first_answer["answer"])
        self.assertIn("乙专属标记", second_answer["answer"])
        self.assertNotIn("甲专属标记", second_answer["answer"])

    def test_background_write_during_http_answer_discards_old_sources(self):
        note = self.store.create_note("项目甲", "回答前旧标记")
        write_finished = threading.Event()
        writer_errors = []

        def writer():
            try:
                if not self.factory.answer_entered.wait(timeout=5):
                    raise AssertionError("Answer did not start")
                self.store.update_note(note.id, content="后台写入新标记")
            except Exception as error:
                writer_errors.append(error)
            finally:
                write_finished.set()

        def wait_for_background_write(question):
            if not write_finished.wait(timeout=5):
                raise AssertionError("Background writer did not finish")

        self.factory.before_answer = wait_for_background_write
        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        try:
            status, answer = self.request("/api/ask", "POST", {"question": "项目甲的测试标记是什么？"})
        finally:
            writer_thread.join(timeout=6)
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(writer_errors, [])
        self.assertEqual(status, 200, answer)
        self.assertIn("重新提问", answer["answer"])
        self.assertEqual(answer["sources"], [])
        self.assertNotIn("回答前旧标记", answer["answer"])
        self.assertTrue(self.factory.instances[0].closed)
        self.factory.before_answer = None
        status, refreshed = self.request("/api/ask", "POST", {"question": "项目甲的测试标记是什么？"})
        self.assertEqual(status, 200, refreshed)
        self.assertIn("后台写入新标记", refreshed["answer"])
        self.assertNotIn("回答前旧标记", refreshed["answer"])


if __name__ == "__main__":
    unittest.main()
