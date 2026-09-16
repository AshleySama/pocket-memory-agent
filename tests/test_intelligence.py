from __future__ import annotations

import tempfile
import time
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pocket_memory import intelligence
from pocket_memory.intelligence import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_MODEL_REL,
    IntelligenceService,
    LlamaCppTextEngine,
)
from pocket_memory.storage import NoteStore


class StubTextEngine:
    available = True
    unavailable_reason = ""

    def classify(self, text: str, existing_tags: list[str] | None = None) -> dict:
        return {
            "category": "工作",
            "subcategory": "采购风控",
            "tags": ["供应商", "异常监控", "审批"],
        }


class CapturingTextEngine(StubTextEngine):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def classify(
        self,
        text: str,
        existing_tags: list[str] | None = None,
        excluded_tags: list[str] | None = None,
    ) -> dict:
        self.calls.append({"text": text, "existing_tags": existing_tags or [], "excluded_tags": excluded_tags or []})
        return super().classify(text, existing_tags)


class StatusOnlyTextEngine:
    @property
    def available(self):
        raise AssertionError("status must not load the model")

    def runtime_status(self) -> dict:
        return {"available": True, "reason": "", "model_loaded": False}


class IntelligenceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / "data")
        self.service = IntelligenceService(self.store, StubTextEngine())

    def tearDown(self) -> None:
        self.service.shutdown()
        self.store.close()
        self.temp.cleanup()

    def wait_done(self, note_id: int):
        for _ in range(100):
            note = self.store.get_note(note_id)
            if note.ai_status == "done":
                return note
            time.sleep(0.01)
        self.fail("AI task did not complete")

    def test_ai_fills_empty_category_subcategory_and_tags(self) -> None:
        note = self.store.create_note("采购异常", "供应商存在外部风险")

        self.service.enqueue(note.id)
        note = self.wait_done(note.id)

        self.assertEqual(note.category, "工作")
        self.assertEqual(note.subcategory, "采购风控")
        self.assertEqual(note.tags, ["供应商", "异常监控"])

    def test_ai_does_not_override_manual_category_or_tags(self) -> None:
        note = self.store.create_note(
            "手工整理",
            "供应商存在外部风险",
            category="技术",
            subcategory="SQL",
            tags=["手工标签"],
        )

        self.service.enqueue(note.id)
        note = self.wait_done(note.id)

        self.assertEqual(note.category, "技术")
        self.assertEqual(note.subcategory, "SQL")
        self.assertIn("手工标签", note.tags)
        self.assertIn("供应商", note.tags)

    def test_parser_accepts_json_with_extra_text(self) -> None:
        result = LlamaCppTextEngine._parse_json(
            '前面有解释 {"category":"学习","subcategory":"暑假作业","tags":["暑假","作业"]} 后面还有文字'
        )

        self.assertEqual(result, {"category": "学习", "subcategory": "暑假作业", "tags": ["暑假", "作业"]})

    def test_default_model_targets_the_verified_4b_instruct_file(self) -> None:
        self.assertEqual(
            DEFAULT_MODEL_REL,
            "Qwen3-4B/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        )
        self.assertEqual(DEFAULT_CONTEXT_TOKENS, 3072)

    def test_packaged_app_prefers_external_models_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "PocketMemory.exe"
            executable.touch()
            external_model = root / "models" / "Qwen3-4B" / "missing.gguf"
            external_model.parent.mkdir(parents=True)
            external_model.touch()
            with patch.object(intelligence.sys, "_MEIPASS", str(root / "_internal"), create=True), patch.object(
                intelligence.sys, "executable", str(executable)
            ):
                resolved = intelligence.resource_path("models/Qwen3-4B/missing.gguf")
        self.assertEqual(resolved, external_model)

    def test_model_load_falls_back_to_non_mmap_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model_path = Path(temp) / "model.gguf"
            model_path.write_bytes(b"placeholder")
            calls: list[dict] = []

            def fake_llama(**kwargs):
                calls.append(kwargs)
                if kwargs["use_mmap"]:
                    raise ValueError("mapped loading blocked")
                return object()

            fake_module = types.ModuleType("llama_cpp")
            fake_module.Llama = fake_llama
            engine = LlamaCppTextEngine(model_path=model_path)
            with patch.dict(sys.modules, {"llama_cpp": fake_module}):
                engine._ensure_loaded()

        self.assertIsNotNone(engine._llm)
        self.assertEqual(
            [(call["use_mlock"], call["use_mmap"]) for call in calls],
            [(True, True), (False, True), (False, False)],
        )

    def test_answer_messages_bound_evidence_history_and_remove_legacy_control_token(self) -> None:
        contexts = [
            {"title": f"笔记 {index}", "score": 0.9, "text": "证据。" * 400}
            for index in range(6)
        ]
        history = [
            {"role": "user", "content": "历史问题。" * 200},
            {"role": "assistant", "content": "历史回答。" * 200},
        ] * 3
        messages = LlamaCppTextEngine._answer_messages("问题。" * 400, contexts, history)
        rendered = "\n".join(item["content"] for item in messages)

        self.assertNotIn("/no_think", rendered)
        self.assertIn("错误前提", rendered)
        self.assertIn(">10%", rendered)
        self.assertLessEqual(len(messages[-1]["content"]), 2300)
        self.assertEqual(len(messages), 6)  # system + 最近两轮（4 段）+ 当前问题
        self.assertTrue(all(len(item["content"]) <= 140 for item in messages[1:-1]))
        self.assertIn("[1] 标题：笔记 0", messages[-1]["content"])

    def test_answer_prompt_hides_unmatched_versioned_identifier_only(self) -> None:
        messages = LlamaCppTextEngine._answer_messages(
            "Qwen3-0.6B 向量检索方案对比里提到了什么优化？",
            [{"title": "BGE-small-zh 向量检索方案对比", "text": "采用线性扫描，后续引入 sqlite-vec。"}],
            None,
        )

        self.assertNotIn("Qwen3-0.6B", messages[-1]["content"])
        self.assertIn("向量检索方案对比里提到了什么优化", messages[-1]["content"])
        self.assertIn("sqlite-vec", messages[-1]["content"])

    def test_status_does_not_load_model_weights(self) -> None:
        service = IntelligenceService(self.store, StatusOnlyTextEngine())
        try:
            status = service.status()
        finally:
            service.shutdown()
        self.assertTrue(status["available"])
        self.assertFalse(status["model_loaded"])

    def test_background_enqueue_always_uses_the_configured_model(self) -> None:
        note = self.store.create_note("复核计划", "周五前完成供应商复核")
        engine = StubTextEngine()
        service = IntelligenceService(self.store, engine)
        try:
            service.enqueue(note.id)
            updated = self.wait_done(note.id)
        finally:
            service.shutdown()
        self.assertEqual(updated.category, "工作")

    def test_manual_organisation_forces_apply_when_automatic_mode_is_off(self) -> None:
        note = self.store.create_note("手工触发", "供应商需要复核")
        service = IntelligenceService(self.store, StubTextEngine(), auto_apply=False)
        try:
            service.enqueue(note.id, apply=True)
            updated = self.wait_done(note.id)
        finally:
            service.shutdown()

        self.assertEqual(updated.category, "工作")
        self.assertIn("供应商", updated.tags)

    def test_organisation_uses_every_tab_as_bounded_context(self) -> None:
        parent = self.store.create_note("项目复盘", "主页：确认采购计划和责任人")
        child = self.store.create_note(
            "风险页", "页签二：记录供应商风险及付款例外", parent_note_id=parent.id, tab_name="风险"
        )
        engine = CapturingTextEngine()
        service = IntelligenceService(self.store, engine)
        try:
            service.enqueue(child.id)
            self.wait_done(child.id)
        finally:
            service.shutdown()

        self.assertEqual(len(engine.calls), 1)
        prompt = engine.calls[0]["text"]
        self.assertIn("【页签1】", prompt)
        self.assertIn("确认采购计划", prompt)
        self.assertIn("【风险】", prompt)
        self.assertIn("供应商风险", prompt)
        self.assertEqual(self.store.get_note(parent.id).tags, self.store.get_note(child.id).tags)


if __name__ == "__main__":
    unittest.main()
