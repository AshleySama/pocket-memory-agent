# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Zero-generation reproduction of an empty tool result losing a dated action.

The test runner supplies an empty JSON model response. No model, real notes,
network requests or persistent user todos are used.
"""
import json
import unittest

from pocket_memory.office_tools import OfficeTools, _checked_result


class EmptyEngine:
    def __init__(self):
        self.calls = []

    def _chat(self, messages, **options):
        self.calls.append((messages, options))
        return json.dumps({"items": []})


class DatedTodoCoverageTests(unittest.TestCase):
    def extract_empty(self, text):
        return _checked_result("todos", {"text": text}, {"items": []})["items"]

    def test_colleague_action_is_not_lost_in_meeting_background(self):
        text = "此次会议介绍了Copilot在PPT中的常用场景，11月18发送会议讨论项目开发计划排期，若有问题随时沟通~"
        engine = EmptyEngine()
        service = OfficeTools(engine)
        result = service._generate({"kind": "todos", "text": text})
        self.assertEqual(len(engine.calls), 1, "must not retry local generation")
        self.assertEqual(result["items"], [{
            "task": "11月18发送会议讨论项目开发计划排期，若有问题随时沟通~",
            "owner": "", "due_text": "11月18", "evidence": text,
        }])

    def test_different_dates_and_actions_keep_verbatim_date_without_year_guess(self):
        for text, due in [
            ("本次讨论了数据来源，12月3号提交修订说明。", "12月3号"),
            ("于2027年2月6日上午9点确认预算。", "2027年2月6日上午9点"),
            ("明天检查备份文件。", "明天"),
            ("下周三前完成接口测试。", "下周三前"),
            ("2027-02-06发布更新说明。", "2027-02-06"),
        ]:
            with self.subTest(text=text):
                items = self.extract_empty(text)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["due_text"], due)
                self.assertEqual(items[0]["owner"], "")
                self.assertIn(items[0]["task"], text)
                self.assertIn(items[0]["evidence"], text)
                self.assertNotIn("completed", items[0])

    def test_dependent_suffix_is_never_trimmed(self):
        text = "11月18提交审阅材料，不含客户名单，只有审批通过才能对外发布。"
        self.assertEqual(self.extract_empty(text)[0]["task"], text)

    def test_past_negative_background_and_ambiguous_condition_are_not_added(self):
        examples = [
            "11月18发送了会议通知。", "11月18发送过会议通知。",
            "11月18已发送会议通知。", "11月18发送会议通知（已完成）。",
            "11月18不发送会议通知。", "11月18不要发送会议通知。",
            "11月18取消发送会议通知。", "11月18发送会议通知，此项已取消。",
            "昨日会议回顾，11月18发送会议通知。", "回顾：11月18发送会议通知。",
            "会议介绍了11月18日的工作安排。", "11月18天气晴朗。",
            "如果审批通过，11月18发送会议通知。", "审批通过后，明天发送会议通知。",
            "11月18提交初稿，11月20发布终稿。", "需要考虑是否在11月18发送会议通知。",
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(self.extract_empty(text), [])

    def test_existing_model_items_are_preserved_without_duplicate_recovery(self):
        text = "11月18提交初稿。确认后通知同事。"
        original = [{"task": "确认后通知同事", "owner": "", "due_text": "",
                     "evidence": "确认后通知同事。"}]
        result = _checked_result("todos", {"text": text}, {"items": original})
        self.assertEqual(result["items"], original)

    def test_invalid_model_payload_is_not_silently_repaired(self):
        with self.assertRaisesRegex(ValueError, "格式"):
            _checked_result("todos", {"text": "11月18提交初稿。"}, {"items": None})

    def test_many_dated_actions_do_not_silently_truncate_to_limit(self):
        text = "\n".join(f"11月{day}日提交修订材料。" for day in range(1, 10))
        self.assertEqual(self.extract_empty(text), [])


if __name__ == "__main__":
    unittest.main()
