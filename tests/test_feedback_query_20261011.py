# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pocket_memory.rag.pipeline import RagPipeline


class AgentQuestionLeadInTests(unittest.TestCase):
    row = (
        "| 9 | 文件更新检查 Agent | 提醒导入文件重建索引 | 资料维护 | "
        "文件版本 | 更新清单 | 演示负责人 | 试用 |"
    )

    def chunk(self, text: str) -> dict:
        return {
            "title": "Agent场景清单",
            "raw_text": text,
            "hits": ["文件更新检查", "资料"],
            "score": 0.9,
        }

    def test_lead_in_variants_keep_the_same_entity_and_direct_row(self) -> None:
        questions = (
            "文件更新检查Agent输入资料和核心功能是什么",
            "关于文件更新检查Agent的输入资料和核心功能是什么",
            "请问文件更新检查Agent的输入资料和核心功能是什么",
            "请问关于文件更新检查Agent的输入资料和核心功能是什么",
            "关于 文件更新检查 Agent 的输入资料和核心功能是什么",
            "  请问，关于 文件更新检查 Agent 的输入资料和核心功能是什么？",
        )
        chunk = self.chunk(self.row)
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    RagPipeline.required_retrieval_topics(question),
                    ["文件更新检查Agent"],
                )
                self.assertTrue(RagPipeline.has_answerable_evidence(question, [chunk]))
                self.assertEqual(RagPipeline.retain_focused_chunks(question, [chunk]), [chunk])

    def test_about_question_final_contract_accepts_direct_table_evidence(self) -> None:
        question = "关于文件更新检查Agent的输入资料和核心功能是什么"
        source = {
            "index": 1,
            "title": "Agent场景清单",
            "evidence": self.row,
            "matched_terms": ["文件更新检查Agent", "资料"],
        }

        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "来源[1] 文件更新检查Agent的输入资料是文件版本，核心功能是提醒导入文件重建索引。",
            [source],
        )

        self.assertEqual(selected, [source])
        self.assertIn("文件版本", answer)
        self.assertIn("提醒导入文件重建索引", answer)

    def test_lead_in_normalization_still_rejects_a_different_agent(self) -> None:
        for question, text in (
            ("关于文件删除检查Agent的输入资料和核心功能是什么", self.row),
            (
                "关于文件更新检查Agent的输入资料和核心功能是什么",
                self.row.replace("文件更新检查 Agent", "文档归档 Agent"),
            ),
        ):
            with self.subTest(question=question, text=text):
                self.assertFalse(
                    RagPipeline.has_answerable_evidence(question, [self.chunk(text)]),
                )

    def test_lead_in_does_not_turn_an_action_request_into_a_named_agent(self) -> None:
        self.assertEqual(RagPipeline.explicit_entity_terms("请问如何扫描低效Agent？"), [])
        self.assertEqual(RagPipeline.explicit_entity_terms("如何扫描低效Agent？"), [])
        self.assertEqual(
            RagPipeline.explicit_entity_terms("关于隐形低效扫描Agent适合什么场景？"),
            ["隐形低效扫描Agent"],
        )

    def test_chinese_agent_suffix_uses_the_same_lead_in_rule(self) -> None:
        self.assertEqual(
            RagPipeline.explicit_entity_terms("请问关于文件更新检查智能体的核心功能是什么？"),
            ["文件更新检查智能体"],
        )

    def test_spaced_agent_suffix_followed_by_chinese_keeps_the_exact_row(self) -> None:
        chunk = self.chunk(self.row)
        for space in (" ", "\t", "\u3000"):
            for field in ("核心功能", "输入资料"):
                question = f"文件更新检查{space}Agent的{field}是什么"
                with self.subTest(question=question):
                    self.assertEqual(RagPipeline.explicit_entity_terms(question), ["文件更新检查Agent"])
                    self.assertEqual(RagPipeline.required_retrieval_topics(question), ["文件更新检查Agent"])
                    self.assertTrue(RagPipeline.has_answerable_evidence(question, [chunk]))
                    self.assertEqual(RagPipeline.retain_focused_chunks(question, [chunk]), [chunk])

    def test_parallel_fields_of_one_agent_keep_the_whole_question(self) -> None:
        questions = (
            "文件更新检查Agent的输入资料和核心功能分别是什么",
            "关于文件更新检查Agent的输入资料和核心功能分别是什么",
            "请问关于 文件更新检查 Agent 的核心功能与输入资料各自是什么？",
            "文件更新检查Agent的输入资料和输出分别是什么",
            "文件更新检查Agent的核心功能、输入资料以及输出分别是什么",
            "文件更新检查智能体的输入资料和核心功能分别是什么",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(RagPipeline.compound_query_clauses(question), [question])

    def test_parallel_field_rule_does_not_merge_agents_or_unknown_fields(self) -> None:
        for question in (
            "文件更新检查Agent和项目风险扫描Agent的核心功能分别是什么",
            "文件更新检查Agent和项目风险扫描Agent的输入资料和核心功能分别是什么",
            "文件更新检查Agent的输入资料和上线日期分别是什么",
        ):
            with self.subTest(question=question):
                self.assertGreater(len(RagPipeline.compound_query_clauses(question)), 1)

    def test_parallel_record_identifier_fields_still_keep_id_on_each_clause(self) -> None:
        question = "合同HT-2026-021的预付款比例和付款节点分别是什么"
        self.assertEqual(RagPipeline.compound_query_clauses(question), [
            "合同HT-2026-021的预付款比例是什么",
            "合同HT-2026-021的付款节点是什么",
        ])

    def test_output_capability_question_uses_the_explicit_limit_without_an_output_column(self) -> None:
        question = "合同条款比对Agent能输出法律意见吗？"
        row = (
            "编号：23；Agent名称：合同条款比对Agent；核心功能：比对标准条款与版本差异；"
            "适用场景：法务初审；亮点/关键词：差异定位；限制：不能输出法律意见。"
        )
        chunk = self.chunk(row)
        chunk["hits"] = ["合同条款比对Agent", "输出法律意见"]
        self.assertIsNone(RagPipeline.structured_field_coverage(question, [row]))
        self.assertTrue(RagPipeline.has_answerable_evidence(question, [chunk]))
        source = {"index": 1, "title": chunk["title"], "evidence": row, "matched_terms": chunk["hits"]}
        answer, sources = RagPipeline.enforce_answer_contract(
            question, "来源[1] 不能输出法律意见。", [source],
        )
        self.assertIn("不能输出法律意见", answer)
        self.assertEqual(sources, [source])
        for field in ("输入资料", "输出资料"):
            self.assertFalse(RagPipeline.structured_field_coverage(
                f"合同条款比对Agent的{field}是什么？", [row],
            ))


class MarkdownAgentFieldTests(unittest.TestCase):
    question = "关于 文件更新检查 Agent 的输入资料和核心功能是什么"
    header = "| 编号 | Agent 名称 | 核心功能 | 适用场景 | 输入资料 | 输出 | 负责人 | 状态 |"
    separator = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    row = AgentQuestionLeadInTests.row

    def table(self, *rows: str, header: str | None = None) -> str:
        return "\n".join([header or self.header, self.separator, *rows])

    def chunk(self, content: str, fragment: str | None = None) -> dict:
        return {
            "note": SimpleNamespace(id=55, content=content),
            "source_id": 55,
            "source_group": "note-55",
            "title": "Agent场景清单",
            "kind": "content",
            "raw_text": self.row if fragment is None else fragment,
            "excerpt": self.row if fragment is None else fragment,
            "hits": ["文件更新检查Agent", "资料"],
            "location": "正文 4",
            "reason": "正文命中：文件更新检查Agent",
            "score": 0.9,
            "rank_score": 100,
        }

    def test_header_mapping_corrects_a_swapped_input_output_answer(self) -> None:
        pipeline = RagPipeline(None, None)
        chunk = self.chunk(self.table(self.row))

        sources = pipeline.source_payloads(self.question, [chunk])
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0]["structured_table_fields"])
        self.assertIn("输入资料：文件版本", sources[0]["evidence"])
        self.assertIn("核心功能：提醒导入文件重建索引", sources[0]["evidence"])
        self.assertNotIn("更新清单", sources[0]["evidence"])
        self.assertNotIn("演示负责人", sources[0]["evidence"])
        answer, selected = pipeline.enforce_answer_contract(
            self.question,
            "来源[1] 输入资料是更新清单，核心功能是提醒导入文件重建索引。",
            sources,
        )
        self.assertIn("输入资料：文件版本", answer)
        self.assertNotIn("更新清单", answer)
        self.assertEqual(selected[0]["url"], "/api/notes/55")

    def test_mapping_uses_the_table_header_order_instead_of_fixed_positions(self) -> None:
        pipeline = RagPipeline(None, None)
        header = self.header.replace("输入资料 | 输出", "输出 | 输入资料")
        row = self.row.replace("文件版本 | 更新清单", "更新清单 | 文件版本")
        evidence = pipeline.markdown_table_field_evidence(
            self.question, self.chunk(self.table(row, header=header), row),
        )
        self.assertIn("输入资料：文件版本", evidence)
        self.assertNotIn("更新清单", evidence)

    def test_parallel_fields_use_complete_mapping_and_correct_swapped_values(self) -> None:
        pipeline = RagPipeline(None, None)
        for question, expected_fields in (
            ("文件更新检查Agent的输入资料和核心功能分别是什么", ("输入资料：文件版本", "核心功能：提醒导入文件重建索引")),
            ("关于文件更新检查Agent的核心功能和输入资料各自是什么", ("输入资料：文件版本", "核心功能：提醒导入文件重建索引")),
            ("文件更新检查Agent的输入资料和输出分别是什么", ("输入资料：文件版本", "输出：更新清单")),
        ):
            with self.subTest(question=question):
                sources = pipeline.source_payloads(question, [self.chunk(self.table(self.row))])
                self.assertEqual(len(sources), 1)
                answer, selected = pipeline.enforce_answer_contract(
                    question, "来源[1] 输入资料是更新清单。", sources,
                )
                self.assertEqual(len(selected), 1)
                for field in expected_fields:
                    self.assertIn(field, answer)
                self.assertNotIn("输入资料是更新清单", answer)

    def test_parallel_fields_cannot_borrow_missing_input_from_another_agent(self) -> None:
        pipeline = RagPipeline(None, None)
        question = "文件更新检查Agent的输入资料和核心功能分别是什么"
        missing = self.row.replace("| 文件版本 |", "|  |")
        neighbour = self.row.replace("文件更新检查 Agent", "项目风险扫描 Agent")
        chunk = self.chunk(self.table(neighbour, missing), neighbour + "\n" + missing)
        self.assertEqual(pipeline.source_payloads(question, [chunk]), [])

    def test_parallel_fields_select_verified_body_origin_without_appending_ocr_identity(self) -> None:
        pipeline = RagPipeline(None, None)
        question = "文件更新检查Agent的输入资料和核心功能分别是什么"
        body = self.chunk(self.table(self.row))
        ocr = {
            **body,
            "kind": "ocr",
            "raw_text": "9文件更新检查Agent",
            "excerpt": "9文件更新检查Agent",
            "location": "图片识别 2",
            "hits": ["文件更新检查Agent"],
        }
        for preferred_kind in ("content", "ocr"):
            with self.subTest(preferred_kind=preferred_kind):
                # Both orders occur in retrieval. A stronger OCR score cannot
                # replace the complete verified row or change its locator.
                with patch.object(pipeline, "_source_evidence_priority", side_effect=(
                    lambda _question, chunk: (int(chunk["kind"] == preferred_kind),)
                )):
                    sources = pipeline.source_payloads(question, [body, ocr])
                self.assertEqual(len(sources), 1)
                self.assertTrue(sources[0]["structured_table_fields"])
                self.assertIn("输入资料：文件版本", sources[0]["evidence"])
                self.assertIn("核心功能：提醒导入文件重建索引", sources[0]["evidence"])
                self.assertNotIn("9文件更新检查Agent", sources[0]["evidence"])
                self.assertEqual(sources[0]["fallback_excerpt"], self.row)
                self.assertEqual(sources[0]["evidence_locations"], ["正文 4"])
                self.assertEqual(sources[0]["evidence_alignment_anchor"]["location"], "正文 4")
                answer, selected = pipeline.enforce_answer_contract(
                    question, "来源[1] 输入资料是文件版本9文件更新检查Agent。", sources,
                )
                self.assertIn("输入资料：文件版本", answer)
                self.assertNotIn("9文件更新检查Agent", answer)
                self.assertEqual(selected[0]["evidence_locations"], ["正文 4"])

    def test_ordinary_agent_field_questions_prefer_complete_body_over_partial_ocr(self) -> None:
        # The nine named Agent rows from the feedback table. The tenth row is
        # named 专题报告助手 and does not use the Agent-entity extraction path.
        cases = (
            ("会议行动项提取 Agent", "汇总会议结论和待办", "会议纪要"),
            ("项目风险扫描 Agent", "识别项目资料中的风险、缺口和待确认项", "周报、问题清单"),
            ("制度问答 Agent", "基于制度资料回答流程问题并返回来源", "PDF、Word"),
            ("文档归档 Agent", "推荐分类与标签，不改写原件", "文件、笔记"),
            ("质量异常归纳 Agent", "汇总异常描述与复发模式", "异常记录"),
            ("交接风险提醒 Agent", "检查未完成行动项和缺失材料", "交接清单"),
            ("学习资料问答 Agent", "对学习笔记给出带来源解释", "笔记、图片"),
            ("文件更新检查 Agent", "提醒导入文件重建索引", "文件版本"),
            ("术语对照 Agent", "汇总术语、定义与资料出处", "制度、笔记"),
        )
        rows = [
            f"| {index} | {name} | {function} | 资料处理 | {input_value} | 处理记录 | 演示负责人 | 试用 |"
            for index, (name, function, input_value) in enumerate(cases, 1)
        ]
        content = self.table(*rows)
        pipeline = RagPipeline(None, None)
        for (name, function, input_value), row in zip(cases, rows):
            with self.subTest(name=name):
                entity = name.replace(" ", "")
                question = f"{entity}输入资料和核心功能是什么"
                body = self.chunk(content, row)
                body["hits"] = [entity, "资料"]
                partial_ocr = f"Agent名称：{entity}；核心功能：{function}；适用场景：资料处理"
                ocr = {
                    **body, "kind": "ocr", "raw_text": partial_ocr,
                    "excerpt": partial_ocr, "location": "图片识别 1",
                }
                with patch.object(pipeline, "_source_evidence_priority", side_effect=(
                    lambda _question, chunk: (int(chunk["kind"] == "ocr"),)
                )):
                    sources = pipeline.source_payloads(question, [body, ocr])
                self.assertEqual(len(sources), 1)
                self.assertTrue(sources[0]["structured_table_fields"])
                self.assertEqual(sources[0]["evidence_locations"], ["正文 4"])
                self.assertEqual(sources[0]["fallback_excerpt"], row)
                answer, selected = pipeline.enforce_answer_contract(
                    question,
                    f"来源[1] 输入资料是{input_value}，核心功能是{function}，并自动分配责任人与截止日期。",
                    sources,
                )
                self.assertEqual(len(selected), 1)
                self.assertIn(f"输入资料：{input_value}", answer)
                self.assertIn(f"核心功能：{function}", answer)
                self.assertNotIn("自动分配", answer)
                self.assertEqual(selected[0]["evidence_alignment_anchor"]["location"], "正文 4")

    def test_ordinary_origin_preference_still_refuses_missing_or_conflicting_fields(self) -> None:
        pipeline = RagPipeline(None, None)
        question = "文档归档Agent输入资料和核心功能是什么"
        row = "| 4 | 文档归档 Agent | 推荐分类与标签，不改写原件 | 资料整理 | 文件、笔记 | 分类建议 | 演示负责人 | 试用 |"
        missing = row.replace("| 文件、笔记 |", "|  |")
        conflicting = row.replace("| 文件、笔记 |", "| 导入日志 |")
        for content, fragment in (
            (self.table(missing), missing),
            (self.table(row) + "\n\n" + self.table(conflicting), row),
        ):
            with self.subTest(content=content):
                body = self.chunk(content, fragment)
                body["hits"] = ["文档归档Agent", "资料"]
                partial_ocr = "Agent名称：文档归档Agent；核心功能：推荐分类与标签，不改写原件；适用场景：资料整理"
                ocr = {
                    **body, "kind": "ocr", "raw_text": partial_ocr,
                    "excerpt": partial_ocr, "location": "图片识别 1",
                }
                with patch.object(pipeline, "_source_evidence_priority", side_effect=(
                    lambda _question, chunk: (int(chunk["kind"] == "ocr"),)
                )):
                    self.assertEqual(pipeline.source_payloads(question, [body, ocr]), [])

    def test_parallel_fields_bind_names_without_an_agent_suffix_to_exact_values(self) -> None:
        pipeline = RagPipeline(None, None)
        cases = (
            ("专题报告助手", "从选定资料组织可编辑 HTML 初稿", "已选笔记、文档"),
            ("资料核对器", "标记重复记录，不删除原件", "原始清单、备注"),
        )
        rows = [
            f"| {index} | {name} | {function} | 资料整理 | {input_value} | 核对结果 | 演示负责人 | 试用 |"
            for index, (name, function, input_value) in enumerate(cases, 1)
        ]
        content = self.table(*rows)
        for (name, function, input_value), row in zip(cases, rows):
            for question in (
                f"{name}输入资料和核心功能是什么",
                f"{name}的输入资料和核心功能是什么",
                f"{name}的输入资料和核心功能分别是什么",
                f"关于{name}的输入资料和核心功能是什么",
            ):
                with self.subTest(question=question):
                    self.assertEqual(pipeline.parallel_field_subject_terms(question), [name])
                    self.assertEqual(pipeline.compound_query_clauses(question), [question])
                    body = self.chunk(content, row)
                    body["hits"] = [name, "资料"]
                    sources = pipeline.source_payloads(question, [body])
                    self.assertEqual(len(sources), 1)
                    self.assertTrue(sources[0]["structured_table_fields"])
                    answer, selected = pipeline.enforce_answer_contract(
                        question, "来源[1] 输入资料为核对结果，还会删除原件并自动发送邮件。", sources,
                    )
                    self.assertEqual(len(selected), 1)
                    self.assertIn(f"输入资料：{input_value}", answer)
                    self.assertIn(f"核心功能：{function}", answer)
                    self.assertNotIn("自动发送邮件", answer)

    def test_parallel_field_subject_requires_one_name_and_complete_known_fields(self) -> None:
        for question in (
            "专题报告助手和资料核对器的输入资料和核心功能是什么",
            "专题报告助手与资料核对器的输入资料和核心功能分别是什么",
            "专题报告助手的输入资料和未知能力是什么",
            "核心功能和输入资料是什么",
            "关于核心功能和输入资料是什么",
            "核心功能、输入资料和输出是什么",
            "输入资料和核心功能是什么",
            "这个的输入资料和核心功能是什么",
        ):
            with self.subTest(question=question):
                self.assertEqual(RagPipeline.parallel_field_subject_terms(question), [])

    def test_single_field_with_explicit_possessive_keeps_exact_named_row_values(self) -> None:
        pipeline = RagPipeline(None, None)
        for name, function, input_value in (
            ("专题报告助手", "从选定资料组织可编辑 HTML 初稿", "已选笔记、文档"),
            ("资料核对器", "标记重复记录，不删除原件", "原始清单、备注"),
        ):
            row = f"| 8 | {name} | {function} | 资料整理 | {input_value} | 核对结果 | 演示负责人 | 试用 |"
            for field, value in (("核心功能", function), ("输入资料", input_value)):
                for prefix in ("", "请问关于"):
                    question = f"{prefix}{name}的{field}是什么"
                    with self.subTest(question=question):
                        self.assertEqual(pipeline.parallel_field_subject_terms(question), [name])
                        self.assertEqual(pipeline.required_retrieval_topics(question), [name])
                        self.assertFalse(pipeline.requires_definition_evidence(question))
                        body = self.chunk(self.table(row), row)
                        body["hits"] = [name, field]
                        sources = pipeline.source_payloads(question, [body])
                        self.assertEqual(len(sources), 1)
                        self.assertEqual(sources[0]["evidence"], f"Agent名称：{name}；{field}：{value}")
                        self.assertTrue(sources[0]["structured_table_fields"])
                        answer, selected = pipeline.enforce_answer_contract(
                            question, "来源[1] 会自动删除原件并发送邮件。", sources,
                        )
                        self.assertIn(f"{field}：{value}", answer)
                        self.assertNotIn("发送邮件", answer)
                        self.assertEqual(selected[0]["url"], "/api/notes/55")
                        self.assertEqual(selected[0]["evidence_locations"], ["正文 4"])

    def test_single_field_requires_a_subject_and_explicit_possessive_separator(self) -> None:
        for question in (
            "核心功能是什么", "关于核心功能是什么", "的输入资料是什么",
            "专题报告助手核心功能是什么", "资料核对器输入资料是什么",
            "专题报告助手的未知能力是什么", "这个的核心功能是什么",
            "专题报告助手和资料核对器的核心功能分别是什么",
        ):
            with self.subTest(question=question):
                self.assertEqual(RagPipeline.parallel_field_subject_terms(question), [])
        self.assertGreater(len(RagPipeline.compound_query_clauses(
            "专题报告助手和资料核对器的核心功能分别是什么",
        )), 1)

    def test_relationship_field_lookups_keep_the_named_source_and_all_three_risks(self) -> None:
        for position in ("里", "中", "内"):
            for verb in ("提到", "包含", "列出"):
                for requested in ("风险", "风险和输入资料"):
                    question = f"项目复盘{position}{verb}的{requested}是什么？"
                    with self.subTest(question=question):
                        self.assertEqual(RagPipeline.relationship_source_terms(question), ["项目复盘"])
                        self.assertEqual(RagPipeline.parallel_field_subject_terms(question), [])
                        self.assertEqual(RagPipeline.named_structured_subject_terms(question), [])
                        self.assertEqual(RagPipeline.required_retrieval_topics(question), ["项目复盘"])
                        self.assertFalse(RagPipeline.requires_definition_evidence(question))

        fixture = json.loads((Path(__file__).parent / "fixtures" / "rag_regression_user_feedback_portable.json").read_text(encoding="utf-8"))
        case = next(case for case in fixture["cases"] if case["id"] == "relation-001")
        document = next(document for document in fixture["documents"] if document["title"] == case["expected_titles"][0])
        source = {
            "index": 1, "id": 3, "title": document["title"],
            "evidence": document["content"], "fallback_excerpt": document["content"],
            "location": "正文 1", "url": "/api/notes/3",
            "matched_terms": ["项目复盘", "风险"],
        }
        answer, selected = RagPipeline.enforce_answer_contract(
            case["question"], "来源[1] 风险一：导入资料主题过杂。", [source],
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["title"], document["title"])
        for expected in case["expected_answer_terms"]:
            self.assertIn(expected, answer)

    def test_single_field_cannot_borrow_wrong_names_missing_values_or_conflicting_rows(self) -> None:
        pipeline = RagPipeline(None, None)
        row = "| 8 | 专题报告助手 | 从选定资料组织可编辑 HTML 初稿 | 项目汇报 | 已选笔记、文档 | 专题报告 | 演示负责人 | 已验证 |"
        for field, value in (
            ("核心功能", "从选定资料组织可编辑 HTML 初稿"),
            ("输入资料", "已选笔记、文档"),
        ):
            question = f"专题报告助手的{field}是什么"
            missing = row.replace(f"| {value} |", "|  |")
            neighbour = row.replace("专题报告助手", "资料核对器")
            conflicting = row.replace(f"| {value} |", "| 另一版本的值 |")
            for label, content, fragment in (
                ("missing", self.table(neighbour, missing), neighbour + "\n" + missing),
                ("conflicting_value", self.table(row) + "\n\n" + self.table(conflicting), row),
                ("conflicting_header", self.table(row) + "\n\n" + self.table(row, header=self.header.replace(field, "备注")), row),
            ):
                with self.subTest(field=field, boundary=label):
                    body = self.chunk(content, fragment)
                    body["hits"] = ["专题报告助手", field]
                    self.assertEqual(pipeline.source_payloads(question, [body]), [])
            for name in ("报告助手", "专题报告助手增强版", "资料核对器"):
                wrong_question = f"{name}的{field}是什么"
                with self.subTest(field=field, wrong_name=name):
                    body = self.chunk(self.table(row), row)
                    body["hits"] = [name, field]
                    self.assertEqual(pipeline.source_payloads(wrong_question, [body]), [])
                    # Also guard an already-normalized wrong row at the final
                    # contract, where a generic evidence path could expose it.
                    source = {
                        "index": 1, "title": "Agent场景清单",
                        "evidence": f"Agent名称：专题报告助手；{field}：{value}",
                    }
                    answer, selected = pipeline.enforce_answer_contract(
                        wrong_question, f"来源[1] {field}是{value}。", [source],
                    )
                    self.assertEqual(selected, [])
                    self.assertNotIn(value, answer)

    def test_non_agent_name_cannot_borrow_an_existing_neighbour_or_substring_name(self) -> None:
        pipeline = RagPipeline(None, None)
        row = "| 8 | 专题报告助手 | 从选定资料组织可编辑 HTML 初稿 | 项目汇报 | 已选笔记、文档 | 专题报告 | 演示负责人 | 已验证 |"
        body = self.chunk(self.table(row), row)
        body["hits"] = ["报告助手", "资料"]
        for name in ("报告助手", "专题报告助手增强版", "资料核对器"):
            with self.subTest(name=name):
                question = f"{name}输入资料和核心功能是什么"
                sources = pipeline.source_payloads(question, [body])
                answer, selected = pipeline.enforce_answer_contract(
                    question, "现有笔记没有足够信息。", sources,
                )
                self.assertEqual(selected, [])
                self.assertNotIn("已选笔记、文档", answer)

    def test_non_agent_name_keeps_missing_and_conflicting_field_refusals(self) -> None:
        pipeline = RagPipeline(None, None)
        question = "专题报告助手的输入资料和核心功能分别是什么"
        row = "| 8 | 专题报告助手 | 从选定资料组织可编辑 HTML 初稿 | 项目汇报 | 已选笔记、文档 | 专题报告 | 演示负责人 | 已验证 |"
        missing = row.replace("| 已选笔记、文档 |", "|  |")
        conflicting = row.replace("| 已选笔记、文档 |", "| 待核对记录 |")
        for content, fragment in (
            (self.table(missing), missing),
            (self.table(row) + "\n\n" + self.table(conflicting), row),
        ):
            with self.subTest(content=content):
                body = self.chunk(content, fragment)
                body["hits"] = ["专题报告助手", "资料"]
                self.assertEqual(pipeline.source_payloads(question, [body]), [])

    def test_missing_input_cannot_be_borrowed_from_a_neighbouring_row(self) -> None:
        pipeline = RagPipeline(None, None)
        missing = self.row.replace("| 文件版本 |", "|  |")
        neighbour = self.row.replace("文件更新检查 Agent", "项目风险扫描 Agent")
        chunk = self.chunk(self.table(neighbour, missing), neighbour + "\n" + missing)
        self.assertEqual(pipeline.source_payloads(self.question, [chunk]), [])
        missing_header = self.header.replace("输入资料", "备注")
        chunk = self.chunk(self.table(self.row, header=missing_header))
        self.assertEqual(pipeline.source_payloads(self.question, [chunk]), [])

    def test_no_header_or_no_matching_retrieved_row_does_not_infer_columns(self) -> None:
        pipeline = RagPipeline(None, None)
        self.assertIsNone(pipeline.markdown_table_field_evidence(self.question, self.chunk(self.row)))
        wrong_fragment = self.row.replace("文件更新检查 Agent", "文件删除检查 Agent")
        self.assertIsNone(pipeline.markdown_table_field_evidence(
            self.question, self.chunk(self.table(self.row), wrong_fragment),
        ))

    def test_same_named_rows_with_conflicting_headers_or_values_are_refused(self) -> None:
        pipeline = RagPipeline(None, None)
        swapped_header = self.header.replace("输入资料 | 输出", "输出 | 输入资料")
        for other_table in (
            self.table(self.row, header=swapped_header),
            self.table(self.row.replace("文件版本", "导入日志")),
        ):
            with self.subTest(other_table=other_table):
                content = self.table(self.row) + "\n\n" + other_table
                self.assertEqual(pipeline.source_payloads(self.question, [self.chunk(content)]), [])

    def test_child_tab_uses_its_own_header_and_keeps_the_tab_locator(self) -> None:
        child = SimpleNamespace(id=56, content=self.table(self.row))
        pipeline = RagPipeline(SimpleNamespace(get_note=lambda note_id: child), None)
        parent = "父笔记没有表格。"
        chunk = self.chunk(parent)
        chunk.update(source_id=56, source_group="note-56", location="页签2·正文 4")
        sources = pipeline.source_payloads(self.question, [chunk])
        self.assertIn("输入资料：文件版本", sources[0]["evidence"])
        self.assertEqual(sources[0]["url"], "/api/notes/55?tab_id=56")


if __name__ == "__main__":
    unittest.main()
