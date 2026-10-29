# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Zero-model clarification selection and HTTP wiring; not answer-quality scoring."""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import numpy as np

from pocket_memory.config import AppConfig
from pocket_memory.embedding import EmbeddingService
from pocket_memory.intelligence import IntelligenceService
from pocket_memory.ocr import OcrService
from pocket_memory.rag import QueryResolver
from pocket_memory.rag.clarification import clarification_sources
from pocket_memory.rag.development import LiveCandidateEngine, LiveRagPipeline
from pocket_memory.server import PocketMemoryServer
from pocket_memory.storage import NoteStore


QUESTION = "核心功能和适用场景是什么"
NAMES = ("会议行动项提取 Agent", "文档归档 Agent", "术语对照 Agent")
ANSWER = (
    "问题未明确指向具体对象，请明确是哪一个Agent。\n"
    "例如：会议行动项提取 Agent、文档归档 Agent、术语对照 Agent等。\n"
    "当前资料中存在多个Agent，无法唯一确定答案。"
)


def source(index, name, *, source_id=55, name_header="Agent 名称"):
    header = f"| 编号 | {name_header} | 核心功能 | 适用场景 |"
    row = f"| {index} | {name} | 功能{index} | 场景{index} |"
    evidence = header + "\n" + row
    return {
        "index": index, "source_type": "note", "source_id": source_id,
        "title": "对象场景清单", "evidence": evidence, "excerpt": evidence,
        "location": "保存正文 · 20–80", "source_field": "content",
        "raw_evidence": [{"field": "content", "char_start": 20,
                          "char_end_exclusive": 20 + len(evidence), "quote": evidence}],
        "prompt_block": f"[{index}] 对象场景清单 · 保存正文 · 20–80\n表格原始列值：\n"
                        f"{name_header}：{name}\n核心功能：功能{index}\n适用场景：场景{index}",
        "citation_source_id": f"context:{index}",
        "citation_members": [{"id": f"original:{index}", "source_type": "note", "source_id": source_id,
                              "verification": "exact_saved_original", "spans": [{"quote": evidence}]}],
        "citation_provenance": {"issues": [], "semantic_support": "not_validated"},
    }


def response(sources=None, *, question=QUESTION, answer=ANSWER):
    sources = [source(i, name) for i, name in enumerate(NAMES, 5)] if sources is None else sources
    return {
        "answer": answer, "sources": [], "provided_sources": sources,
        "trace": {
            "messages": [{"role": "system", "content": "zero-model fixture"},
                         {"role": "user", "content": "参考资料：\n" + "\n\n".join(s["prompt_block"] for s in sources)
                          + "\n\n问题：" + question}],
            "evidence_registry": {
                "prompt_checked": True, "deterministic_or_structured_path": False,
                "supplied_to_identity": {str(s["index"]): s["citation_source_id"] for s in sources},
            },
        },
    }


class ClarificationSelectionTests(unittest.TestCase):
    def test_only_source_bound_alternatives_are_returned_without_mutation(self):
        packet = response()
        noise = source(2, "别的项目", name_header="项目名称")
        packet["provided_sources"].append(noise)  # Retrieved metadata is not a supplied model block.
        before = copy.deepcopy(packet)
        selected = clarification_sources(QUESTION, packet)
        self.assertEqual([s["index"] for s in selected], [5, 6, 7])
        self.assertEqual([s["clarification_entities"] for s in selected], [[name] for name in NAMES])
        self.assertTrue(all(s["reference_role"] == "clarification" for s in selected))
        self.assertEqual(selected[0]["raw_evidence"], before["provided_sources"][0]["raw_evidence"])
        selected[0]["raw_evidence"][0]["quote"] = "changed copy"
        self.assertEqual(packet, before)

    def test_bare_attribute_variants_use_source_column_names(self):
        for question in (QUESTION, "请问核心功能及适用场景是什么？", "Agent的核心功能和适用场景是什么？"):
            with self.subTest(question=question):
                self.assertEqual(len(clarification_sources(question, response(question=question))), 3)
        alternatives = [source(1, "工具甲", name_header="工具名称"), source(2, "工具乙", name_header="工具名称")]
        packet = response(alternatives, answer="问题未指定具体对象，当前资料有多个工具，请明确工具名称。")
        self.assertEqual(len(clarification_sources(QUESTION, packet)), 2)

    def test_explicit_subjects_and_plural_list_questions_are_not_clarifications(self):
        for question in ("会议行动项提取Agent的核心功能和适用场景是什么？",
                         "不存在的工具的核心功能和适用场景是什么？",
                         "所有Agent的核心功能和适用场景是什么？", "有哪些Agent？"):
            with self.subTest(question=question):
                self.assertEqual(clarification_sources(question, response(question=question)), [])

    def test_resolved_conversation_subject_is_preserved(self):
        resolved = QueryResolver.resolve("它的核心功能和适用场景是什么？", [
            {"role": "user", "content": "会议行动项提取Agent能做什么？"},
            {"role": "assistant", "content": "这里不是证据。"},
        ])
        self.assertTrue(resolved.history_used)
        self.assertEqual(clarification_sources(resolved.evidence_question, response()), [])

    def test_no_or_one_distinct_object_cannot_establish_ambiguity(self):
        for sources in ([], [source(1, NAMES[0])], [source(1, NAMES[0]), source(2, NAMES[0])]):
            with self.subTest(count=len(sources)):
                self.assertEqual(clarification_sources(QUESTION, response(sources)), [])

    def test_refusal_factual_answers_and_cited_answers_do_not_get_references(self):
        for answer in ("没有足够资料，无法回答。", "核心功能是记录信息。", ANSWER + "[1]",
                       "问题未指定具体合同，多个合同需要进一步确认。"):
            with self.subTest(answer=answer):
                self.assertEqual(clarification_sources(QUESTION, response(answer=answer)), [])
        packet = response()
        packet["sources"] = [copy.deepcopy(packet["provided_sources"][0])]
        self.assertEqual(clarification_sources(QUESTION, packet), [])

    def test_unverified_prompt_or_location_cannot_supply_references(self):
        for change in ("messages", "prompt_checked", "identity", "prompt_block", "members", "issues"):
            with self.subTest(change=change):
                packet = response()
                if change == "messages":
                    packet["trace"]["messages"] = []
                elif change == "prompt_checked":
                    packet["trace"]["evidence_registry"]["prompt_checked"] = False
                elif change == "identity":
                    packet["trace"]["evidence_registry"]["supplied_to_identity"] = {}
                else:
                    for item in packet["provided_sources"]:
                        if change == "prompt_block":
                            item["prompt_block"] = "未发送的片段"
                        elif change == "members":
                            item["citation_members"][0]["verification"] = "supplied_fragment_only"
                        else:
                            item["citation_provenance"]["issues"] = [{"reason": "original_span_mismatch"}]
                self.assertEqual(clarification_sources(QUESTION, packet), [])

    def test_saved_row_attributes_must_also_be_in_actual_model_block(self):
        sources = [source(i, name) for i, name in enumerate(NAMES, 5)]
        for item in sources:
            item["prompt_block"] = item["prompt_block"].split("\n核心功能：", 1)[0]
        self.assertEqual(clarification_sources(QUESTION, response(sources)), [])

    def test_unbound_model_examples_do_not_manufacture_references(self):
        answer = "问题未指定具体对象。存在多个Agent，例如：幽灵 Agent、虚构 Agent。请明确对象名称。"
        self.assertEqual(clarification_sources(QUESTION, response(answer=answer)), [])


class _Embedding:
    available = True
    unavailable_reason = ""

    def embed(self, text):
        return np.array([1.0, 0.0], dtype=np.float32)


class _TextEngine:
    available = True
    unavailable_reason = ""
    model_loaded = False

    def session(self):
        return nullcontext()

    def _clean_answer(self, text):
        return text


class _Candidate:
    def __init__(self, context):
        self.store = context.store
        self.evidence_registry = SimpleNamespace(stage=lambda prepared: prepared["sources"])

    def stage(self, question):
        parent = self.store.list_notes(limit=20)[0]
        note = next(tab for tab in self.store.get_note_group(parent.id) if tab.parent_note_id is not None)
        self.packet = response([source(i, name, source_id=note.id) for i, name in enumerate(NAMES, 5)], question=question)
        return {"mode": "clarification-zero-model-fixture", "sources": self.packet["provided_sources"]}

    def ask(self, question):
        return copy.deepcopy(self.packet)

    def close(self):
        pass


class ClarificationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = NoteStore(root / "data")
        self.parent = self.store.create_note("父笔记", "主页")
        self.child = self.store.create_note("对象场景清单", "接线测试资料", parent_note_id=self.parent.id, tab_name="场景表")
        self.embedding = EmbeddingService(self.store, _Embedding())
        self.native = _TextEngine()
        intelligence = IntelligenceService(self.store, self.native, auto_apply=False)
        config = AppConfig(root / "config.json", self.store.data_dir, ai_auto_apply=False, ai_warmup_on_start=False)
        self.server = PocketMemoryServer(("127.0.0.1", 0), self.store, config, lambda initial: None,
                                        OcrService(self.store, SimpleNamespace(available=False)), self.embedding, intelligence)
        self.pipeline = LiveRagPipeline(self.server.rag_pipeline, self.native, _Candidate)
        self.server.rag_pipeline = self.pipeline
        intelligence.engine = LiveCandidateEngine(self.native, self.pipeline)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.embedding.drain(timeout=5)
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def ask(self, stream=False, **extra):
        payload = {"question": QUESTION, "stream": stream, **extra}
        request = Request(f"http://127.0.0.1:{self.server.server_port}/api/ask",
                          data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=8) as result:
            raw = result.read().decode()
        if stream:
            events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
            return next(event for event in events if event["type"] == "done")
        return json.loads(raw)

    def test_json_and_sse_keep_original_answer_and_separate_reference_targets(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                result = self.ask(stream)
                self.assertEqual(result["answer"], ANSWER)
                self.assertEqual(result["sources"], [])
                references = result["clarification_sources"]
                self.assertEqual(len(references), 3)
                for item in references:
                    self.assertEqual(item["source_id"], self.child.id)
                    self.assertEqual(item["parent_note_id"], self.parent.id)
                    self.assertEqual(item["tab_name"], "场景表")
                    self.assertEqual(item["url"], f"/api/notes/{self.parent.id}?tab_id={self.child.id}")
                    self.assertNotIn("prompt_block", item)
                    self.assertTrue(item["citation_members"])

    def test_followup_with_a_known_subject_does_not_add_clarification_sources(self):
        result = self.ask(question="它的核心功能和适用场景是什么？", history=[
            {"role": "user", "content": "会议行动项提取Agent能做什么？"},
        ])
        self.assertEqual(result.get("clarification_sources", []), [])


if __name__ == "__main__":
    unittest.main()
