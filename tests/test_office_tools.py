# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import tempfile
import json
import threading
import time
import unittest

from pocket_memory.office_tools import OfficeTools, TodoStore, _checked_result, _load_json, _numbers


class Engine:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def _chat(self, messages, **options):
        self.calls.append((messages, options))
        return self.output


class OfficeToolTests(unittest.TestCase):
    def wait_job(self, service, job_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = service.get_job(job_id, "library-a")
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(.01)
        self.fail("job did not finish")

    def test_real_job_contract_and_library_isolation(self):
        engine = Engine('{"text":"请在周五提交初稿。"}')
        service = OfficeTools(engine)
        initial = service.create_job({"kind": "rewrite", "text": "周五交初稿", "mode": "polite"}, "library-a")
        result = self.wait_job(service, initial["job_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["text"], "请在周五提交初稿。")
        self.assertEqual(len(engine.calls), 1)
        with self.assertRaises(KeyError):
            service.get_job(initial["job_id"], "library-b")
        result["result"]["text"] = "mutated"
        self.assertNotEqual(service.get_job(initial["job_id"], "library-a")["result"]["text"], "mutated")

    def test_incomplete_json_fails_without_retry(self):
        engine = Engine('{"text":"未完成')
        service = OfficeTools(engine)
        initial = service.create_job({"kind": "rewrite", "text": "原文"}, "library-a")
        job = self.wait_job(service, initial["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("不完整", job["error"])
        self.assertEqual(job["input_text"], "原文")
        self.assertEqual(len(engine.calls), 1)

    def test_busy_does_not_queue_more_generations(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingEngine:
            def _chat(self, *args, **kwargs):
                entered.set()
                release.wait(2)
                return '{"items":[]}'
        service = OfficeTools(BlockingEngine())
        first = service.create_job({"kind": "todos", "text": "暂无待办"}, "library-a")
        self.assertTrue(entered.wait(1))
        try:
            self.assertTrue(service.busy)
            with self.assertRaisesRegex(ValueError, "正在处理"):
                service.create_job({"kind": "todos", "text": "另一个"}, "library-a")
            service.close()
        finally:
            release.set()
        self.wait_job(service, first["job_id"])
        with self.assertRaisesRegex(ValueError, "关闭"):
            service.create_job({"kind": "todos", "text": "另一个"}, "library-a")

    def test_input_and_modes_are_validated_without_truncation(self):
        service = OfficeTools(Engine('{}'))
        for payload in ({"kind": "todos", "text": "字" * 1001},
                        {"kind": "rewrite", "text": "正文", "mode": []},
                        {"kind": [], "text": "正文"}):
            with self.assertRaises(ValueError):
                service.create_job(payload, "library-a")

    def test_invented_owner_and_date_are_cleared(self):
        text = "请提交初稿。"
        result = _checked_result("todos", {"text": text}, {"items": [
            {"task": "提交初稿", "owner": "张三", "due_text": "明天", "evidence": text}]})
        self.assertEqual(result["items"][0]["owner"], "")
        self.assertEqual(result["items"][0]["due_text"], "")
        self.assertEqual(len(result["warnings"]), 2)

    def test_fabricated_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "原句"):
            _checked_result("todos", {"text": "张三交初稿。"}, {"items": [
                {"task": "交初稿", "owner": "李四", "evidence": "李四交初稿。"}]})

    def test_changed_numbers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "数字"):
            _checked_result("rewrite", {"text": "预算300元。"}, {"text": "预算500元。"})

    def test_list_numbers_do_not_hide_real_numbers_or_versions(self):
        self.assertEqual(_numbers("1. 预算300元\n2、使用1.2.0版本\n（3）处理24项"),
                         _numbers("预算300元，使用1.2.0版本，处理24项"))
        with self.assertRaisesRegex(ValueError, "数字"):
            _checked_result("rewrite", {"text": "条件为@media(max-width:1024px)"},
                            {"text": "条件为@media(max-width:1000px)"})

    def test_table_is_unavailable_without_calling_the_model(self):
        engine = Engine('{}')
        service = OfficeTools(engine)
        with self.assertRaisesRegex(ValueError, "本版仅支持待办提取和文本加工"):
            service.create_job({"kind": "table", "text": "名称：甲；颜色：蓝色", "columns": ["名称", "颜色"]},
                               "library-a")
        self.assertEqual(engine.calls, [])
        self.assertFalse(service.busy)

    def test_json_fence_supported_but_extra_prose_rejected(self):
        self.assertEqual(_load_json('```json\n{"items":[]}\n```'), {"items": []})
        with self.assertRaises(ValueError):
            _load_json('这里是结果 {"items":[]}')

    def test_rewrite_rejects_diagnostic_prose_without_salvaging_partial_text(self):
        # The real screenshot failure: generation stopped normally, but model
        # appended prompt checklists as invalid JSON members after the text.
        output = '{"text":"完整正文。","原文数值及出现次数（不含列表序号）：{}","须原样保留的技术片段：[]"}'
        engine = Engine(output)
        service = OfficeTools(engine)
        initial = service.create_job({"kind": "rewrite", "text": "完整正文。"}, "library-a")
        job = self.wait_job(service, initial["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(job["result"])
        self.assertNotIn("缩短", job["error"])
        self.assertEqual(job["input_text"], "完整正文。")
        self.assertEqual(len(engine.calls), 1)

    def test_rewrite_keeps_json_escaped_quotes_newlines_and_paths(self):
        source = '请保留 "原文"。\n路径：C:\\notes\\draft.txt；版本1.2.0。'
        revised = '请保留 "原文"。\n文件路径：C:\\notes\\draft.txt；版本1.2.0。'
        engine = Engine(json.dumps({"text": revised}, ensure_ascii=False))
        service = OfficeTools(engine)
        initial = service.create_job({"kind": "rewrite", "text": source}, "library-a")
        job = self.wait_job(service, initial["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["text"], revised)


class TodoStoreTests(unittest.TestCase):
    def test_persistence_idempotence_and_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            source = {"note_id": 7, "title": "会议记录", "library_id": "library-a"}
            items = [{"task": "提交初稿", "owner": "张三", "due_text": "周五", "evidence": "张三周五提交初稿。"}]
            first = TodoStore(folder).add(items, source, "request-1")
            again = TodoStore(folder).add(items, source, "request-1")
            self.assertEqual(first, again)
            self.assertEqual(len(TodoStore(folder).list()), 1)
            changed = TodoStore(folder).update(first[0]["id"], {"completed": True, "task": "提交修订初稿"})
            self.assertTrue(changed["completed"])
            self.assertEqual(changed["source"], source)
            self.assertEqual(changed["evidence"], items[0]["evidence"])
            self.assertFalse(TodoStore(folder).update(first[0]["id"], {"completed": False})["completed"])

    def test_invalid_batch_is_atomic_and_receipt_detects_changed_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TodoStore(folder)
            with self.assertRaises(ValueError):
                store.add([{"task": "有效"}, {"task": ""}], {}, "bad")
            self.assertEqual(store.list(), [])
            store.add([{"task": "原内容"}], {}, "same")
            with self.assertRaises(ValueError):
                store.add([{"task": "新内容"}], {}, "same")
            self.assertEqual(len(store.list()), 1)
            with self.assertRaises(ValueError):
                store.update(store.list()[0]["id"], {"completed": "false"})


if __name__ == "__main__":
    unittest.main()
