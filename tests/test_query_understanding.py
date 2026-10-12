# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import unittest

from pocket_memory.rag import QueryResolver


class QueryResolverTests(unittest.TestCase):
    def test_complete_new_question_never_inherits_prior_subject(self) -> None:
        result = QueryResolver.resolve(
            "项目有哪些风险？",
            [
                {"role": "user", "content": "工业互联网和人工智能各自承担什么角色？"},
                {"role": "assistant", "content": "工业互联网负责连接。"},
            ],
        )

        self.assertEqual(result.mode, "single_turn")
        self.assertEqual(result.retrieval_query, "项目有哪些风险?")
        self.assertFalse(result.history_used)

    def test_deictic_followup_inherits_only_the_previous_user_subject(self) -> None:
        result = QueryResolver.resolve(
            "它能直接给法律结论吗？",
            [
                {"role": "user", "content": "合同条款比对 Agent 能做什么？"},
                {"role": "assistant", "content": "这段回答不能作为证据。"},
            ],
        )

        self.assertEqual(result.mode, "contextual_followup")
        self.assertEqual(result.subject, "合同条款比对Agent")
        self.assertIn("合同条款比对Agent", result.retrieval_query)
        self.assertNotIn("这段回答", result.retrieval_query)

    def test_threshold_and_event_ellipsis_are_resolved(self) -> None:
        threshold = QueryResolver.resolve(
            "超过一点点也要停吗？",
            [{"role": "user", "content": "营销活动预算怎么控制？"}],
        )
        event = QueryResolver.resolve(
            "正式上线前先给谁试？",
            [{"role": "user", "content": "北辰项目什么时候上线？"}],
        )

        self.assertEqual(threshold.subject, "营销活动预算")
        self.assertEqual(event.subject, "北辰项目")
        self.assertTrue(threshold.history_used)
        self.assertTrue(event.history_used)

    def test_subject_keeps_a_business_field_needed_to_disambiguate_sources(self) -> None:
        result = QueryResolver.resolve(
            "那供应商 A 下一步是谁做？",
            [{"role": "user", "content": "供应商 A 现在风险是什么？"}],
        )

        self.assertEqual(result.subject, "供应商 A 风险")
        self.assertTrue(result.retrieval_query.startswith("关于供应商 A 风险，"))
        self.assertIn("下一步", result.retrieval_query)

    def test_followup_subject_excludes_the_previous_colour_state(self) -> None:
        result = QueryResolver.resolve(
            "那绿色呢？",
            [{"role": "user", "content": "供应商 A 的交货延迟率红色预警是多少？"}],
        )

        self.assertEqual(result.subject, "供应商 A 的交货延迟率")
        self.assertTrue(result.retrieval_query.startswith("关于供应商 A 的交货延迟率，"))
        self.assertIn("那绿色呢?", result.retrieval_query)

    def test_followup_keeps_a_prior_colour_when_it_defines_the_requested_threshold(self) -> None:
        result = QueryResolver.resolve(
            "那它的阈值呢？",
            [{"role": "user", "content": "供应商 A 的交货延迟率红色预警是什么？"}],
        )

        self.assertEqual(result.subject, "供应商 A 的交货延迟率红色预警")
        self.assertIn("红色预警", result.evidence_question)
        self.assertIn("amount", result.requested_fields)

    def test_ambiguous_action_does_not_borrow_an_unrelated_target(self) -> None:
        result = QueryResolver.resolve(
            "为什么不能删除",
            [{"role": "user", "content": "PO 订单是否需要上传合同？"}],
        )

        self.assertEqual(result.mode, "single_turn")
        self.assertFalse(result.history_used)

    def test_followup_without_history_remains_unresolved(self) -> None:
        result = QueryResolver.resolve("这个期限怎么算？", [])

        self.assertEqual(result.mode, "single_turn")
        self.assertEqual(result.reason, "followup_without_history")

    def test_structured_features_are_exposed_without_a_model_call(self) -> None:
        result = QueryResolver.resolve("项目风险扫描 Agent 的核心功能和适用场景是什么？")

        self.assertIn("项目风险扫描Agent", result.entities)
        self.assertEqual(result.operation, "definition")
        self.assertIn("function", result.requested_fields)
        self.assertIn("scenario", result.requested_fields)
        self.assertIn("项目风险扫描 Agent", result.aliases)

    def test_agent_spacing_before_chinese_fields_keeps_a_complete_new_question(self) -> None:
        history = [{"role": "user", "content": "项目风险扫描Agent的适用场景是什么？"}]
        for space in (" ", "\u3000", "\u00a0", "\n"):
            for suffix in ("Agent", "aGeNt"):
                for field in ("的核心功能是什么", "输入资料和核心功能是什么"):
                    question = f"文件更新检查{space}{suffix}{field}"
                    expected = f"文件更新检查{suffix}{field}"
                    with self.subTest(question=question):
                        result = QueryResolver.resolve(question, history)
                        self.assertEqual(result.normalized_query, expected)
                        self.assertEqual(result.evidence_question, expected)
                        self.assertIn(f"文件更新检查{suffix}", result.entities)
                        self.assertEqual(result.mode, "single_turn")
                        self.assertFalse(result.history_used)
                        self.assertEqual(result.subject, "")

    def test_existing_technical_tokens_allow_a_following_chinese_word(self) -> None:
        for token in ("AI", "RAG", "OCR"):
            with self.subTest(token=token):
                self.assertEqual(
                    QueryResolver.normalize(f"本地 {token}的作用是什么"),
                    f"本地{token}的作用是什么",
                )

    def test_ascii_token_continuations_are_not_joined_as_shorter_suffixes(self) -> None:
        for token in ("AgentX", "Agent_Pro", "Agent-Toolkit", "Agent2", "AIModel", "RAG_Pro", "OCR2"):
            question = f"文件检查 {token}的核心功能是什么"
            with self.subTest(token=token):
                self.assertEqual(QueryResolver.normalize(question), question)

    def test_model_size_followup_exposes_a_size_field(self) -> None:
        result = QueryResolver.resolve(
            "那模型多大？",
            [{"role": "user", "content": "BGE-small-zh 向量检索方案有什么特点？"}],
        )

        self.assertEqual(result.mode, "contextual_followup")
        self.assertIn("size", result.requested_fields)
        self.assertIn("BGE-small-zh", result.evidence_question)

    def test_scalar_how_many_questions_are_not_misclassified_as_counts(self) -> None:
        for question in (
            "供应商A黄色预警的交货延迟率是多少？",
            "采购审批当前平均耗时多少天？",
            "这个模型占用多少GB？",
            "合同金额一共是多少万元？",
        ):
            with self.subTest(question=question):
                result = QueryResolver.resolve(question)
                self.assertEqual(result.operation, "lookup")
                self.assertNotIn("count", result.requested_fields)

    def test_true_enumeration_questions_remain_counts(self) -> None:
        for question in (
            "项目有几个风险？",
            "一共有多少条笔记？",
            "总共有多少份资料？",
            "标签数量是多少？",
        ):
            with self.subTest(question=question):
                result = QueryResolver.resolve(question)
                self.assertEqual(result.operation, "count")
                self.assertIn("count", result.requested_fields)

    def test_hyphenated_business_identifier_is_not_parsed_as_a_date(self) -> None:
        result = QueryResolver.resolve("HT-2026-021 的预付款比例和尾款付款节点是什么？")

        self.assertEqual(result.time_expressions, ())
        self.assertIn("amount", result.requested_fields)


if __name__ == "__main__":
    unittest.main()
