# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import copy
import hashlib
import os
import re
import subprocess
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from pocket_memory.rag.pipeline import RagPipeline
from pocket_memory.rag.intent import IntentRouter
from pocket_memory.rag.query import QueryResolver
from pocket_memory.chunking import split_note_text


class RagRankingTests(unittest.TestCase):
    def test_weighted_rrf_is_default_and_legacy_override_remains_available(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RagPipeline(None, None).fusion_mode, "weighted_rrf")
        self.assertEqual(RagPipeline(None, None, fusion_mode="legacy").fusion_mode, "legacy")
        self.assertEqual(RagPipeline(None, None, fusion_mode="unknown").fusion_mode, "weighted_rrf")

    def test_query_term_selection_is_stable_across_python_hash_seeds(self) -> None:
        code = (
            "import json; "
            "from pocket_memory.rag.pipeline import RagPipeline; "
            "terms = RagPipeline.query_terms('供应商收款账户变更后要重新做哪些校验？'); "
            "print(json.dumps(RagPipeline.focused_query_terms(terms), ensure_ascii=True))"
        )
        outputs = []
        for seed in ("1", "91357"):
            environment = dict(os.environ)
            environment["PYTHONHASHSEED"] = seed
            outputs.append(subprocess.check_output(
                [sys.executable, "-c", code],
                env=environment,
                text=True,
                encoding="utf-8",
            ).strip())

        self.assertEqual(outputs[0], outputs[1])

    def test_weighted_rrf_combines_parent_chunk_fts_and_lexical_votes(self) -> None:
        note = type("Note", (), {"id": 7, "updated_at": "2026-01-01"})()
        correct = {
            "note": note, "chunk_identity": "correct", "rank_score": 20.0,
            "score": 0.66, "lexical_score": 13.0, "fts_score": 5.2,
            "fusion_channels": {"semantic_parent": 1, "semantic_chunk": 1, "fts": 2},
            "hits": ["账户变更后", "供应商", "校验", "重新"],
            "title": "采购审批流重构方案与灰度复盘",
            "raw_text": "供应商收款账户变更后，只需重新做银行账户真实性和授权人双重校验。",
        }
        distractor = {
            "note": note, "chunk_identity": "distractor", "rank_score": 25.0,
            "score": 0.56, "lexical_score": 16.0, "fts_score": 5.3,
            "fusion_channels": {"semantic_parent": 2, "semantic_chunk": 3, "fts": 1},
            "hits": ["账户变更后", "供应商", "校验", "重新"],
            "title": "供应商履约风险周报与行动清单",
            "raw_text": "付款异常不等同于采购审批流中的银行账户重新校验。",
        }

        fused = RagPipeline.apply_weighted_rrf([correct, distractor])
        reranked = RagPipeline.rerank_candidates(
            "供应商收款账户变更后要重新做哪些校验？",
            fused,
        )

        self.assertGreater(correct["rank_score"], distractor["rank_score"])
        self.assertEqual(
            set(reranked[0]["fusion_channels"]),
            {"semantic_parent", "semantic_chunk", "fts", "lexical"},
        )

    def test_candidate_channel_merge_deduplicates_one_persisted_chunk(self) -> None:
        note = type("Note", (), {"id": 7})()
        parent = {
            "note": note, "chunk_identity": "note-chunk-1", "rank_score": 12.0,
            "score": 0.61, "lexical_score": 4.0, "fts_score": 5.5,
            "fusion_channels": {"semantic_parent": 2, "fts": 1}, "hits": ["规则"],
        }
        chunk = {
            "note": note, "chunk_identity": "note-chunk-1", "rank_score": 10.0,
            "score": 0.68, "lexical_score": 3.0, "fts_score": 0.0,
            "fusion_channels": {"semantic_chunk": 1}, "hits": ["审批"],
        }

        merged = RagPipeline.merge_candidate_channels([parent, chunk])

        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0]["fusion_channels"],
            {"semantic_parent": 2, "fts": 1, "semantic_chunk": 1},
        )
        self.assertEqual(merged[0]["score"], 0.68)
        self.assertEqual(merged[0]["hits"], ["规则", "审批"])

    def test_direct_action_source_filter_removes_definition_only_neighbour(self) -> None:
        correct = {
            "index": 1,
            "title": "采购审批流重构方案与灰度复盘",
            "evidence": "已准入供应商发生收款账户变更时，必须重新校验银行账户，不需要重复完成其他三项。",
            "fallback_excerpt": "供应商准入规则。",
        }
        distractor = {
            "index": 2,
            "title": "供应商履约风险周报与行动清单",
            "evidence": "付款异常指账户变更后发生重复付款。",
            "fallback_excerpt": "付款异常不等同于采购审批流中的银行账户重新校验。",
        }

        retained = RagPipeline.retain_direct_action_sources(
            "供应商收款账户变更后要重新做哪些校验？",
            [correct, distractor],
        )

        self.assertEqual(retained, [correct])
        self.assertEqual(retained[0]["index"], 1)

    def test_direct_action_source_filter_keeps_boundary_comparison_sources(self) -> None:
        sources = [
            {
                "index": 1,
                "title": "采购审批流",
                "evidence": "账户变更后必须重新校验银行账户。",
                "fallback_excerpt": "",
            },
            {
                "index": 2,
                "title": "付款异常口径",
                "evidence": "付款异常不等同于银行账户重新校验。",
                "fallback_excerpt": "",
            },
        ]

        retained = RagPipeline.retain_direct_action_sources(
            "付款异常是否等同于银行账户重新校验？",
            sources,
        )

        self.assertEqual(retained, sources)

    def test_chinese_topic_in_title_outranks_incidental_model_identifier(self) -> None:
        topic_bonus = RagPipeline.title_topic_bonus(
            "BGE-small-zh 向量检索方案对比",
            ["向量检索方", "检索方案对比"],
        )
        identifier_bonus = RagPipeline.title_topic_bonus(
            "Qwen3-0.6B 本地部署笔记",
            ["Qwen3-0", "6B"],
        )

        self.assertGreater(topic_bonus, identifier_bonus)
        self.assertLessEqual(topic_bonus, 3.0)

    def test_title_chunk_does_not_displace_available_content_evidence(self) -> None:
        note = type("Note", (), {"id": 7})()
        title = {"note": note, "source_id": 7, "kind": "title", "excerpt": "主题标题", "hits": ["主题"]}
        content = {"note": note, "source_id": 7, "kind": "content", "excerpt": "主题的具体参数是 >10%。", "hits": ["主题"]}

        selected = RagPipeline.dedupe_chunks([title, content], limit=3)

        self.assertEqual(selected, [content])

    def test_parent_child_context_replaces_heading_with_answer_sibling(self) -> None:
        note = SimpleNamespace(id=7, title="Vue3 + PyWebView 桌面打包踩坑", updated_at="2026-01-01")
        siblings = [
            SimpleNamespace(
                id=1, note_id=7, title=note.title,
                text="PyInstaller 打包 Pocket Memory 桌面版遇到的问题：",
                location="正文 1", kind="content", chunk_order=0,
            ),
            SimpleNamespace(
                id=2, note_id=7, title=note.title,
                text="常见问题包括动态库丢失、资源路径错误和 WebView2 缺失。",
                location="正文 2", kind="content", chunk_order=1,
            ),
            SimpleNamespace(
                id=3, note_id=7, title=note.title,
                text="最终打包结构：PocketMemory.exe + _internal/ + models/ + runtime/。",
                location="正文 3", kind="content", chunk_order=2,
            ),
        ]

        class Store:
            @staticmethod
            def list_note_chunk_window(note_id: int, center_order: int, radius: int = 2):
                return [chunk for chunk in siblings if abs(chunk.chunk_order - center_order) <= radius]

        anchor = {
            "note": note, "source_id": 7, "source_group": "note-7",
            "chunk_identity": "note-chunk-1", "note_chunk_id": 1,
            "note_chunk_order": 0, "kind": "content", "title": note.title,
            "raw_text": siblings[0].text, "excerpt": siblings[0].text,
            "location": "正文 1", "hits": ["PyInstaller", "打包"],
            "lexical_score": 9.0, "fts_score": 5.0, "score": 0.65,
            "rank_score": 30.0, "reason": "正文命中：PyInstaller、打包",
            "fusion_channels": {},
        }
        diagnostics = {}

        selected = RagPipeline(Store(), None).expand_parent_child_context(
            "PyInstaller 最终目录里有哪些核心项？",
            [anchor],
            focus_terms=["PyInstaller", "最终目录", "核心项"],
            limit=5,
            diagnostics=diagnostics,
        )

        self.assertEqual(len(selected), 1)
        self.assertIn("PocketMemory.exe", selected[0]["raw_text"])
        self.assertTrue(selected[0]["context_expanded"])
        self.assertIn("PyInstaller", selected[0]["context_anchor_text"])
        self.assertTrue(RagPipeline.has_answerable_evidence(
            "PyInstaller 最终目录里有哪些核心项？",
            selected,
        ))
        self.assertEqual(diagnostics["replaced_chunk_count"], 1)

    def test_structure_field_bonus_ignores_component_name_substrings(self) -> None:
        question = "PyInstaller 最终目录里有哪些核心项？"
        implementation_problem = (
            "构建时需要处理 onnxruntime 动态库，并通过 --add-data 参数打包资源。"
        )
        final_structure = (
            "最终打包结构：PocketMemory.exe + _internal/ + models/ + runtime/。"
        )

        self.assertEqual(
            0.0,
            RagPipeline.requested_field_evidence_bonus(question, implementation_problem),
        )
        self.assertGreater(
            RagPipeline.requested_field_evidence_bonus(question, final_structure),
            0.0,
        )

    def test_parent_child_context_selects_requested_search_method(self) -> None:
        note = SimpleNamespace(id=8, title="MySQL 慢查询优化：笔记全文检索", updated_at="2026-01-01")
        siblings = [
            SimpleNamespace(
                id=11, note_id=8, title=note.title,
                text="排查发现 FTS5 因 LIKE 查询走了全表扫描。",
                location="正文 2", kind="content", chunk_order=1,
            ),
            SimpleNamespace(
                id=12, note_id=8, title=note.title,
                text="优化方案：关键词长度 >= 2 走 FTS5 MATCH + bm25() 排序；单字回退 instr()。",
                location="正文 3", kind="content", chunk_order=2,
            ),
        ]

        class Store:
            @staticmethod
            def list_note_chunk_window(note_id: int, center_order: int, radius: int = 2):
                return siblings

        anchor = {
            "note": note, "source_id": 8, "source_group": "note-8",
            "chunk_identity": "note-chunk-11", "note_chunk_id": 11,
            "note_chunk_order": 1, "kind": "content", "title": note.title,
            "raw_text": siblings[0].text, "excerpt": siblings[0].text,
            "location": "正文 2", "hits": ["关键词"], "lexical_score": 3.0,
            "fts_score": 0.0, "score": 0.62, "rank_score": 24.0,
            "reason": "正文命中：关键词", "fusion_channels": {},
        }

        selected = RagPipeline(Store(), None).expand_parent_child_context(
            "两字及以上关键词走什么检索方式？",
            [anchor],
            focus_terms=["两字", "关键词", "检索方式"],
            limit=5,
        )

        self.assertIn("MATCH + bm25", selected[0]["raw_text"])

    def test_parent_child_context_does_not_pull_unrelated_neighbour(self) -> None:
        note = SimpleNamespace(id=9, title="本地模型部署", updated_at="2026-01-01")
        siblings = [
            SimpleNamespace(
                id=21, note_id=9, title=note.title,
                text="模型运行内存上限为 4GB，超过后应拒绝加载。",
                location="正文 1", kind="content", chunk_order=0,
            ),
            SimpleNamespace(
                id=22, note_id=9, title=note.title,
                text="下一阶段计划重新设计欢迎页配色和导航动效。",
                location="正文 2", kind="content", chunk_order=1,
            ),
        ]

        class Store:
            @staticmethod
            def list_note_chunk_window(note_id: int, center_order: int, radius: int = 2):
                return siblings

        anchor = {
            "note": note, "source_id": 9, "source_group": "note-9",
            "chunk_identity": "note-chunk-21", "note_chunk_id": 21,
            "note_chunk_order": 0, "kind": "content", "title": note.title,
            "raw_text": siblings[0].text, "excerpt": siblings[0].text,
            "location": "正文 1", "hits": ["模型", "内存", "上限"],
            "lexical_score": 8.0, "fts_score": 5.0, "score": 0.70,
            "rank_score": 34.0, "reason": "正文命中：模型、内存、上限",
            "fusion_channels": {},
        }

        selected = RagPipeline(Store(), None).expand_parent_child_context(
            "模型运行内存上限是多少？",
            [anchor],
            focus_terms=["模型", "运行内存", "上限"],
            limit=5,
        )

        self.assertEqual(selected[0]["chunk_identity"], "note-chunk-21")
        self.assertNotIn("欢迎页", selected[0]["raw_text"])

    def test_requested_field_bonus_prefers_performance_value_over_root_cause(self) -> None:
        question = "OCR 和向量检索的性能目标分别是多少？"
        root_cause = "根因是 4K 截图让 ORT 分配 2GB 内存。"
        measured_result = "单图 OCR 延迟从 4-8 秒降到 1.5-2.5 秒。"

        self.assertGreater(
            RagPipeline.requested_field_evidence_bonus(question, measured_result),
            RagPipeline.requested_field_evidence_bonus(question, root_cause),
        )

    def test_rate_question_prefers_matching_percentage_over_workflow_duration(self) -> None:
        question = "供应商A黄色预警的交货延迟率是多少？"
        threshold = "交货延迟率 > 5% 触发黄色预警。"
        workflow = "异常处置流程：采购经理 24 小时内确认，3 个工作日内出报告。"

        self.assertGreater(
            RagPipeline.requested_field_evidence_bonus(question, threshold),
            RagPipeline.requested_field_evidence_bonus(question, workflow),
        )

    def test_duration_question_prefers_requested_unit_and_phase(self) -> None:
        current_question = "采购审批当前平均耗时多少天？"
        target_question = "采购审批重构后的目标耗时多少天？"
        current = "现状问题：三级审批平均耗时 5.2 天。"
        per_item = "风控评估依赖人工，平均每单 2.5 小时。"
        target = "目标：审批平均耗时从 5.2 天降到 2.5 天。"

        self.assertGreater(
            RagPipeline.requested_field_evidence_bonus(current_question, current),
            RagPipeline.requested_field_evidence_bonus(current_question, per_item),
        )
        self.assertGreater(
            RagPipeline.requested_field_evidence_bonus(target_question, target),
            RagPipeline.requested_field_evidence_bonus(target_question, current),
        )

    def test_dedupe_keeps_bounded_multiple_evidences_from_one_source(self) -> None:
        note = type("Note", (), {"id": 7})()
        first = {"note": note, "source_id": 7, "source_group": "note-7", "chunk_identity": "note-chunk-1", "kind": "content", "excerpt": "条件一：审批前需要法务复核。", "hits": ["审批"], "lexical_score": 4}
        second = {"note": note, "source_id": 7, "source_group": "note-7", "chunk_identity": "note-chunk-2", "kind": "content", "excerpt": "条件二：付款前需要财务复核。", "hits": ["付款"], "lexical_score": 4}
        third = {"note": type("Note", (), {"id": 8})(), "source_id": 8, "source_group": "note-8", "chunk_identity": "note-chunk-3", "kind": "content", "excerpt": "项目背景说明。", "hits": ["项目"], "lexical_score": 2}

        selected = RagPipeline.dedupe_chunks([first, second, third], limit=3)

        self.assertEqual([chunk["chunk_identity"] for chunk in selected], ["note-chunk-1", "note-chunk-3", "note-chunk-2"])

    def test_parameter_and_in_note_time_questions_stay_on_qa_path(self) -> None:
        self.assertEqual(IntentRouter.detect("FTS5 在多少条笔记时性能异常？").type, "qa")
        self.assertEqual(IntentRouter.detect("OCR 图片最长边要缩放到多少？").type, "qa")
        self.assertEqual(IntentRouter.detect("本周 RAG 学习安排是什么？").type, "qa")
        for question in (
            "提示词注入是什么？",
            "提示词注入是啥",
            "提示词注入啥意思？",
            "给我解释一下提示词注入",
        ):
            with self.subTest(question=question):
                self.assertEqual(IntentRouter.detect(question).type, "qa")

    def test_actual_note_count_question_still_uses_stats(self) -> None:
        self.assertEqual(IntentRouter.detect("本周写了多少条笔记？").type, "stats")
        self.assertEqual(IntentRouter.detect("我的笔记有多少条？").type, "stats")
        self.assertEqual(IntentRouter.detect("一共 几个笔记").type, "stats")
        self.assertEqual(IntentRouter.detect("一共多少笔记").type, "stats")
        self.assertEqual(IntentRouter.detect("有几篇记录").type, "stats")
        self.assertEqual(IntentRouter.detect("异步任务最多重试几次？").type, "qa")
        self.assertEqual(IntentRouter.detect("线性扫描建议控制在多少条以内？").type, "qa")

    def test_absolute_dates_in_content_questions_are_not_note_timestamp_queries(self) -> None:
        self.assertFalse(IntentRouter._is_note_timestamp_query("北辰项目 9 月 8 日是全量上线吗？"))
        self.assertTrue(IntentRouter._is_note_timestamp_query("9 月 8 日创建的笔记有哪些？"))

    def test_general_facts_with_count_or_superlative_use_qa(self) -> None:
        self.assertEqual(IntentRouter.detect("太阳系有多少颗行星？").type, "qa")
        self.assertEqual(IntentRouter.detect("这份通识笔记里人体有多少块骨头？").type, "qa")
        self.assertEqual(IntentRouter.detect("太阳系最大的行星是哪一颗？").type, "qa")

    def test_compact_topic_anchor_rejects_a_partial_keyword_hit(self) -> None:
        self.assertEqual(RagPipeline.topic_anchor_terms("提示词注入是啥"), ["提示词注入"])
        self.assertEqual(RagPipeline.topic_anchor_terms("给我解释一下提示词注入"), ["提示词注入"])
        self.assertEqual(RagPipeline.topic_anchor_terms("供应商有没有风险？"), [])
        self.assertEqual(RagPipeline.topic_anchor_terms("这家公司的注册信息是什么？"), [])
        self.assertEqual(RagPipeline.search_anchor_terms("提示词注入"), ["提示词注入"])
        self.assertEqual(RagPipeline.search_anchor_terms("供应商有没有风险？"), [])
        self.assertEqual(RagPipeline.answer_anchor_terms("提示词注入"), ["提示词注入"])
        note = type("Note", (), {"id": 7, "title": "AI 安全"})()
        partial = {
            "note": note, "source_id": 7, "title": "AI 安全", "raw_text": "请在 Chat 框里输入提示词。",
            "excerpt": "请在 Chat 框里输入提示词。", "hits": ["提示词"], "score": 0.61,
            "lexical_score": 2, "rank_score": 7,
        }
        exact = {
            "note": type("Note", (), {"id": 8, "title": "提示词注入"})(), "source_id": 8,
            "title": "提示词注入", "raw_text": "提示词注入会诱导模型忽略原有指令。",
            "excerpt": "提示词注入会诱导模型忽略原有指令。", "hits": ["提示词注入"], "score": 0.61,
            "lexical_score": 4, "rank_score": 11,
        }

        kept = RagPipeline.filter_weak_chunks([partial, exact], topic_anchors=["提示词注入"])

        self.assertEqual(kept, [exact])

    def test_relationship_lookup_prioritizes_named_source_for_exact_recall(self) -> None:
        question = "项目复盘里提到的风险是什么？"
        focus = RagPipeline.focused_query_terms(RagPipeline.query_terms(question))
        required = RagPipeline.answer_anchor_terms(question)

        self.assertEqual(required, ["项目复盘"])
        self.assertEqual(
            RagPipeline.prioritize_required_terms(focus, required)[0],
            "项目复盘",
        )
        self.assertFalse(RagPipeline.requires_definition_evidence(question))

    def test_stable_technical_identifiers_become_retrieval_anchors(self) -> None:
        self.assertEqual(
            RagPipeline.retrieval_anchor_terms("采购审批如何用 bm25 排序？"),
            ["bm25"],
        )
        self.assertEqual(
            RagPipeline.subquery_anchor_terms("OCR 优化采取了什么手段"),
            ["OCR"],
        )
        self.assertEqual(
            RagPipeline.attribute_subject_terms("打包的内存或体积限制是什么"),
            ["打包"],
        )
        self.assertEqual(
            RagPipeline.attribute_subject_terms("模型部署的内存或体积限制是什么"),
            ["模型部署"],
        )
        self.assertEqual(
            RagPipeline.attribute_subject_recall_terms("模型部署的内存或体积限制是什么"),
            ["模型部署", "模型", "部署"],
        )
        self.assertEqual(
            RagPipeline.subquery_anchor_terms("模型部署的内存或体积限制是什么"),
            [],
        )
        self.assertEqual(RagPipeline.retrieval_identifier_terms("AI 能做什么？"), [])

    def test_stable_identifier_survives_broad_topic_pruning(self) -> None:
        broad_note = type("Note", (), {"id": 1, "title": "采购审批流重构方案"})()
        identifier_note = type("Note", (), {"id": 2, "title": "MySQL 慢查询优化"})()
        broad = {
            "note": broad_note,
            "source_id": 1,
            "title": broad_note.title,
            "raw_text": "采购审批采用三级审批流程。",
            "excerpt": "采购审批采用三级审批流程。",
            "hits": ["采购审批"],
            "score": 0.72,
            "lexical_score": 10.0,
            "rank_score": 100.0,
        }
        identifier = {
            "note": identifier_note,
            "source_id": 2,
            "title": identifier_note.title,
            "raw_text": "全文索引使用 bm25() 排序。",
            "excerpt": "全文索引使用 bm25() 排序。",
            "hits": ["bm25"],
            "score": 0.58,
            "lexical_score": 3.0,
            "rank_score": 70.0,
        }

        kept = RagPipeline.filter_weak_chunks(
            [broad, identifier],
            question="采购审批如何用 bm25 排序？",
        )

        self.assertIn(identifier, kept)

    def test_definition_question_requires_direct_topic_coverage_before_answer_or_fallback(self) -> None:
        partial = [{
            "title": "Prompt 编写", "raw_text": "演示时可以把提示词粘贴到 Chat 框。",
            "hits": ["提示词"], "score": 0.82,
        }]
        sources = [{
            "index": 1, "title": "Prompt 编写", "evidence": "演示时可以把提示词粘贴到 Chat 框。",
            "fallback_excerpt": "演示时可以把提示词粘贴到 Chat 框。", "matched_terms": ["提示词"],
        }]

        for question in ("提示词注入是啥", "提示词注入", "提示词"):
            with self.subTest(question=question):
                self.assertFalse(RagPipeline.has_answerable_evidence(question, partial))
                answer, selected = RagPipeline.enforce_answer_contract(
                    question, "根据笔记，提示词可以粘贴到 Chat 框。[1]", sources
                )
                self.assertEqual(answer, "现有笔记没有足够信息。")
                self.assertEqual(selected, [])

    def test_long_bare_topic_can_use_an_explicit_dated_outcome(self) -> None:
        question = "工业互联网与人工智能融合"
        chunks = [{
            "title": "行动方案", "raw_text": "到2028年，工业互联网与人工智能融合赋能水平显著提升。",
            "hits": ["工业互联网与", "人工智能融"], "score": 0.66,
        }]

        self.assertFalse(RagPipeline.requires_definition_evidence(question))
        self.assertTrue(RagPipeline.has_answerable_evidence(question, chunks))

    def test_dated_outcome_outranks_generic_same_document_passage(self) -> None:
        question = "哪一年，工业互联网与人工智能融合水平会提升？"
        generic = {
            "title": "行动方案", "raw_text": "强化对工业互联网与人工智能融合赋能的统筹协调。",
            "hits": ["工业互联网", "人工智能融"], "rank_score": 20.0,
        }
        dated = {
            "title": "行动方案", "raw_text": "到2028年，工业互联网与人工智能融合赋能水平显著提升。",
            "hits": ["工业互联网", "人工智能融"], "rank_score": 15.0,
        }

        ranked = RagPipeline.rerank_candidates(question, [generic, dated])

        self.assertGreater(ranked[1]["rank_score"], ranked[0]["rank_score"])

    def test_enumeration_evidence_keeps_every_labelled_item_and_contract_rejects_one_item(self) -> None:
        pipeline = object.__new__(RagPipeline)
        question = "项目有哪些风险？"
        text = "风险一：导入资料主题过杂，缺少直接回答问题的证据。风险二：长 Excel 或扫描 PDF 的原文很难阅读。风险三：本地 4B 模型首次问答需加载。"

        evidence = pipeline.enumerated_evidence(question, text)
        self.assertIn("风险一", evidence)
        self.assertIn("风险二", evidence)
        self.assertIn("风险三", evidence)

        sources = [{"index": 1, "evidence": evidence, "fallback_excerpt": evidence, "matched_terms": ["项目风险"]}]
        answer, selected = RagPipeline.enforce_answer_contract(question, "来源[1] 风险一：导入资料主题过杂。", sources)

        self.assertIn("风险三", answer)
        self.assertEqual(selected, sources)

    def test_count_phrased_enumeration_keeps_the_complete_labelled_set(self) -> None:
        question = "项目有几个风险？"
        text = "风险一：导入资料主题过杂。风险二：长文档不易定位。风险三：首次问答需要加载模型。"

        self.assertTrue(RagPipeline.is_enumeration_question(question))
        evidence = RagPipeline.enumerated_evidence(question, text)
        self.assertIn("风险一", evidence)
        self.assertIn("风险二", evidence)
        self.assertIn("风险三", evidence)

        sources = [{
            "index": 1,
            "evidence": evidence,
            "fallback_excerpt": evidence,
            "matched_terms": ["项目", "风险"],
        }]
        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "来源[1] 风险一：导入资料主题过杂。风险二：长文档不易定位。风险三：首次问答需要加载模型。",
            sources,
        )

        self.assertIn("共三个风险", answer)
        self.assertEqual(1, len(selected))
        self.assertEqual(1, selected[0]["index"])
        self.assertIn("风险三", selected[0]["evidence"])

    def test_answer_contract_restores_an_omitted_explicit_component(self) -> None:
        question = "PyInstaller 最终目录里有哪些核心项？"
        evidence = (
            "最终打包结构：PocketMemory.exe + _internal/ + models/ + runtime/，"
            "总体积约 1.8GB。"
        )
        sources = [{
            "index": 1,
            "title": "桌面打包记录",
            "evidence": evidence,
            "fallback_excerpt": evidence,
            "matched_terms": ["PyInstaller", "最终目录", "核心项"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "来源[1] PyInstaller最终目录中的核心项包括：_internal/、models/、runtime/。",
            sources,
        )

        self.assertIn("PocketMemory.exe", answer)
        self.assertIn("_internal/", answer)
        self.assertIn("models/", answer)
        self.assertIn("runtime/", answer)
        self.assertEqual(selected, sources)

    def test_explicit_component_guard_ignores_ordinary_comma_prose(self) -> None:
        question = "这个方案包含什么？"
        prose = "方案说明：系统先读取资料，然后建立索引，最后生成答案。"

        self.assertEqual(RagPipeline.explicit_component_list(question, prose), [])
        self.assertFalse(RagPipeline.answer_omits_explicit_component_list(
            question,
            "来源[1] 系统读取资料并生成答案。",
            [{"index": 1, "evidence": prose}],
        ))

    def test_enumeration_count_is_not_inferred_from_unlabelled_prose(self) -> None:
        answer = RagPipeline.complete_enumeration_count(
            "项目有几个风险？",
            "来源[1] 项目可能存在资料质量和性能风险。",
            [{"index": 1, "evidence": "项目可能存在资料质量和性能风险。"}],
        )

        self.assertNotIn("共", answer)

    def test_enumeration_keeps_numbered_sibling_chunks_before_generic_sources(self) -> None:
        pipeline = object.__new__(RagPipeline)
        question = "项目有哪些风险？"
        note = type("Note", (), {"id": 27})()
        risks = [
            "风险一：导入资料主题过杂，缺少直接回答问题的证据。",
            "风险二：长 Excel 或扫描 PDF 的原文很难阅读。",
            "风险三：本地 4B 模型首次问答需加载。",
        ]
        risk_chunks = [
            {
                "note": note,
                "source_id": 27,
                "source_group": "note-27",
                "chunk_identity": f"risk-{index}",
                "kind": "content",
                "title": "风险与行动项",
                "raw_text": risk,
                "excerpt": risk,
                "hits": ["风险"],
                "lexical_score": 6,
                "rank_score": 15 - index,
                "score": 0.7,
                "location": f"正文 {index}",
                "reason": "正文命中：风险",
            }
            for index, risk in enumerate(risks, 1)
        ]
        generic = {
            "note": type("Note", (), {"id": 23})(),
            "source_id": 23,
            "source_group": "note-23",
            "chunk_identity": "generic-risk",
            "kind": "content",
            "title": "AI+制造摘要",
            "raw_text": "项目风险需要通过来源核对降低。",
            "excerpt": "项目风险需要通过来源核对降低。",
            "hits": ["项目", "风险"],
            "lexical_score": 8,
            "rank_score": 30,
            "score": 0.7,
            "location": "正文 1",
            "reason": "正文命中：项目、风险",
        }

        selected = RagPipeline.dedupe_chunks([generic, *risk_chunks], limit=5, question=question)
        sources = pipeline.source_payloads(question, selected)

        self.assertEqual([chunk["chunk_identity"] for chunk in selected[:3]], ["risk-1", "risk-2", "risk-3"])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["title"], "风险与行动项")
        self.assertIn("风险一", sources[0]["evidence"])
        self.assertIn("风险二", sources[0]["evidence"])
        self.assertIn("风险三", sources[0]["evidence"])

    def test_principle_question_prefers_a_complete_numbered_source_set(self) -> None:
        pipeline = object.__new__(RagPipeline)
        question = "这次演示资料需要遵守哪些原则？"
        text = (
            "本次演示版本的产品范围做了三项明确取舍。"
            "第一，文件切片采用标准自动策略，优先保证原文、索引和引用位置一致。"
            "第二，知识地图和知识卡片暂时下线，不能为了展示新概念牺牲成品质量。"
            "第三，保留专题报告，用户主动选择资料后生成可编辑 HTML 初稿。"
        )

        evidence = pipeline.principle_evidence(question, text)

        self.assertIn("第一", evidence)
        self.assertIn("第二", evidence)
        self.assertIn("第三", evidence)

    def test_relation_lookup_anchors_the_named_note_not_the_full_question(self) -> None:
        self.assertEqual(
            RagPipeline.answer_anchor_terms("项目复盘里提到的风险是什么？"),
            ["项目复盘"],
        )
        self.assertTrue(RagPipeline.is_enumeration_question("项目复盘里提到的风险是什么？"))

    def test_flattened_ocr_agent_table_keeps_function_and_scene_in_one_row(self) -> None:
        pipeline = object.__new__(RagPipeline)
        compact_question = "项目风险扫描Agent 的核心功能和适用场景是什么？"
        spaced_question = "项目风险扫描 Agent 的核心功能和适用场景是什么？"
        text = (
            "编号 Agent名称 核心功能 适用场景 输入资料 输出 负责人 状态\n"
            "1会议行动项提取Agent 汇总会议结论和待办 周例会 会议纪要 行动清单 演示负责人 已验证\n"
            "2项目风险扫描 Agent 识别项目资料中的风险、缺口和待确认项 项目复盘 周报、问题清单 风险清单 演示负责人 已验证\n"
            "3制度问答Agent 基于制度资料回答流程问题并返回来源 制度查询 PDF、Word 带来源回答 演示负责人 试用"
        )
        generic = {
            "title": "无关资料",
            "raw_text": "项目风险需要通过来源核对。",
            "excerpt": "项目风险需要通过来源核对。",
            "hits": ["项目", "风险"],
            "rank_score": 35.0,
        }
        table = {
            "title": "Agent场景清单",
            "raw_text": text,
            "excerpt": text,
            "hits": ["项目风险扫描Agent"],
            "rank_score": 8.0,
        }

        compact_evidence = pipeline.evidence_sentence(compact_question, text, ["项目风险扫描Agent"])
        spaced_evidence = pipeline.evidence_sentence(spaced_question, text, ["项目风险扫描Agent"])
        ranked = RagPipeline.rerank_candidates(compact_question, [generic, table])

        self.assertEqual(
            compact_evidence,
            "Agent名称：项目风险扫描Agent；核心功能：识别项目资料中的风险、缺口和待确认项；适用场景：项目复盘。",
        )
        self.assertEqual(spaced_evidence, compact_evidence)
        self.assertGreater(ranked[1]["rank_score"], ranked[0]["rank_score"])

    def test_ocr_wrapped_cell_tail_is_restored_without_borrowing_another_row(self) -> None:
        question = "项目风险扫描Agent 的核心功能和适用场景是什么？"
        text = (
            "编号\nAgent名称\n核心功能\n适用场景\n"
            "2项目风险扫描Agent\n认项\n识别项目资料中的风险、缺口和待确\n项目复盘\n"
            "周报、问题清单\n风险清单\n"
            "3制度问答Agent\n基于制度资料回答流程问题\n制度查询"
        )

        evidence = RagPipeline.ocr_agent_table_evidence(question, text)

        self.assertEqual(
            evidence,
            "Agent名称：项目风险扫描Agent；核心功能：识别项目资料中的风险、缺口和待确认项；适用场景：项目复盘。",
        )

    def test_ocr_agent_table_evidence_strips_markdown_cell_borders(self) -> None:
        question = "项目风险扫描 Agent 的核心功能和适用场景是什么？"
        text = (
            "| 编号 | Agent名称 | 核心功能 | 适用场景 |\n"
            "| --- | --- | --- | --- |\n"
            "| 2 | 项目风险扫描 Agent | 识别项目资料中的风险、缺口和待确认项 | 项目复盘 |"
        )

        self.assertEqual(
            RagPipeline.ocr_agent_table_evidence(question, text),
            "Agent名称：项目风险扫描Agent；核心功能：识别项目资料中的风险、缺口和待确认项；适用场景：项目复盘。",
        )

    def test_narrow_ocr_d_artifact_is_normalized_without_harming_formula_symbol(self) -> None:
        self.assertEqual(RagPipeline.normalize_ocr_query("km 和 m d标准换算比例"), "km 和 m 的标准换算比例")
        self.assertEqual(RagPipeline.normalize_ocr_query("C 与 d d标准关系式"), "C 与 d 的标准关系式")

    def test_masked_ocr_date_adds_retrieval_anchors_without_guessing_the_digit(self) -> None:
        normalized = RagPipeline.normalize_ocr_query("2026-0?-24 这个 OCR 日期校验后是什么？")

        self.assertIn("2026-0?-24", normalized)
        self.assertTrue(normalized.endswith("OCR 日期 校验"))
        self.assertIn("OCR", RagPipeline.query_terms(normalized))

    def test_spaced_dates_match_a_compact_query_term(self) -> None:
        self.assertIn("9月8日", RagPipeline.matched_terms("9 月 8 日开始试点", ["9月8日"]))

    def test_formula_symbols_are_searchable_but_url_fragments_are_not_evidence(self) -> None:
        terms = RagPipeline.query_terms("C 与 d 的关系是什么？")
        self.assertIn("C", terms)
        self.assertIn("d", terms)
        self.assertEqual(RagPipeline.matched_terms("C = πd", ["C", "d"]), ["C", "d"])
        self.assertEqual(RagPipeline.matched_terms(RagPipeline.without_urls("https://example.com/c"), ["C"]), [])

    def test_evidence_prefers_a_later_concrete_conclusion_in_the_same_chunk(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = "供应商风险阈值用于初步筛查。审批说明见制度附录。供应商风险阈值为 50 万元，超过后需要升级审批。"

        evidence = pipeline.evidence_sentence("供应商风险阈值是多少？", text, ["供应商风险阈值"])

        self.assertIn("50 万元", evidence)

    def test_source_payload_prefers_a_numeric_threshold_over_a_scope_note(self) -> None:
        pipeline = object.__new__(RagPipeline)
        note = type("Note", (), {"id": 31})()
        common = {
            "note": note,
            "source_id": 31,
            "source_group": "note-31",
            "kind": "content",
            "title": "供应商A采购风控KRI阈值方案",
            "excerpt": "",
            "lexical_score": 7.0,
            "score": 0.7,
            "reason": "正文命中",
        }
        scope = {
            **common,
            "raw_text": "注意：该方案仅适用于 A 类核心供应商，B/C 类供应商只监控交货延迟率。",
            "hits": ["交货延迟率", "供应商", "阈值"],
            "rank_score": 110.0,
            "location": "正文 3",
        }
        threshold = {
            **common,
            "raw_text": "交货延迟率 > 5% 触发黄色预警。交货延迟率 > 10% 触发红色预警，暂停新订单。",
            "hits": ["交货延迟率", "供应商", "阈值"],
            "rank_score": 100.0,
            "location": "正文 1",
        }

        source = pipeline.source_payloads(
            "关于供应商A的交货延迟率红色预警，那它的阈值呢？",
            [scope, threshold],
        )[0]

        self.assertTrue(source["evidence"].startswith("交货延迟率 > 10%"))
        self.assertEqual(source["evidence_locations"][0], "正文 1")

    def test_source_payload_prefers_an_explicit_model_size_over_a_heading(self) -> None:
        pipeline = object.__new__(RagPipeline)
        note = type("Note", (), {"id": 32})()
        common = {
            "note": note,
            "source_id": 32,
            "source_group": "note-32",
            "kind": "content",
            "title": "BGE-small-zh 向量检索方案对比",
            "excerpt": "",
            "hits": ["BGE-small-zh", "向量检索方案"],
            "lexical_score": 20.0,
            "score": 0.7,
            "reason": "正文命中",
        }
        heading = {
            **common,
            "raw_text": "Pocket Memory 向量检索选型对比：",
            "rank_score": 115.0,
            "location": "正文 1",
        }
        values = {
            **common,
            "raw_text": "BGE-small-zh-v1.5（512 维，ONNX Q8 量化）：体积 95MB，中文语义好。",
            "rank_score": 105.0,
            "location": "正文 2",
        }

        source = pipeline.source_payloads(
            "关于BGE-small-zh向量检索方案，那模型多大？",
            [heading, values],
        )[0]

        self.assertIn("95MB", source["evidence"])
        self.assertEqual(source["evidence_locations"][0], "正文 2")

    def test_source_payload_prefers_selection_criteria_over_a_heading(self) -> None:
        pipeline = object.__new__(RagPipeline)
        note = type("Note", (), {"id": 33})()
        common = {
            "note": note,
            "source_id": 33,
            "source_group": "note-33",
            "kind": "content",
            "title": "BGE-small-zh 向量检索方案对比",
            "excerpt": "",
            "hits": ["BGE", "选型"],
            "lexical_score": 20.0,
            "score": 0.7,
            "reason": "正文命中",
        }
        heading = {
            **common,
            "raw_text": "Pocket Memory 向量检索选型对比：",
            "rank_score": 115.0,
            "location": "正文 1",
        }
        final_choice = {
            **common,
            "raw_text": "最终选 BGE-small-zh；笔记数超过 500 后再调整索引。",
            "rank_score": 110.0,
            "location": "正文 3",
        }
        criteria = {
            **common,
            "raw_text": (
                "BGE-small-zh-v1.5：体积 95MB，中文语义好，线性扫描延迟低；"
                "另一方案准确率略高但内存占用太重。"
            ),
            "rank_score": 100.0,
            "location": "正文 2",
        }

        source = pipeline.source_payloads(
            "BGE 选型时关注什么？",
            [heading, final_choice, criteria],
        )[0]

        self.assertIn("体积 95MB", source["evidence"])
        self.assertEqual(source["evidence_locations"][0], "正文 2")

    def test_evidence_sentence_preserves_decimals_and_version_numbers(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "冷却泵验收要求：稳定运行十分钟后，最大振动值不得超过 4.5 mm/s。"
            "该规则从 v1.1.1 开始执行。"
        )

        evidence = pipeline.evidence_sentence(
            "冷却泵验收允许的最大振动值是多少？",
            text,
            ["冷却泵验收", "最大振动值"],
        )
        candidates = [sentence for sentence, _section in pipeline._evidence_candidates(text)]

        self.assertIn("4.5 mm/s", evidence)
        self.assertIn("4.5 mm/s", candidates[0])
        self.assertIn("v1.1.1", candidates[1])

    def test_preparation_question_does_not_cite_a_nearby_risk_paragraph(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "命题\n"
            "先明确评审标准，再分别准备文字资料和视频展示，突出作品核心价值。\n"
            "风险\n"
            "时间有限，文字和视频可能准备不充分或重点不清。\n"
            "最小实验\n"
            "列出评审维度，写一页说明文档，录一段三分钟演示视频。"
        )

        evidence = pipeline.evidence_sentence("初赛的作品，需要准备什么？", text, ["初赛", "作品", "准备"])

        self.assertIn("准备文字资料和视频展示", evidence)
        self.assertNotIn("时间有限", evidence)

    def test_two_subject_role_question_prefers_explicit_assignments_over_a_shared_topic_title(self) -> None:
        pipeline = object.__new__(RagPipeline)
        question = "工业互联网和人工智能各自承担什么角色？"
        direct = {
            "title": "AI工业互联网协同赋能摘要.txt",
            "raw_text": "工业互联网承担连接与数据基础的角色，人工智能承担从已有信息中发现模式、生成摘要、回答问题和提出建议的角色。",
            "excerpt": "工业互联网承担连接与数据基础的角色，人工智能承担从已有信息中发现模式、生成摘要、回答问题和提出建议的角色。",
            "hits": ["工业互联网", "人工智能", "承担", "角色"],
            "rank_score": 26.0,
        }
        broad = {
            "title": "AI制造行动方案节选.pdf",
            "raw_text": "工业互联网和人工智能融合赋能行动方案。人工智能是战略性技术，工业互联网是重要基础设施。",
            "excerpt": "工业互联网和人工智能融合赋能行动方案。",
            "hits": ["工业互联网", "人工智能", "角色"],
            "rank_score": 39.0,
        }

        pipeline.rerank_candidates(question, [broad, direct])
        evidence = pipeline.evidence_sentence(question, direct["raw_text"], direct["hits"])

        self.assertGreater(direct["rank_score"], broad["rank_score"])
        self.assertIn("工业互联网承担连接与数据基础", evidence)
        self.assertIn("人工智能承担从已有信息中发现模式", evidence)

    def test_two_subject_role_question_accepts_adjacent_explicit_role_clauses(self) -> None:
        pipeline = object.__new__(RagPipeline)
        question = "工业互联网和人工智能各自承担什么角色？"
        text = (
            "工业互联网承担设备、系统、文档和人员之间的连接与数据基础角色；"
            "人工智能承担从已有信息中发现模式、生成摘要、回答问题和提出建议的角色。"
        )

        evidence = pipeline.role_comparison_evidence(question, text)

        self.assertIn("工业互联网承担", evidence)
        self.assertIn("人工智能承担", evidence)

    def test_two_subject_role_question_rejects_topic_only_adjacent_clauses(self) -> None:
        pipeline = object.__new__(RagPipeline)
        question = "工业互联网和人工智能各自承担什么角色？"
        text = (
            "工业互联网和人工智能融合赋能行动方案提出要推动产业升级；"
            "本节只说明政策方向，没有定义两者各自承担的具体角色。"
        )

        evidence = pipeline.role_comparison_evidence(question, text)

        self.assertEqual(evidence, "")

    def test_answer_contract_rejects_a_cited_source_title_for_a_role_question(self) -> None:
        question = "工业互联网和人工智能各自承担什么角色？"
        sources = [{
            "index": 1,
            "title": "AI制造行动方案节选.pdf",
            "evidence": "工业互联网和人工智能融合赋能行动方案。",
            "fallback_excerpt": "人工智能是战略性技术，工业互联网是重要基础设施。",
            "matched_terms": ["工业互联网", "人工智能"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            question, "工业互联网和人工智能融合赋能行动方案 [1]", sources
        )

        self.assertNotIn("融合赋能行动方案", answer)
        self.assertIn("人工智能是战略性技术", answer)
        self.assertEqual([source["title"] for source in selected], [sources[0]["title"]])
        self.assertIn("人工智能是战略性技术", selected[0]["evidence"])

    def test_same_source_prefers_direct_evidence_over_a_higher_ranked_generic_chunk(self) -> None:
        pipeline = object.__new__(RagPipeline)
        note = type("Note", (), {"id": 7})()
        generic = {
            "note": note,
            "source_id": 7,
            "source_group": "note-7",
            "title": "RAG 协作验收手册",
            "raw_text": "知识地图不是默认智能问答的资料源，不应替代手册验收。",
            "excerpt": "知识地图不是默认智能问答的资料源。",
            "hits": ["知识", "资料源"],
            "reason": "语义相关",
            "location": "正文 7",
            "kind": "content",
            "score": 0.78,
            "rank_score": 30.0,
        }
        direct = {
            "note": note,
            "source_id": 7,
            "source_group": "note-7",
            "title": "RAG 协作验收手册",
            "raw_text": "没有来源的字段是否显示为“待补充”，而不是模型猜测的结论。",
            "excerpt": "没有来源的字段是否显示为“待补充”。",
            "hits": ["没有来源的字段", "待补充"],
            "reason": "正文命中",
            "location": "正文 8",
            "kind": "content",
            "score": 0.61,
            "rank_score": 12.0,
        }

        source = pipeline.source_payloads("没有来源的字段应该怎么样？", [generic, direct])[0]

        self.assertEqual(source["evidence_locations"], ["正文 8", "正文 7"])
        self.assertIn("待补充", source["evidence"])

    def test_answer_contract_rejects_a_weaker_model_citation_when_source_one_has_the_exact_field(self) -> None:
        sources = [
            {
                "index": 1,
                "title": "RAG 协作验收手册",
                "evidence": "没有来源的字段是否显示为“待补充”，而不是模型猜测的结论。",
                "fallback_excerpt": "没有来源的字段是否显示为“待补充”。",
                "matched_terms": ["没有来源的字段"],
            },
            {
                "index": 2,
                "title": "交接记录",
                "evidence": "找不到明确来源的字段显示为“未在当前资料范围找到明确来源”。",
                "fallback_excerpt": "找不到明确来源的字段显示为“未在当前资料范围找到明确来源”。",
                "matched_terms": ["来源的字段"],
            },
        ]
        question = "没有来源的字段yinggai怎么样？"
        answer = "来源[2] 没有来源的字段显示为“未在当前资料范围找到明确来源”。"

        self.assertTrue(RagPipeline.answer_cites_weaker_source_than_direct_match(question, answer, sources))
        recovered, selected = RagPipeline.enforce_answer_contract(question, answer, sources)

        self.assertIn("待补充", recovered)
        self.assertEqual(selected, [sources[0]])

    def test_short_approval_follow_up_prefers_explicit_amount_comparison(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = "不同 PO 金额应该经过不同层级审批。例如金额越高，要求的审批人级别越高。"

        evidence = pipeline.evidence_sentence("同一级审批吗", text, ["审批"])

        self.assertEqual(evidence, text)

    def test_resolution_question_prefers_an_explicit_recommendation(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "审核期间网站打不开会影响备案。\n"
            "💡 核心建议：在提交公安备案申请的当天或前一天添加DNS A记录，并立即测试确保通过域名能访问网站。"
        )

        evidence = pipeline.evidence_sentence("审核时网站打不开怎么办", text, ["审核", "网站", "打不开"])

        self.assertEqual(
            evidence,
            "在提交公安备案申请的当天或前一天添加DNS A记录，并立即测试确保通过域名能访问网站。",
        )

    def test_evidence_keeps_the_explicit_negative_boundary(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = "72 小时内形成处置记录。这里的 72 小时是处置记录时限，不是对外通知承诺。"

        evidence = pipeline.evidence_sentence("72 小时具体指什么，不是对外通知承诺吗？", text, ["72", "小时", "对外通知"])

        self.assertIn("形成处置记录", evidence)
        self.assertIn("不是对外通知承诺", evidence)

    def test_numeric_lookup_inside_a_process_is_not_an_impossible_hard_anchor(self) -> None:
        clause = "数据泄露处置中的 72 小时具体指什么"

        self.assertEqual(RagPipeline.subquery_anchor_terms(clause), [])

    def test_boundary_continuation_inherits_the_first_clause_subject(self) -> None:
        self.assertEqual(
            RagPipeline.compound_query_clauses(
                "数据泄露处置中的 72 小时具体指什么，不能理解成什么？"
            ),
            [
                "数据泄露处置中的 72 小时具体指什么",
                "数据泄露处置中的 72 小时不能理解成什么",
            ],
        )

    def test_boundary_question_prefers_explicit_exclusion_over_a_matching_record(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "合同编号：HT-2026-027；合作伙伴：北辰云；付款节点：月度服务确认后 30 天；"
            "预付款比例：0%；币种：美元。\n"
            "说明：合同编号末尾数字不是项目风险编号。"
        )

        evidence = pipeline.evidence_sentence(
            "合同编号 HT-2026-027 末尾的 027 是项目风险编号吗？",
            text,
            ["HT-2026-027", "合同编号", "项目风险编号"],
        )

        self.assertEqual(evidence, "说明：合同编号末尾数字不是项目风险编号。")

    def test_duration_boundary_keeps_the_real_deadline_and_the_exclusion(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = "费用应在发生后 60 天内提交。这里的 60 天不是付款到账时限。"

        evidence = pipeline.evidence_sentence(
            "差旅报销的 60 天是到账时限吗？",
            text,
            ["60 天", "到账时限"],
        )

        self.assertIn("60 天内提交", evidence)
        self.assertIn("不是付款到账时限", evidence)

    def test_current_state_boundary_keeps_the_actual_architecture(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "当前版本使用笔记级语义向量与词面召回，命中后在候选笔记内做切片级定位。"
            "它不是全库切片级向量索引。"
        )

        evidence = pipeline.evidence_sentence(
            "系统现在是不是已经给所有切片建了全库向量索引？",
            text,
            ["全库向量索引", "切片", "索引"],
        )

        self.assertIn("笔记级语义向量", evidence)
        self.assertIn("候选笔记内", evidence)
        self.assertIn("不是全库切片级向量索引", evidence)

    def test_spreadsheet_record_keeps_name_function_and_scenario_together(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "编号：26；Agent名称：AI影子团队Agent；核心功能：创建项目的数字孪生Agent团队；"
            "适用场景：大型项目；亮点/关键词：组织数字孪生"
        )

        evidence = pipeline.evidence_sentence("AI影子团队Agent有什么样的功能，什么场景有用？", text, ["影子团队", "Agent", "功能", "场景", "AI"])
        sources = [
            {"index": 1, "title": "办公Agent创意.xlsx", "evidence": evidence, "matched_terms": ["影子团队", "Agent", "功能", "场景", "AI"]},
            {"index": 2, "title": "其他资料", "evidence": "Agent名称：其他Agent", "matched_terms": ["Agent", "功能", "场景", "AI"]},
        ]

        self.assertIn("核心功能：创建项目的数字孪生Agent团队", evidence)
        self.assertIn("适用场景：大型项目", evidence)
        self.assertTrue(RagPipeline.can_use_evidence_fallback(sources))
        fallback = RagPipeline.fallback_answer("有什么场景有用？", sources)
        self.assertIn("大型项目", fallback)
        self.assertNotIn("其他Agent", fallback)
        self.assertIn("核心功能：创建项目的数字孪生Agent团队", fallback)

    def test_long_spreadsheet_record_evidence_is_compact_and_keeps_rule_fields(self) -> None:
        pipeline = object.__new__(RagPipeline)
        record = (
            "编号：KRI-06；一级流程：合同签署；二级流程：合同审批；"
            "核心风险：合同签署未经恰当审批；风险监控指标：间接采购总监（大于50万或主框架），Central采购经理（10万到50万）；"
            "一期数据落地方案：" + "冗余说明" * 300
        )

        evidence = pipeline.compact_structured_record_evidence(record, "什么情况下需要人工审批确认", ["审批", "确认"])

        self.assertLessEqual(len(evidence), 520)
        self.assertIn("核心风险：合同签署未经恰当审批", evidence)
        self.assertIn("间接采购总监", evidence)

    def test_long_kri_evidence_keeps_the_monitoring_field_continuation(self) -> None:
        pipeline = object.__new__(RagPipeline)
        record = (
            "编号：KRI-06；核心风险：合同签署未经恰当审批；"
            "风险监控指标：[KRI 1] 合同审批未遵循CoA；[KRI 2] 授权签字人不符合要求：间接采购总监（大于50万或主框架）；"
            + "补充说明" * 300
        )

        evidence = pipeline.compact_structured_record_evidence(record, "什么情况下需要人工审批确认", ["审批", "确认"])

        self.assertIn("KRI 2", evidence)
        self.assertIn("间接采购总监", evidence)

    def test_note_splitter_does_not_cut_a_structured_row_at_semicolons(self) -> None:
        row_one = "编号：21；Agent名称：需求污染检测Agent；核心功能：识别需求偏离与范围失控；适用场景：产品研发；限制：不能替代需求评审结论。"
        row_two = "编号：26；Agent名称：AI影子团队Agent；核心功能：创建项目的数字孪生Agent团队；适用场景：大型项目；限制：输出需要项目经理确认。"
        text = "工作表：Agent 清单\n" + "\n".join([row_one, row_two, row_one, row_two, row_one])

        chunks = RagPipeline.split_text(text, target_size=240)

        target = next(chunk for chunk in chunks if "AI影子团队Agent" in chunk)
        self.assertIn("核心功能：创建项目的数字孪生Agent团队", target)
        self.assertIn("适用场景：大型项目", target)

    def test_structured_chunk_keeps_multiple_requested_agent_records(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "编号：21；Agent名称：需求污染检测Agent；核心功能：识别需求偏离与范围失控；适用场景：产品研发；亮点/关键词：需求漂移分析\n"
            "编号：26；Agent名称：AI影子团队Agent；核心功能：创建项目的数字孪生Agent团队；适用场景：大型项目；亮点/关键词：组织数字孪生"
        )

        evidence = pipeline.evidence_sentence(
            "需求污染检测Agent有什么功能？；AI影子团队Agent有什么功能和适用场景？",
            text,
            ["需求污染检测", "影子团队", "Agent"],
        )

        self.assertIn("需求污染检测Agent", evidence)
        self.assertIn("AI影子团队Agent", evidence)
        self.assertLess(evidence.index("需求污染检测Agent"), evidence.index("AI影子团队Agent"))

    def test_structured_chunk_separates_two_agent_tasks_in_one_question(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "编号：23；Agent名称：会议战斗力评分Agent；核心功能：评估会议是否有效推进；适用场景：管理协作；亮点/关键词：会议评分\n"
            "编号：28；Agent名称：隐形低效扫描Agent；核心功能：识别组织中的低效行为；适用场景：企业效率；亮点/关键词：流程优化"
        )

        evidence = pipeline.evidence_sentence(
            "会议是否有效推进需要用什么工具，并且我该如何扫描低效Agent？", text, ["会议", "有效推进", "低效Agent"]
        )
        sources = [{"index": 1, "title": "办公Agent创意", "evidence": evidence, "matched_terms": ["会议", "低效Agent"]}]
        answer, selected = RagPipeline.enforce_answer_contract(
            "会议是否有效推进需要用什么工具，并且我该如何扫描低效Agent？", "使用会议工具 [1]。", sources
        )

        self.assertIn("会议战斗力评分Agent", evidence)
        self.assertIn("隐形低效扫描Agent", evidence)
        self.assertIn("评估会议是否有效推进", answer)
        self.assertIn("识别组织中的低效行为", answer)
        self.assertEqual([source["title"] for source in selected], [sources[0]["title"]])
        self.assertIn("会议战斗力评分Agent", selected[0]["evidence"])
        self.assertIn("隐形低效扫描Agent", selected[0]["evidence"])

        noisy_sources = [*sources, {"index": 2, "title": "无关日志", "evidence": "Agent 正在扫描。", "matched_terms": ["Agent"]}]
        self.assertEqual(
            RagPipeline.fallback_sources(noisy_sources, force_multiple=True),
            sources,
        )

    def test_refusal_wording_used_by_local_model_triggers_evidence_recovery(self) -> None:
        answer = "现有笔记未提及“AI影子团队Agent”具体功能或场景，无法提供相关信息。"

        self.assertTrue(RagPipeline.answer_needs_fallback(answer))
        self.assertTrue(RagPipeline.answer_needs_fallback("现有笔记没有足够信息 [1]。"))

    def test_final_sources_follow_valid_answer_citations(self) -> None:
        sources = [{"index": 1, "title": "地球"}, {"index": 2, "title": "火星"}, {"index": 3, "title": "木卫三"}]

        selected = RagPipeline.cited_sources("地球是第三颗行星 [1]；火星有两颗卫星 [2]。", sources)

        self.assertEqual([source["title"] for source in selected], ["地球", "火星"])

    def test_answer_contract_rejects_an_uncovered_exact_value(self) -> None:
        sources = [{"index": 1, "evidence": "供应商 A 的交付准时率为 88%。", "fallback_excerpt": "供应商 A 的交付准时率为 88%。", "matched_terms": ["供应商 A", "交付准时率"]}]

        answer, selected = RagPipeline.enforce_answer_contract(
            "供应商 A 的交付准时率是多少？", "供应商 A 的交付准时率为 98% [1]。", sources
        )

        self.assertIn("88%", answer)
        self.assertEqual(selected, sources)

    def test_answer_contract_allows_a_question_value_used_against_an_evidence_range(self) -> None:
        sources = [{
            "index": 1,
            "evidence": "5 万元（含）至 20 万元（不含），由部门负责人、预算负责人及采购负责人审批。",
            "fallback_excerpt": "5 万元（含）至 20 万元（不含），由部门负责人、预算负责人及采购负责人审批。",
            "matched_terms": ["采购", "18 万元", "审批"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            "采购金额 18 万元需要哪些审批？",
            "18 万元需要部门负责人、预算负责人及采购负责人审批 [1]。",
            sources,
        )

        self.assertIn("18 万元", answer)
        self.assertIn("采购负责人", answer)
        self.assertEqual(selected, sources)

    def test_answer_evidence_mapping_rejects_a_cited_but_unrelated_claim(self) -> None:
        sources = [{
            "index": 1,
            "evidence": "工业互联网承担连接与数据基础的角色。",
            "fallback_excerpt": "工业互联网承担连接与数据基础的角色。",
            "matched_terms": ["工业互联网"],
        }]

        mapping = RagPipeline.answer_evidence_mapping(
            "工业互联网承担什么角色？",
            "火星有两颗卫星 [1]。",
            sources,
        )
        answer, selected = RagPipeline.enforce_answer_contract(
            "工业互联网承担什么角色？",
            "火星有两颗卫星 [1]。",
            sources,
        )

        self.assertEqual(mapping[0]["status"], "unsupported")
        self.assertIn("工业互联网", answer)
        self.assertNotIn("火星", answer)
        self.assertEqual(selected, sources)

    def test_answer_evidence_mapping_preserves_decimal_claims(self) -> None:
        sources = [{
            "index": 1,
            "evidence": "稳定速度为 4.5 mm/s，当前版本为 v1.1.1。",
            "fallback_excerpt": "稳定速度为 4.5 mm/s，当前版本为 v1.1.1。",
        }]

        mapping = RagPipeline.answer_evidence_mapping(
            "稳定速度和版本分别是什么？",
            "稳定速度为 4.5 mm/s [1]。当前版本为 v1.1.1 [1]。",
            sources,
        )

        self.assertEqual(len(mapping), 2)
        self.assertEqual([item["status"] for item in mapping], ["supported", "supported"])

    def test_single_source_trailing_citation_is_expanded_to_every_claim(self) -> None:
        sources = [{
            "index": 1,
            "evidence": "本次订单同步延迟不是支付接口导致。数据库不是根因，支付接口也未异常。同步延迟最高 47 分钟。",
            "fallback_excerpt": "本次订单同步延迟不是支付接口导致。数据库不是根因，支付接口也未异常。同步延迟最高 47 分钟。",
            "matched_terms": ["订单延迟", "支付接口"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            "这次订单延迟是支付接口拖慢的吗？",
            "不是。数据库不是根因，支付接口也未异常。同步延迟最高 47 分钟。 [1]",
            sources,
        )
        mapping = RagPipeline.answer_evidence_mapping(
            "这次订单延迟是支付接口拖慢的吗？",
            answer,
            selected,
        )

        self.assertEqual(3, len(mapping))
        self.assertTrue(all(item["citations"] == [1] for item in mapping))
        self.assertTrue(all(item["status"] == "supported" for item in mapping))
        self.assertEqual(4, answer.count("来源[1]"))

    def test_single_source_completion_does_not_duplicate_existing_sentence_citations(self) -> None:
        completed = RagPipeline.complete_single_source_citations(
            "第一项结论 [1]。第二项结论。第三项结论 [1]。"
        )

        self.assertEqual(3, completed.count("[1]"))

    def test_accepted_answer_realigns_the_displayed_evidence_to_its_claim(self) -> None:
        sources = [{
            "index": 1,
            "evidence": "工业互联网承担连接与数据基础的角色。",
            "fallback_excerpt": (
                "工业互联网承担连接与数据基础的角色。"
                "人工智能承担从已有信息中发现模式、生成摘要和回答问题的角色。"
            ),
            "matched_terms": ["工业互联网", "人工智能", "角色"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            "人工智能承担什么角色？",
            "人工智能承担发现模式、生成摘要和回答问题的角色 [1]。",
            sources,
        )

        self.assertIn("人工智能", answer)
        self.assertIn("人工智能承担", selected[0]["evidence"])

    @staticmethod
    def approval_band_source() -> dict:
        lower = "【新审批规则】单笔含税金额 5 万元以下，由部门负责人和预算负责人审批；"
        middle = "5 万元（含）至 20 万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        upper = "20 万元（含）以上，增加法务与财务负责人会签。"
        return {"index": 1, "source_id": "procurement", "title": "采购审批流重构方案与灰度复盘",
                "evidence": lower, "fallback_excerpt": lower + middle + upper,
                "location": "正文 1", "evidence_locations": ["正文 1"], "chunk_id": 7, "chunk_ids": [7],
                "matched_terms": ["采购", "金额", "审批"], "type": "note"}

    def test_amount_alignment_keeps_correct_evidence_for_original_18_wan_answer(self) -> None:
        source = self.approval_band_source()
        middle = "5 万元（含）至 20 万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        for current in (source["evidence"], middle):
            with self.subTest(current=current):
                selected_source = {**source, "evidence": current}
                answer, selected = RagPipeline.enforce_answer_contract(
                    "采购金额 18 万元需要哪些审批？",
                    "采购金额18万元需部门负责人、预算负责人及采购负责人审批 [1]。", [selected_source],
                )
                self.assertIn("采购负责人", answer)
                self.assertEqual(selected[0]["evidence"], middle)
                for key in ("source_id", "location", "evidence_locations", "chunk_id", "chunk_ids"):
                    self.assertEqual(selected[0][key], source[key])
                self.assertEqual(selected_source["evidence"], current)

    def test_amount_alignment_respects_original_5_and_20_wan_boundaries(self) -> None:
        source = self.approval_band_source()
        cases = [
            ("4.99", "部门负责人和预算负责人审批", "5 万元以下"),
            ("4.999999", "部门负责人和预算负责人审批", "5 万元以下"),
            ("5", "部门负责人、预算负责人及采购负责人审批", "20 万元（不含）"),
            ("19.99", "部门负责人、预算负责人及采购负责人审批", "20 万元（不含）"),
            ("19.999999", "部门负责人、预算负责人及采购负责人审批", "20 万元（不含）"),
            ("20", "增加法务与财务负责人会签", "20 万元（含）以上"),
            ("1000000", "增加法务与财务负责人会签", "20 万元（含）以上"),
        ]
        for value, approvers, required_band in cases:
            with self.subTest(value=value):
                selected = RagPipeline.align_sources_to_answer(
                    f"采购金额{value}万元需{approvers} [1]。", [source], question=f"采购金额{value}万元需要哪些审批？",
                )
                self.assertIn(required_band, selected[0]["evidence"])
                self.assertIn(approvers, selected[0]["evidence"])

    def test_amount_alignment_can_inherit_only_a_unique_question_amount(self) -> None:
        source = self.approval_band_source()
        answer = "需部门负责人、预算负责人及采购负责人审批 [1]。"
        _accepted, selected = RagPipeline.enforce_answer_contract("采购金额18万元需要哪些审批？", answer, [source])
        self.assertIn("20 万元（不含）", selected[0]["evidence"])
        self.assertEqual(RagPipeline._alignment_amount_hint(
            "金额5万元需三名负责人审批", "金额20万元需要什么审批？", source,
        ), "5万元")
        self.assertEqual(RagPipeline._alignment_amount_hint(
            "需部门负责人、预算负责人及采购负责人审批", "金额5万元和18万元如何审批？", source,
        ), "")
        self.assertEqual(RagPipeline._alignment_amount_hint(
            "金额18万元，仍按18.0万元确认", "金额20万元如何审批？", source,
        ), "18万元")

    def test_amount_alignment_does_not_treat_mixed_or_unsupported_amounts_as_absent(self) -> None:
        source = self.approval_band_source()
        claims = [
            "金额5万元与18万元都要审批", "金额18万元和500元", "金额18万元和2亿元",
            "金额18万美元", "USD180000", "金额-5万元", "金额负5万元", "金额十八万元",
            "金额500元", "金额2亿元", "金额1,000万元", "金额1e2万元", "金额5至20万元", "金额18",
            "金额18万元，预算500", "金额18万元和1e2万元",
            "金额18.00000000000000001万元与18.00000000000000002万元",
        ]
        for claim in claims:
            with self.subTest(claim=claim):
                self.assertEqual(RagPipeline._alignment_amount_hint(claim, "金额18万元如何审批？", source), "")
        self.assertEqual(RagPipeline._alignment_amount_hint("金额1.18万元需审批", "", source), "1.18万元")
        self.assertEqual(RagPipeline._alignment_amount_hint(
            "金额18万元需审批", "", {**source, "title": "美元采购审批规则"},
        ), "")

    def test_amount_alignment_does_not_borrow_a_range_from_an_uncited_source(self) -> None:
        source = self.approval_band_source()
        first = {**source, "fallback_excerpt": source["evidence"]}
        second = {**source, "index": 2, "source_id": "other-policy", "evidence": "其他制度的完整审批规则。"}
        answer = "金额18万元需部门负责人、预算负责人及采购负责人审批 [1]。"
        selected = RagPipeline.align_sources_to_answer(answer, [first, second], question="18万元需要哪些审批？")
        self.assertEqual(RagPipeline._alignment_amount_hint(answer, "", first), "")
        self.assertEqual(selected, [first, second])
        self.assertNotIn("20 万元（不含）", selected[0]["evidence"])

    def test_amount_alignment_does_not_move_evidence_to_an_unmapped_location(self) -> None:
        source = self.approval_band_source()
        variants = [
            {"location": "PDF 第1页", "evidence_locations": ["PDF 第1页", "PDF 第2页"]},
            {"location": "PDF 第1页", "evidence_locations": ["PDF 第1页"], "chunk_ids": [7, 8]},
            {"location": "", "evidence_locations": [], "chunk_id": None, "chunk_ids": []},
            {"location": "", "evidence_locations": [], "chunk_id": 7, "chunk_ids": [7]},
        ]
        for metadata in variants:
            with self.subTest(metadata=metadata):
                candidate = {**source, **metadata}
                self.assertEqual(RagPipeline._alignment_amount_hint("金额18万元需审批", "", candidate), "")
                selected = RagPipeline.align_sources_to_answer(
                    "金额18万元需部门负责人、预算负责人及采购负责人审批 [1]。", [candidate],
                )
                self.assertEqual(selected, [candidate])

    def test_amount_alignment_keeps_the_structured_identity_row(self) -> None:
        row = "设备编号：EQ-C31；采购金额：18万元；负责人：林岚；付款状态：待付款；付款比例：0%"
        source = {**self.approval_band_source(), "evidence": row,
                  "fallback_excerpt": row + "\n设备编号：EQ-C310；采购金额：5万元以下；负责人：周远；付款比例：80%"}
        self.assertEqual(RagPipeline._alignment_amount_hint("EQ-C31采购金额18万元", "", source), "")
        selected = RagPipeline.align_sources_to_answer("EQ-C31采购金额18万元，负责人林岚 [1]。", [source])
        self.assertEqual(selected, [source])
        self.assertNotIn("EQ-C310", selected[0]["evidence"])

    def test_amount_alignment_leaves_no_amount_and_multiple_claims_on_the_old_path(self) -> None:
        source = self.approval_band_source()
        self.assertEqual(RagPipeline._alignment_amount_hint("需要哪些审批", "谁来审批？", source), "")
        source["evidence"] = source["fallback_excerpt"]
        answer = "来源[1] 金额18万元需部门负责人、预算负责人及采购负责人审批。金额20万元增加法务与财务负责人会签。"
        selected = RagPipeline.align_sources_to_answer(answer, [source], question="金额18万元和20万元怎么审批？")
        # Preserve the baseline's lexical choice for multiple claims. Its
        # incomplete claim coverage is a separate issue, outside this hint.
        self.assertEqual(selected[0]["evidence"],
                         "【新审批规则】单笔含税金额 5 万元以下，由部门负责人和预算负责人审批；\n"
                         "20 万元（含）以上，增加法务与财务负责人会签。")
        first = self.approval_band_source()
        second = {**first, "index": 2, "source_id": "other-policy"}
        selected = RagPipeline.align_sources_to_answer(
            "金额18万元需部门负责人、预算负责人及采购负责人审批 [1]。"
            "金额19万元需部门负责人、预算负责人及采购负责人审批 [2]。", [first, second],
        )
        self.assertEqual(selected, [first, second])

    @staticmethod
    def approval_fixture_jobs() -> tuple[str, list[dict]]:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "rag_eval_realistic_office_100.json").read_text(encoding="utf-8"))
        document = next(item for item in fixture["documents"] if item["title"] == "采购审批流重构方案与灰度复盘")
        question = next(item["question"] for item in fixture["cases"] if item["id"] == "exact-001")
        note = SimpleNamespace(id=4)
        jobs = [{
            "note": note, "source_id": 4, "source_group": "note-4", "kind": "content",
            "title": document["title"], "raw_text": text, "excerpt": text,
            "hits": ["18", "万元", "审批", "采购", "金额"], "score": 0.7087,
            "rank_score": 73.57, "lexical_score": 20.0, "reason": "正文命中",
            "location": f"正文 {index}",
        } for index, text in enumerate(split_note_text(document["content"]), 1)]
        return question, jobs

    def test_amount_alignment_anchor_recovers_real_two_segment_note(self) -> None:
        question, jobs = self.approval_fixture_jobs()
        self.assertEqual(len(jobs), 2)
        source = object.__new__(RagPipeline).source_payloads(question, jobs)[0]
        self.assertIn("20 万元（不含）", source["evidence"])
        self.assertEqual(source["fallback_excerpt"], jobs[0]["raw_text"])
        self.assertEqual(source["evidence_locations"], ["正文 1", "正文 2"])
        answer, selected = RagPipeline.enforce_answer_contract(
            question, "采购金额18万元需部门负责人、预算负责人及采购负责人审批 [1]。", [source],
        )
        self.assertIn("采购负责人", answer)
        self.assertIn("20 万元（不含）", selected[0]["evidence"])
        self.assertNotIn("5 万元以下", selected[0]["evidence"])
        self.assertEqual(selected[0]["location"], "正文 1")
        self.assertEqual(selected[0]["evidence_locations"], ["正文 1"])
        self.assertEqual(selected[0]["url"], "/api/notes/4")
        self.assertEqual(selected[0]["fallback_excerpt"], source["fallback_excerpt"])
        self.assertEqual(source["evidence_locations"], ["正文 1", "正文 2"])
        self.assertEqual(source["location"], "正文 1；正文 2")

    def test_amount_alignment_anchor_is_absent_when_first_job_has_no_evidence(self) -> None:
        question, jobs = self.approval_fixture_jobs()
        pipeline = object.__new__(RagPipeline)
        middle = "5 万元（含）至 20 万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        empty = {**jobs[1], "raw_text": "这里只列审批目录。", "excerpt": "这里只列审批目录。"}
        with patch.object(pipeline, "_source_evidence_priority", return_value=0), patch.object(
            pipeline, "evidence_sentence", side_effect=["", middle],
        ):
            source = pipeline.source_payloads(question, [empty, jobs[0]])[0]
        self.assertEqual(source["evidence"], middle)
        self.assertEqual(source["fallback_excerpt"], empty["raw_text"])
        self.assertNotIn("evidence_alignment_anchor", source)
        self.assertIsNone(RagPipeline._amount_alignment_anchor_source(source))
        self.assertEqual(RagPipeline._alignment_amount_hint("金额18万元需审批", question, source), "")
        # Even equal strings cannot merge two distinct actual jobs: fallback
        # was first accepted from the empty-evidence job at another location.
        same_text_empty = {**jobs[0], "location": "正文 3"}
        with patch.object(pipeline, "_source_evidence_priority", return_value=0), patch.object(
            pipeline, "evidence_sentence", side_effect=["", middle],
        ):
            source = pipeline.source_payloads(question, [same_text_empty, jobs[0]])[0]
        self.assertNotIn("evidence_alignment_anchor", source)

    def test_amount_alignment_anchor_rejects_duplicate_text_at_distinct_locations(self) -> None:
        question, jobs = self.approval_fixture_jobs()
        pipeline = object.__new__(RagPipeline)
        duplicate = {**jobs[0], "location": "正文 3"}
        source = pipeline.source_payloads(question, [jobs[0], duplicate])[0]
        self.assertNotIn("evidence_alignment_anchor", source)
        self.assertEqual(RagPipeline._alignment_amount_hint("金额18万元需审批", question, source), "")
        repeated_job = pipeline.source_payloads(question, [jobs[0], dict(jobs[0])])[0]
        self.assertEqual(repeated_job["evidence_alignment_anchor"]["location"], "正文 1")
        documents = [{**jobs[0], "source_kind": "document", "document_chunk_id": chunk_id,
                      "document_chunk_order": order, "location": "PDF 第 1 页"}
                     for order, chunk_id in enumerate((41, 42))]
        same_page = pipeline.source_payloads(question, documents)[0]
        self.assertNotIn("evidence_alignment_anchor", same_page)

    def test_amount_alignment_anchor_projects_document_chunk_url_and_location_together(self) -> None:
        question, jobs = self.approval_fixture_jobs()
        document_jobs = [{
            **job, "note": SimpleNamespace(id=9), "source_kind": "document", "source_group": "document-9",
            "document_chunk_id": 41 + index, "document_chunk_order": index,
            "location": f"PDF 第 {index + 3} 页",
        } for index, job in enumerate(jobs)]
        source = object.__new__(RagPipeline).source_payloads(question, list(reversed(document_jobs)))[0]
        self.assertEqual(set(source["chunk_ids"]), {41, 42})
        bad_anchor = {**source["evidence_alignment_anchor"], "url": "/api/documents/9/file?chunk_id=42"}
        self.assertIsNone(RagPipeline._amount_alignment_anchor_source({**source, "evidence_alignment_anchor": bad_anchor}))
        _answer, selected = RagPipeline.enforce_answer_contract(
            question, "采购金额18万元需部门负责人、预算负责人及采购负责人审批 [1]。", [source],
        )
        self.assertIn("20 万元（不含）", selected[0]["evidence"])
        self.assertEqual(selected[0]["url"], "/api/documents/9/file?chunk_id=41")
        self.assertEqual(selected[0]["location"], "PDF 第 3 页")
        self.assertEqual(selected[0]["evidence_locations"], ["PDF 第 3 页"])
        self.assertEqual((selected[0]["chunk_id"], selected[0]["chunk_order"]), (41, 0))
        self.assertEqual((selected[0]["chunk_ids"], selected[0]["chunk_orders"]), ([41], [0]))
        self.assertEqual(set(source["chunk_ids"]), {41, 42})

    def test_amount_alignment_anchor_rejects_stale_text_and_source_identity(self) -> None:
        question, jobs = self.approval_fixture_jobs()
        source = object.__new__(RagPipeline).source_payloads(question, jobs)[0]
        variants = [
            {"evidence": source["evidence"] + "补充"},
            {"fallback_excerpt": source["fallback_excerpt"] + "补充"},
            {"id": 99}, {"type": "document"},
        ]
        for changed in variants:
            with self.subTest(changed=changed):
                candidate = {**source, **changed}
                self.assertIsNone(RagPipeline._amount_alignment_anchor_source(candidate))
                selected = RagPipeline.align_sources_to_answer("金额18万元需采购负责人审批 [1]。", [candidate], question=question)
                self.assertEqual(selected[0]["location"], source["location"])
                self.assertEqual(selected[0]["url"], source["url"])

    def test_amount_alignment_anchor_binds_the_bounded_payload_context(self) -> None:
        question, jobs = self.approval_fixture_jobs()
        long_job = {**jobs[0], "raw_text": jobs[0]["raw_text"] + "背景资料。" * 400}
        source = object.__new__(RagPipeline).source_payloads(question, [long_job])[0]
        self.assertEqual(len(source["fallback_excerpt"]), 1800)
        self.assertIsNotNone(RagPipeline._amount_alignment_anchor_source(source))
        self.assertIsNone(RagPipeline._amount_alignment_anchor_source({**source, "fallback_excerpt": source["fallback_excerpt"][:-1]}))

    def test_amount_alignment_anchor_does_not_change_nonamount_or_multiple_claim_paths(self) -> None:
        question, jobs = self.approval_fixture_jobs()
        pipeline = object.__new__(RagPipeline)
        source = pipeline.source_payloads(question, jobs)[0]
        without_anchor = {key: value for key, value in source.items() if key != "evidence_alignment_anchor"}
        cases = [
            ("需要哪些审批？", "需要部门负责人和预算负责人审批 [1]。"),
            (question, "金额18万元需采购负责人审批 [1]。金额20万元需法务负责人会签 [1]。"),
        ]
        for current_question, answer in cases:
            with self.subTest(answer=answer):
                actual = RagPipeline.align_sources_to_answer(answer, [source], question=current_question)[0]
                prior = RagPipeline.align_sources_to_answer(answer, [without_anchor], question=current_question)[0]
                self.assertEqual({k: v for k, v in actual.items() if k != "evidence_alignment_anchor"}, prior)
        compound = "采购金额18万元需要哪些审批？紧急采购补齐比价依据的期限是什么？"
        self.assertEqual(len(pipeline.compound_query_clauses(compound)), 2)
        self.assertNotIn("evidence_alignment_anchor", pipeline.source_payloads(compound, jobs)[0])
        row = "合同编号：EQ-C31；采购金额：18万元；负责人：林岚；付款状态：待付款；付款比例：0%"
        other = "合同编号：EQ-C310；采购金额：5万元；负责人：周远；付款比例：80%"
        row_source = pipeline.source_payloads("合同 EQ-C31 的采购金额是多少？", [
            {**jobs[0], "raw_text": row, "excerpt": row},
            {**jobs[1], "raw_text": other, "excerpt": other},
        ])[0]
        self.assertIn("evidence_alignment_anchor", row_source)
        aligned = RagPipeline.align_sources_to_answer("EQ-C31的采购金额是18万元 [1]。", [row_source])[0]
        self.assertEqual(aligned, row_source)

    @classmethod
    def budget_pause_job(cls, text: str, *, title: str = "营销活动项目例会纪要（夏季会员周）", location: str = "正文 1", note_id: int = 7) -> dict:
        return {**cls.count_threshold_job(text, title=title, location=location, note_id=note_id), "hits": ["营销活动", "预算"]}

    @staticmethod
    def budget_pause_text(rule: str = "任何单渠道超预算 5% 必须暂停并向项目经理说明。") -> str:
        return "【会议日期】2026-06-18。\n【预算】优惠券成本上限38万元，媒体投放预算上限12万元。" + rule

    @staticmethod
    def budget_pause_query(assistant: str = "优惠券成本和媒体投放都有上限。"):
        return QueryResolver.resolve("超过一点点也要停吗？", [{"role": "user", "content": "营销活动预算怎么控制？"}, {"role": "assistant", "content": assistant}])

    def test_budget_pause_original_history_split_producer_and_contract(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "rag_eval_realistic_office_100.json").read_text(encoding="utf-8"))
        case = next(item for item in fixture["cases"] if item["id"] == "followup-006")
        doc = next(item for item in fixture["documents"] if item["title"] == case["expected_titles"][0])
        query = QueryResolver.resolve(case["question"], case["history"])
        jobs = [self.budget_pause_job(text, title=doc["title"], location=f"正文 {index}") for index, text in enumerate(split_note_text(doc["content"]), 1)]
        pipeline = object.__new__(RagPipeline)
        sources = pipeline.source_payloads(query.evidence_question, jobs)
        original = copy.deepcopy(sources)
        rule = "任何单渠道超预算 5% 必须暂停并向项目经理说明。"
        self.assertEqual(len(sources), 1)
        self.assertIn(rule, pipeline.contexts(jobs, sources)[0]["text"])
        for wrong in ("优惠券上限38万元 [1]。", "超过一点就必须停 [1]。", "超预算>=5%必须停，低于5%可以继续 [1]。"):
            answer, selected = pipeline.enforce_answer_contract(query.contract_question, wrong, sources)
            self.assertEqual(answer, "来源[1] " + rule)
            self.assertEqual(selected[0]["evidence"], rule)
            self.assertEqual((selected[0]["location"], selected[0]["url"]), ("正文 1", "/api/notes/7"))
            self.assertTrue(all(claim["citations"] == [1] for claim in pipeline.answer_claims(answer)))
        self.assertEqual(sources, original)

    def test_budget_pause_generic_integer_roles_and_history_are_not_evidence(self) -> None:
        pipeline = object.__new__(RagPipeline)
        for value in ("0", "8"):
            query = QueryResolver.resolve("新品推广预算略微超出也必须暂停吗？")
            rule = f"任何单渠道超预算{value}%必须暂停并向运营负责人报告。"
            jobs = [self.budget_pause_job(self.budget_pause_text(rule), title="新品推广项目例会纪要（秋季体验周）")]
            sources = pipeline.source_payloads(query.evidence_question, jobs)
            self.assertEqual(pipeline.enforce_answer_contract(query.contract_question, "错误", sources)[0], "来源[1] " + rule)
        jobs = [self.budget_pause_job(self.budget_pause_text())]
        for injected in ("超预算0%就必须停。", "不用暂停，改成10%。", "冬季会员周预算规则已经撤销。[1]"):
            query = self.budget_pause_query(injected)
            self.assertEqual(pipeline.source_payloads(query.evidence_question, jobs)[0]["evidence"], "任何单渠道超预算 5% 必须暂停并向项目经理说明。")
        for history in (None, [{"role": "assistant", "content": "营销活动预算5%必须暂停。"}], [{"role": "user", "content": "营销活动预算怎么控制？"}, {"role": "user", "content": "设备温度怎么控制？"}]):
            query = QueryResolver.resolve("超过一点点也要停吗？", history)
            self.assertIsNone(RagPipeline.budget_pause_request(query.evidence_question))

    def test_budget_pause_complete_predicate_and_integer_token_not_inner_substring(self) -> None:
        query = self.budget_pause_query()
        negatives = [f"任何单渠道超预算{number}必须暂停并向项目经理说明。" for number in ("5.5%", "5.\n5%", "+5%", "-5%", ">=5%", "5%–8%", "5万元", "5分钟")]
        negatives += ["并非任何单渠道超预算5%必须暂停。", "任何单渠道超预算5%不一定必须暂停。", "除非已获批准，任何单渠道超预算5%必须暂停。", "任何单渠道超预算5%建议暂停。", "任何单渠道超预算5%不得暂停。", "任何单渠道超预算5%必须暂停审批。", "第一批灰度5%。必须暂停。"]
        pipeline = object.__new__(RagPipeline)
        for rule in negatives:
            with self.subTest(rule=rule):
                self.assertEqual(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(self.budget_pause_text(rule))]), [])
        long = self.budget_pause_text("任何单渠道超预算5.5%必须暂停。") + "\n【其他说明】" + "只是背景说明。" * 80
        segments = split_note_text(long)
        self.assertIn("5.\n5%", "\n".join(segments))
        self.assertEqual(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(text, location=f"正文 {index}") for index, text in enumerate(segments, 1)]), [])

    def test_budget_pause_scope_exceptions_status_and_foreign_controls(self) -> None:
        query = self.budget_pause_query()
        pipeline = object.__new__(RagPipeline)
        for suffix in ("短信渠道除外。", "\n【补充要求】上述规则已撤回。", "\n【适用范围】只适用于已准入渠道。", "\n【补充预算规则】任何单渠道超预算8%必须暂停。", "\n【争议记录】本预算规则已否决。"):
            self.assertEqual(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(self.budget_pause_text() + suffix)]), [])
        for suffix in ("\n【争议记录】未经授权短信触达方案被否决。", "\n【长期改进】8月1日前计划进行演练。", "\n【新品推广预算规则】该预算规则已撤回。", "\n【渠道安排】第一批灰度10%，观察24小时。"):
            self.assertEqual(len(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(self.budget_pause_text() + suffix)])), 1)
        for title in ("营销活动备份项目例会纪要（夏季会员周）", "营销活动项目例会纪要（夏季会员周草案）", "旧版营销活动项目例会纪要（夏季会员周）"):
            self.assertEqual(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(self.budget_pause_text(), title=title)]), [])
        for rule in ("活动名称为冬季会员周。任何单渠道超预算5%必须暂停。", "冬季会员周预算规则：任何单渠道超预算5%必须暂停。"):
            self.assertEqual(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(self.budget_pause_text(rule))]), [])

    def test_budget_pause_meeting_identity_and_unique_actual_origin(self) -> None:
        query = self.budget_pause_query()
        pipeline = object.__new__(RagPipeline)
        first = self.budget_pause_job(self.budget_pause_text())
        for text in (self.budget_pause_text().replace("2026-06-18", "2026-02-30"), self.budget_pause_text().replace("【会议日期】2026-06-18。", ""), self.budget_pause_text() + "\n【会议日期】2026-06-19。"):
            self.assertEqual(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(text)]), [])
        for second in (self.budget_pause_job(self.budget_pause_text(), location="正文 2"), self.budget_pause_job(self.budget_pause_text(), title="营销活动项目例会纪要（冬季会员周）"), self.budget_pause_job("该预算规则已撤回。", location="正文 2")):
            self.assertEqual(pipeline.source_payloads(query.evidence_question, [first, second]), [])
        self.assertEqual(len(pipeline.source_payloads(query.evidence_question, [first, first])), 1)
        self.assertEqual(len(pipeline.source_payloads(query.evidence_question, [first, self.budget_pause_job("该预算规则已撤回。", note_id=8)])), 1)

    def test_budget_pause_named_rule_status_in_neutral_column_and_later_job(self) -> None:
        query = self.budget_pause_query()
        pipeline = object.__new__(RagPipeline)
        base = self.budget_pause_text() + "\n【争议记录】未经授权的触达提议被否决。"
        for status in ("拟于下月生效", "尚待批准", "已废止", "已撤回"):
            text = base + "\n【补充说明】该预算暂停规则" + status + "。"
            self.assertEqual(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(text)]), [])
        first = self.budget_pause_job(base)
        later = self.budget_pause_job("预算暂停规则已撤回。", location="正文 2")
        self.assertEqual(pipeline.source_payloads(query.evidence_question, [first, later]), [])
        foreign = base + "\n【新品推广预算暂停规则】该预算暂停规则已撤回。"
        self.assertEqual(len(pipeline.source_payloads(query.evidence_question, [self.budget_pause_job(foreign)])), 1)
        self.assertEqual(len(pipeline.source_payloads(query.evidence_question, [first, {**later, "note": SimpleNamespace(id=8), "source_group": "note-8", "source_id": 8}])), 1)

    def test_budget_pause_transport_visible_context_and_mixed_table_denial(self) -> None:
        query = self.budget_pause_query()
        pipeline = object.__new__(RagPipeline)
        document = {**self.budget_pause_job(self.budget_pause_text(), location="PDF 第 3 页"), "source_kind": "document", "source_group": "document-7", "document_chunk_id": 21, "document_chunk_order": 2}
        source = pipeline.source_payloads(query.evidence_question, [document])[0]
        self.assertEqual((source["location"], source["url"], source["chunk_id"]), ("PDF 第 3 页", "/api/documents/7/file?chunk_id=21", 21))
        for key, value in (("evidence", "必须暂停。"), ("fallback_excerpt", self.budget_pause_text() + "该规则已撤回。"), ("title", "营销活动项目例会纪要（冬季会员周）"), ("url", "/api/notes/7"), ("chunk_id", 22), ("location", "PDF 第 4 页"), ("evidence_alignment_anchor", {})):
            altered = {**copy.deepcopy(source), key: value}
            self.assertEqual(pipeline.enforce_answer_contract(query.contract_question, "错误 [1]。", [altered])[1], [])
        table = self.budget_pause_job("合同编号：EQ-C31；付款比例：5%；负责人：林岚", note_id=8)
        declined = [self.budget_pause_job(self.budget_pause_text() + "短信渠道除外。"), table]
        self.assertEqual(pipeline.source_payloads(query.evidence_question, declined), [])
        self.assertEqual(pipeline.contexts(declined, sources=[]), [])
        self.assertTrue(pipeline.contexts(declined, sources=None))
        self.assertEqual(pipeline.enforce_answer_contract(query.contract_question, "必须暂停。", [])[1], [])

    @staticmethod
    def retention_source(evidence: str, *, index: int = 1, fallback: str = "") -> dict:
        return {"index": index, "id": index, "type": "note", "title": "离岗资料处理",
                "evidence": evidence, "fallback_excerpt": fallback or evidence, "excerpt": evidence,
                "matched_terms": ["历史资料", "账号停用"], "score": 0.8, "rank_score": 30,
                "location": "正文 1", "url": f"/api/notes/{index}"}

    def test_retention_endpoint_original_fixture_recovers_visible_fact(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "rag_eval_realistic_office_100.json").read_text(encoding="utf-8"))
        case = next(item for item in fixture["cases"] if item["id"] == "paraphrase-011")
        doc = next(item for item in fixture["documents"] if item["title"] == case["expected_titles"][0])
        jobs = [{**self.count_threshold_job(text, title=doc["title"], location=f"正文 {index}"),
                 "hits": ["账号停用", "历史资料", "删除"]}
                for index, text in enumerate(split_note_text(doc["content"]), 1)]
        pipeline = object.__new__(RagPipeline)
        sources = pipeline.source_payloads(case["question"], jobs)
        before = copy.deepcopy(sources)
        answer, selected = pipeline.enforce_answer_contract(case["question"], "账号停用后历史资料不会立刻被删除，会保留直至权限交接完成 [1]。", sources)
        self.assertIn("不等于", answer)
        self.assertIn("所有历史资料立刻删除", answer)
        self.assertNotIn("权限交接完成", answer)
        self.assertIn("所有历史资料立刻删除", selected[0]["evidence"])
        self.assertEqual(selected[0]["url"], sources[0]["url"])
        self.assertEqual(sources, before)
        self.assertTrue(all(claim["citations"] for claim in pipeline.answer_claims(answer)))

    def test_retention_endpoint_complete_same_object_relation_is_supported(self) -> None:
        for material, action in (("历史资料", "保留"), ("审计记录", "保存"), ("客户数据", "保存"), ("项目文件", "保留")):
            evidence = f"{material}{action}直至权限交接完成。"
            source = self.retention_source(evidence)
            answer = evidence[:-1] + " [1]。"
            self.assertFalse(RagPipeline.answer_has_unsupported_retention_endpoint("历史资料如何处理？", answer, [source]))
        source = self.retention_source("账号停用不会立即删除历史资料。历史资料保留直至权限交接完成。")
        answer, selected = RagPipeline.enforce_answer_contract("人离职后账号一关，历史资料会马上被删吗？", "历史资料保留直至权限交接完成 [1]。", [source])
        self.assertIn("历史资料保留直至权限交接完成", answer)
        self.assertTrue(selected)

    def test_retention_endpoint_other_object_action_and_log_duration_do_not_prove_it(self) -> None:
        answer = "历史资料保留直至权限交接完成 [1]。"
        for evidence in ("访问记录保存12个月。", "备份文件保留直至权限交接完成。", "历史资料的移交工作持续直至权限交接完成。", "历史资料保存直至权限交接完成。"):
            self.assertTrue(RagPipeline.answer_has_unsupported_retention_endpoint("历史资料如何处理？", answer, [self.retention_source(evidence)]))

    def test_retention_endpoint_negated_evidence_cannot_supply_affirmative_relation(self) -> None:
        answer = "历史资料保留直至权限交接完成 [1]。"
        for evidence in ("并非历史资料保留直至权限交接完成。", "历史资料并非保留直至权限交接完成。", "不能将历史资料保留直至权限交接完成。", "历史资料保留直至权限交接完成并非事实。"):
            self.assertTrue(RagPipeline.answer_has_unsupported_retention_endpoint("历史资料如何处理？", answer, [self.retention_source(evidence)]))
        visible = "账号停用不等于历史资料立刻删除。"
        source = self.retention_source(visible, fallback="并非历史资料保留直至权限交接完成。")
        recovered, selected = RagPipeline.enforce_answer_contract("历史资料如何处理？", answer, [source])
        self.assertNotIn("权限交接完成", recovered)
        self.assertIn(visible, recovered)
        self.assertEqual(selected[0]["evidence"], visible)

    def test_retention_endpoint_requires_actual_citation_not_another_source(self) -> None:
        first = self.retention_source("账号停用不等于历史资料立刻删除。")
        second = self.retention_source("历史资料保留直至权限交接完成。", index=2)
        bad = "历史资料保留直至权限交接完成 [1]。"
        self.assertTrue(RagPipeline.answer_has_unsupported_retention_endpoint("历史资料如何处理？", bad, [first, second]))
        self.assertFalse(RagPipeline.answer_has_unsupported_retention_endpoint("历史资料如何处理？", bad.replace("[1]", "[2]"), [first, second]))
        answer, selected = RagPipeline.enforce_answer_contract("历史资料如何处理？", bad, [first, second])
        self.assertNotIn("权限交接完成", answer)
        self.assertEqual([source["id"] for source in selected], [1])

    def test_retention_endpoint_missing_visible_fact_refuses_and_other_relations_are_unchanged(self) -> None:
        bad = "历史资料保留直至权限交接完成 [1]。"
        for sources in ([], [self.retention_source("", fallback="权限交接完成后结束访问。")]):
            answer, selected = RagPipeline.enforce_answer_contract("历史资料如何处理？", bad, sources)
            self.assertEqual(selected, [])
            self.assertNotIn("权限交接完成", answer)
        for answer in ("值班持续直到交接完成 [1]。", "普通服务工单自关闭日起保存24个月 [1]。", "资料保存期限另按业务与合规规则执行 [1]。"):
            self.assertFalse(RagPipeline.answer_has_unsupported_retention_endpoint("如何处理？", answer, []))

    @classmethod
    def cause_pair_job(cls, text: str, *, title: str = "订单同步延迟 P1 故障复盘", location: str = "正文 1", note_id: int = 7) -> dict:
        return {**cls.count_threshold_job(text, title=title, location=location, note_id=note_id),
                "hits": ["消息队列", "数据库", "P1", "根因", "订单"]}

    @staticmethod
    def cause_pair_text(root: str = "促销峰值使消息队列积压；消费者并发设置不足。数据库不是根因。") -> str:
        return "【事件摘要】7 月 8 日 09:12 至 10:06，订单中心向仓储系统的同步延迟最高 47 分钟。\n【根因】" + root

    def test_cause_pair_original_split_recovers_complete_block_and_every_claim_citation(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "rag_eval_realistic_office_100.json").read_text(encoding="utf-8"))
        document = next(item for item in fixture["documents"] if item["title"] == "订单同步延迟 P1 故障复盘")
        question = next(item["question"] for item in fixture["cases"] if item["id"] == "distractor-003")
        segments = split_note_text(document["content"])
        self.assertEqual(len(segments), 1)
        sources = object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(segments[0])])
        self.assertEqual(len(sources), 1)
        evidence = "【根因】促销峰值使消息队列积压；消费者扩容策略仅依据 CPU 使用率，未纳入队列滞留时间。数据库不是根因，支付接口也未异常。"
        self.assertEqual(sources[0]["evidence"], evidence)
        before = copy.deepcopy(sources)
        for generated in ("数据库是根因 [1]。", "消息队列是根因 [1]。", "人工扩容导致数据库故障 [1]。"):
            answer, selected = RagPipeline.enforce_answer_contract(question, generated, sources)
            claims = RagPipeline.answer_claims(answer)
            self.assertEqual(len(claims), 3)
            self.assertTrue(all(claim["citations"] == [1] for claim in claims))
            plain = re.sub(r"来源\[1\]|\s+", "", answer)
            self.assertEqual(plain, re.sub(r"\s+", "", evidence))
            from tests.rag_eval.metrics import answer_metrics
            metrics = answer_metrics({"question": question, "expected_answer_terms": []}, {"answer": answer, "source_details": selected})
            self.assertEqual(metrics["claim_citation_coverage"], 1.0)
            self.assertEqual(selected[0]["evidence"], evidence)
            self.assertEqual(selected[0]["location"], "正文 1")
            self.assertEqual(selected[0]["url"], "/api/notes/7")
            self.assertEqual(RagPipeline.enforce_answer_contract(question, answer, selected), (answer, selected))
        self.assertEqual(sources, before)

    def test_cause_pair_generalises_subject_grade_options_and_question_order(self) -> None:
        text = "【事件摘要】8 月 6 日 13:00 至 13:45，库存中心向履约系统的同步延迟最高 18 分钟。\n【根因】批量导入使任务队列积压；消费者并发设置不足。缓存服务不是根因。\n【处置】13:20 扩容消费者。\n【长期改进】9 月 1 日前新增积压告警，计划进行演练。"
        for options in ("缓存服务还是任务队列", "任务队列还是缓存服务"):
            question = "库存 P2 的根因是" + options + "？"
            sources = object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(text, title="库存同步延迟 P2 故障复盘")])
            self.assertEqual(len(sources), 1)
            answer, selected = RagPipeline.enforce_answer_contract(question, "缓存服务是根因 [1]。", sources)
            self.assertIn("批量导入使任务队列积压", answer)
            self.assertIn("缓存服务不是根因", answer)
            self.assertEqual(selected[0]["evidence"], text.split("\n")[1])

    def test_cause_pair_requires_mechanism_explicit_exclusion_and_consistent_direction(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        negatives = (
            "数据库不是根因。", "消息队列积压，数据库未异常。",
            "消息队列积压，尚未排除数据库。", "消息队列积压，不能认定数据库不是根因。",
            "消息队列积压。并非数据库不是根因。",
            "消息队列积压。数据库正常运行，支付接口不是根因。",
            "数据库故障导致消息队列积压。数据库不是根因。",
            "消息队列是根因。数据库是根因。", "消息队列不是根因。数据库不是根因。",
            "消息队列未发生积压。数据库不是根因。", "可能是消息队列积压。数据库不是根因。",
            "消息队列积压。数据库不是根因。该结论已撤回。",
        )
        for root in negatives:
            with self.subTest(root=root):
                sources = object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(self.cause_pair_text(root))])
                self.assertEqual(sources, [])
                self.assertEqual(RagPipeline.enforce_answer_contract(question, "数据库是根因。", sources), ("现有笔记没有足够信息。", []))
        source = object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(self.cause_pair_text("消息队列积压导致数据库超时。数据库不是根因。"))])
        self.assertEqual(len(source), 1)
        self.assertIn("消息队列积压导致数据库超时", RagPipeline.enforce_answer_contract(question, "错误", source)[0])

    def test_cause_pair_requires_complete_title_and_summary_owner_not_receiver(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        text = self.cause_pair_text()
        for title in ("订单备份同步延迟 P1 故障复盘", "订单支付接口中断 P1 故障复盘", "订单同步延迟 P1 与 P2 故障复盘", "历史订单同步延迟 P1 故障复盘", "订单同步延迟 P2 故障复盘"):
            with self.subTest(title=title):
                self.assertEqual(object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(text, title=title)]), [])
        for changed in (text.replace("订单中心", "支付中心"), text.replace("订单中心向仓储系统", "仓储中心向订单系统"), text.replace("10:06", "08:06"), text.replace("7 月 8 日", "7 月 32 日")):
            self.assertEqual(object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(changed)]), [])

    def test_cause_pair_candidate_polarity_and_symptom_qualifications_share_event_scope(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        pipeline = object.__new__(RagPipeline)
        contradictions = ("消息队列也不是根因。", "数据库是根因。", "数据库才是真正根因。",
                          "消息队列积压不属于根因。", "消息队列积压只是症状而非根因。",
                          "消息队列积压仅仅是表象。", "消息队列积压，只是症状而非根因。",
                          "数据库不是唯一根因。", "根因是数据库。")
        for contradiction in contradictions:
            for heading in ("", "\n【补充结论】", "\n【补充订单P1根因】"):
                with self.subTest(contradiction=contradiction, heading=heading):
                    jobs = [self.cause_pair_job(self.cause_pair_text() + heading + contradiction)]
                    self.assertEqual(pipeline.source_payloads(question, jobs), [])
                    self.assertEqual(pipeline.contexts(jobs, sources=[]), [])
                    self.assertEqual(RagPipeline.enforce_answer_contract(question, "消息队列是根因 [1]。", [])[1], [])
            # An explicit other-event scope cannot change this event's roles.
            other_event = self.cause_pair_text() + "\n【支付P1根因】" + contradiction
            self.assertEqual(len(pipeline.source_payloads(question, [self.cause_pair_job(other_event)])), 1)
        for supplement in ("数据库并非根因。", "消息队列确实是根因。", "根因是消息队列。"):
            self.assertEqual(len(pipeline.source_payloads(question, [self.cause_pair_job(self.cause_pair_text() + "\n【补充结论】" + supplement)])), 1)
        for contradiction in contradictions:
            second = self.cause_pair_job(contradiction, location="正文 2")
            self.assertEqual(pipeline.source_payloads(question, [self.cause_pair_job(self.cause_pair_text()), second]), [])
        for qualifier in ("主要", "唯一", "直接"):
            text = self.cause_pair_text(f"消息队列积压。数据库不是{qualifier}根因。")
            self.assertEqual(pipeline.source_payloads(question, [self.cause_pair_job(text)]), [])
        # Full object/predicate binding supplies positive exclusion, whereas
        # ambiguous wider scans can only refuse and never supply that fact.
        for root in ("消息队列积压。数据库也不是根因。",
                     "消息队列积压。数据库不是根因，支付接口也未异常。",
                     "消息队列积压。数据库不是根因。数据库正常运行，支付接口不是根因。"):
            self.assertEqual(len(pipeline.source_payloads(question, [self.cause_pair_job(self.cause_pair_text(root))])), 1)
        for extra in ("并非数据库不是根因。", "无法证明数据库不是根因。"):
            self.assertEqual(pipeline.source_payloads(question, [self.cause_pair_job(self.cause_pair_text() + extra)]), [])

    def test_cause_pair_explicit_occurrence_date_in_both_orders_matches_incident(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        pipeline = object.__new__(RagPipeline)
        templates = ("本次事故发生于7月{day}日。", "此次故障的发生日期为7月{day}日。",
                     "7月{day}日发生的本次事故。", "7月{day}日出现的该次事件。")
        for template in templates:
            for day in (8, 9):
                with self.subTest(template=template, day=day):
                    text = self.cause_pair_text() + "\n【补充结论】" + template.format(day=day)
                    self.assertEqual(bool(pipeline.source_payloads(question, [self.cause_pair_job(text)])), day == 8)
            foreign = self.cause_pair_text() + "\n【其他支付P1根因】" + template.format(day=9)
            self.assertEqual(len(pipeline.source_payloads(question, [self.cause_pair_job(foreign)])), 1)
        for heading in ("处置", "长期改进", "经验"):
            text = self.cause_pair_text() + f"\n【{heading}】本次事故发生于7月9日。"
            self.assertEqual(pipeline.source_payloads(question, [self.cause_pair_job(text)]), [])
        actions = self.cause_pair_text() + "\n【长期改进】8月1日前完成事故告警改进，计划于8月2日演练。"
        self.assertEqual(len(pipeline.source_payloads(question, [self.cause_pair_job(actions)])), 1)

    def test_cause_pair_local_scope_status_and_future_actions_are_distinct(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        text = self.cause_pair_text()
        for suffix in ("\n【补充结论】该根因结论已撤回。", "\n【补充结论】8月9日另一订单P1事故的根因是数据库。", "\n【补充结论】支付P1事故的根因是消息队列。",
                       "\n【补充订单P1根因】该根因结论已撤回。", "\n【初步结论】消息队列积压，数据库不是根因。",
                       "\n【处置】该根因结论已撤回。", "\n【长期改进】该根因结论已撤回。", "\n【经验】该根因结论已撤回。"):
            self.assertEqual(object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(text + suffix)]), [])
        for suffix in ("\n【长期改进】8月1日前新增告警，拟下月演练。", "\n【处置】09:41人工扩容。", "\n【其他支付P1根因】该根因结论已撤回。"):
            self.assertEqual(len(object.__new__(RagPipeline).source_payloads(question, [self.cause_pair_job(text + suffix)])), 1)

    def test_cause_pair_single_actual_job_and_same_source_counterevidence(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        pipeline = object.__new__(RagPipeline)
        first = self.cause_pair_job(self.cause_pair_text())
        for other in (self.cause_pair_job(self.cause_pair_text(), location="正文 2"),
                      self.cause_pair_job(self.cause_pair_text().replace("7 月 8 日", "7 月 9 日"), location="正文 2"),
                      self.cause_pair_job("该根因结论已撤回。", location="正文 2")):
            self.assertEqual(pipeline.source_payloads(question, [first, other]), [])
        for other in (self.cause_pair_job("该根因结论已撤回。", note_id=8),
                      self.cause_pair_job("该根因结论已撤回。", title="支付同步延迟 P1 故障复盘", location="正文 2")):
            self.assertEqual(len(pipeline.source_payloads(question, [first, other])), 1)
        self.assertEqual(len(pipeline.source_payloads(question, [first, first])), 1)
        only_mechanism = self.cause_pair_job(self.cause_pair_text("消息队列积压。"))
        only_exclusion = self.cause_pair_job(self.cause_pair_text("数据库不是根因。"), location="正文 2")
        self.assertEqual(pipeline.source_payloads(question, [only_mechanism, only_exclusion]), [])

    def test_cause_pair_document_origin_and_final_visible_context_hash_guards(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        pipeline = object.__new__(RagPipeline)
        job = {**self.cause_pair_job(self.cause_pair_text(), location="PDF 第 3 页"), "source_kind": "document", "source_group": "document-7", "document_chunk_id": 21, "document_chunk_order": 2}
        sources = pipeline.source_payloads(question, [self.cause_pair_job("背景说明。"), job])
        self.assertEqual(len(sources), 1)
        selected = RagPipeline.enforce_answer_contract(question, "错误", sources)[1][0]
        self.assertEqual((selected["url"], selected["chunk_id"], selected["chunk_order"], selected["location"]), ("/api/documents/7/file?chunk_id=21", 21, 2, "PDF 第 3 页"))
        for key, value in (("evidence", "【根因】消息队列积压。"), ("fallback_excerpt", self.cause_pair_text() + "该结论已撤回。"),
                           ("title", "支付同步延迟 P1 故障复盘"), ("id", 8), ("url", "/api/documents/7/file?chunk_id=22"),
                           ("location", "PDF 第 4 页"), ("chunk_id", 22), ("evidence_alignment_anchor", {})):
            altered = copy.deepcopy(sources)
            altered[0][key] = value
            self.assertEqual(RagPipeline.enforce_answer_contract(question, "错误", altered)[1], [])
        altered = copy.deepcopy(sources)
        altered[0]["evidence"] = "【根因】消息队列积压。"
        altered[0]["evidence_alignment_anchor"]["evidence_sha256"] = hashlib.sha256(altered[0]["evidence"].encode()).hexdigest()
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "错误", altered)[1], [])
        self.assertEqual(pipeline.source_payloads(question, [self.cause_pair_job(self.cause_pair_text() + "\n【长期改进】" + "记录" * 1000)]), [])

    def test_cause_pair_mixed_table_does_not_bypass_denial_and_other_questions_stay_out(self) -> None:
        question = "订单 P1 的根因是数据库还是消息队列？"
        pipeline = object.__new__(RagPipeline)
        table = self.cause_pair_job("事件编号：EQ-C31；负责人：林岚；根因：数据库", note_id=8)
        sources = pipeline.source_payloads(question, [self.cause_pair_job(self.cause_pair_text()), table])
        self.assertEqual(len(sources), 1)
        jobs = [self.cause_pair_job(self.cause_pair_text() + "该结论已撤回。"), table]
        self.assertEqual(pipeline.source_payloads(question, jobs), [])
        self.assertEqual(pipeline.contexts(jobs, sources=[]), [])
        self.assertTrue(pipeline.contexts(jobs, sources=None))
        for other in ("订单同步 P1 为什么被定为 P1，不是因为哪个现象？", "这次订单延迟是支付接口拖慢的吗？", "订单系统的队列积压告警被调整到多少条？", "订单 P1 故障如何处置？", "合同 EQ-C31 的根因是什么？", "采购18万元谁审批？"):
            self.assertIsNone(RagPipeline.cause_pair_request(other))

    @classmethod
    def inverse_approval_job(cls, text: str, *, title: str = "采购审批规则", location: str = "正文 1", note_id: int = 7) -> dict:
        return {**cls.count_threshold_job(text, title=title, location=location, note_id=note_id),
                "hits": ["采购负责人", "金额", "采购", "审批"]}

    def test_inverse_approval_original_fixture_full_split_chain_recovers_low_band_and_visible_pair(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "rag_eval_realistic_office_100.json").read_text(encoding="utf-8"))
        document = next(item for item in fixture["documents"] if item["title"] == "采购审批流重构方案与灰度复盘")
        question = next(item["question"] for item in fixture["cases"] if item["id"] == "paraphrase-001")
        segments = split_note_text(document["content"])
        self.assertEqual(len(segments), 2)
        self.assertIn("4.\n6", segments[0])
        pipeline = object.__new__(RagPipeline)
        sources = pipeline.source_payloads(question, [self.inverse_approval_job(text, title=document["title"], location=f"正文 {index}") for index, text in enumerate(segments, 1)])
        target = "【新审批规则】单笔含税金额 5 万元以下，由部门负责人和预算负责人审批；"
        witness = "5 万元（含）至 20 万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["evidence"], target + "\n" + witness)
        before = copy.deepcopy(sources)
        for generated in (witness + " [1]", "5万元以内无需任何审批 [1]。", target + " [1]"):
            answer, selected = RagPipeline.enforce_answer_contract(question, generated, sources)
            self.assertEqual(answer, "来源[1] " + target)
            self.assertEqual(selected[0]["evidence"], target + "\n" + witness)
            self.assertEqual(selected[0]["location"], "正文 1")
            self.assertEqual(selected[0]["url"], "/api/notes/7")
            self.assertNotIn("采购负责人", answer)
            self.assertEqual(RagPipeline.enforce_answer_contract(question, answer, selected), (answer, selected))
        self.assertEqual(sources, before)

    def test_inverse_approval_uses_generic_integer_boundary_business_and_role(self) -> None:
        question = "设备采购什么金额以下无需财务负责人审批？"
        for boundary, included, high_included in (("7", "", "含"), ("1000000", "", "含"), ("7", "（含）", "不含")):
            with self.subTest(boundary=boundary, included=included):
                target = f"单笔金额{boundary}万元{included}以下，由设备主管和预算主管审批；"
                witness = f"{boundary}万元（{high_included}）至2000000万元（不含），由设备主管、预算主管及财务负责人审批；"
                source = object.__new__(RagPipeline).source_payloads(question, [self.inverse_approval_job(target + witness, title="设备采购审批规则")])
                answer, selected = RagPipeline.enforce_answer_contract(question, "错误 [1]", source)
                self.assertEqual(answer, "来源[1] " + target)
                self.assertEqual(selected[0]["evidence"], target + "\n" + witness)

    def test_inverse_approval_requires_closed_rosters_and_full_same_condition(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        target = "单笔金额5万元以下，由部门负责人和预算负责人审批；"
        witness = "5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        pipeline = object.__new__(RagPipeline)
        invalid = [
            target.replace("预算负责人", "预算负责人等") + witness,
            target.replace("由", "至少由") + witness,
            target.replace("由", "可由") + witness,
            target.replace("和", "或") + witness,
            target.replace("审批", "初审") + witness,
            target.replace("预算负责人", "采购负责人") + witness,
            target.replace("预算负责人", "采购负责人的代理人") + witness,
            target + witness.replace("采购负责人", "采购负责人助理"),
            target + "仍须采购负责人复核。" + witness,
            target + "采购负责人已在立项阶段审批，不需重复审批。" + witness,
            "所有采购均须采购负责人会签。" + target + witness,
            "仅已准入供应商适用以下规则。" + target + witness,
            target + witness + "低档仍须采购负责人授权。",
            target,
        ]
        for text in invalid:
            with self.subTest(text=text):
                self.assertEqual(pipeline.source_payloads(question, [self.inverse_approval_job(text)]), [])
                self.assertEqual(RagPipeline.enforce_answer_contract(question, "5万元以内不找采购负责人", [])[1], [])

    def test_inverse_approval_scope_and_status_cannot_be_borrowed(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        target = "单笔金额5万元以下，由部门负责人和预算负责人审批；"
        witness = "5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        pipeline = object.__new__(RagPipeline)
        for text, title in [
            (target + witness, "采购备份审批规则"),
            (target.replace("单笔", "报销单笔") + witness, "采购审批规则"),
            ("【报销审批规则】" + target + witness, "采购审批规则"),
            ("【新报销审批规则】" + target + witness, "采购审批规则"),
            ("【当前报销审批规则】" + target + witness, "采购审批规则"),
            ("【现行报销审批规则】" + target + witness, "采购审批规则"),
            ("【补充报销审批规则】" + target + witness, "采购审批规则"),
            ("【其他报销审批规则】" + target + witness, "采购审批规则"),
            ("【旧版】" + target + "【新版】" + witness, "采购审批规则"),
            ("【拟议方案】" + target + witness, "采购审批规则"),
            ("本规则拟于下月生效。" + target + witness, "采购审批规则"),
            ("本方案尚待批准。" + target + witness, "采购审批规则"),
            (target + witness, "采购审批规则草案"),
            (target + witness + "该规则已撤回。", "采购审批规则"),
            (target + witness + "【规则状态】该规则已撤回。", "采购审批规则"),
            (target + witness, "采购审批旧版规则"),
            (target + witness + target.replace("5万元", "6万元") + witness.replace("5万元", "6万元"), "采购审批规则"),
        ]:
            with self.subTest(text=text, title=title):
                self.assertEqual(pipeline.source_payloads(question, [self.inverse_approval_job(text, title=title)]), [])
        for suffix in ("该规则未取消，继续生效。", "【报销审批规则】所有报销须采购负责人审批，该规则已取消。"):
            self.assertEqual(len(pipeline.source_payloads(question, [self.inverse_approval_job(target + witness + suffix)])), 1)
        for header in ("新审批规则", "新采购审批规则", "当前采购审批规则", "补充规则"):
            self.assertEqual(len(pipeline.source_payloads(question, [self.inverse_approval_job(f"【{header}】" + target + witness)])), 1)
        for separator in (" ", "\n", "\n\n"):
            text = "【采购审批规则】" + separator + target + witness
            sources = pipeline.source_payloads(question, [self.inverse_approval_job(text)])
            answer, selected = RagPipeline.enforce_answer_contract(question, "错误 [1]", sources)
            self.assertEqual(selected[0]["evidence"], "【采购审批规则】" + separator + target + "\n" + witness)
            self.assertIn(target, answer)

    def test_inverse_approval_integer_intervals_reject_overlap_gaps_and_unsupported_money(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        target = "单笔金额5万元以下，由部门负责人和预算负责人审批；"
        witness = "5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        invalid = [target.replace("以下", "以内") + witness,
                   target + witness.replace("（含）", "（不含）", 1),
                   target + witness.replace("5万元", "6万元"),
                   target + witness.replace("5万元", "50000元"),
                   target + witness.replace("20万元", "2万元")]
        for value in ("7.25", "7.\n25", "4.999999", "-5", "1e5", "1,000"):
            invalid.append((target + witness).replace("5万元", value + "万元"))
        invalid.append((target + witness).replace("万元", "万美元"))
        for text in invalid:
            with self.subTest(text=text):
                self.assertEqual(object.__new__(RagPipeline).source_payloads(question, [self.inverse_approval_job(text)]), [])

    def test_inverse_approval_neutral_headings_and_same_source_continue_rule_constraints(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        pair = "【采购审批规则】单笔金额5万元以下，由部门负责人和预算负责人审批；5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        pipeline = object.__new__(RagPipeline)
        for text in ("这是采购审批规则草案。" + pair,
                     pair + "【补充要求】低档仍须采购负责人复核。",
                     pair + "【适用范围】仅已准入供应商适用上述审批规则。",
                     "【适用范围】仅已准入供应商适用上述审批规则。" + pair,
                     pair + "【补充要求】上述规则已撤回。"):
            with self.subTest(text=text):
                self.assertEqual(pipeline.source_payloads(question, [self.inverse_approval_job(text)]), [])
        first = self.inverse_approval_job(pair)
        for text in ("采购审批规则已撤回。", "【补充要求】低档仍须采购负责人复核。",
                     "【适用范围】仅已准入供应商适用上述审批规则。"):
            with self.subTest(sibling=text):
                self.assertEqual(pipeline.source_payloads(question, [first, self.inverse_approval_job(text, location="正文 2")]), [])
        for suffix in ("【报销规则】所有报销须采购负责人复核。", "【维护说明】页面每周备份一次。"):
            self.assertEqual(len(pipeline.source_payloads(question, [self.inverse_approval_job(pair + suffix)])), 1)
        for other in (self.inverse_approval_job("报销审批规则已撤回。", location="正文 2"),
                      self.inverse_approval_job("该规则已撤回。", note_id=8)):
            self.assertEqual(len(pipeline.source_payloads(question, [first, other])), 1)

    def test_inverse_approval_actual_decimal_split_and_chunk_seam_never_accept_suffix(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        target = "单笔金额7.25万元以下，由部门负责人和预算负责人审批；"
        witness = "7.25万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        # Force the actual punctuation splitter, then separately choose a budget
        # whose decimal's first fragment ends a chunk. These are test variants.
        text = "【采购审批规则】" + target + witness + "背景说明。" * 90
        same_job = split_note_text(text)
        self.assertIn("7.\n25万元", same_job[0])
        seam_size = len("【采购审批规则】单笔金额7.")
        at_seam = split_note_text(text, target_size=seam_size, overlap=0)
        self.assertTrue(any(item.endswith("7.") for item in at_seam))
        self.assertTrue(any(item.startswith("25万元") for item in at_seam))
        for segments in (same_job, at_seam):
            jobs = [self.inverse_approval_job(item, location=f"正文 {index}") for index, item in enumerate(segments, 1)]
            self.assertEqual(object.__new__(RagPipeline).source_payloads(question, jobs), [])

    def test_inverse_approval_foreign_currency_scope_is_not_assumed_to_be_yuan(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        pair = "【采购审批规则】单笔金额5万元以下，由部门负责人和预算负责人审批；5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        pipeline = object.__new__(RagPipeline)
        for title, text in (("采购审批规则（美元计价）", pair), ("采购审批规则", "所有金额以美元计价。" + pair),
                            ("采购审批规则", pair + "【币种说明】金额单位采用USD。"),
                            ("采购审批规则（欧元）", pair)):
            with self.subTest(title=title, text=text):
                self.assertEqual(pipeline.source_payloads(question, [self.inverse_approval_job(text, title=title)]), [])
        for extra in ("所有金额以人民币计价。", "【报销规则】本栏以美元计价。"):
            self.assertEqual(len(pipeline.source_payloads(question, [self.inverse_approval_job(pair + extra)])), 1)

    def test_inverse_approval_unrelated_structured_job_cannot_bypass_proof(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        target = "单笔金额5万元以下，由部门负责人和预算负责人审批；"
        witness = "5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        pipeline = object.__new__(RagPipeline)
        row = self.inverse_approval_job("合同编号：EQ-C31；合同金额：5万元；负责人：采购负责人", note_id=8)
        prose = self.inverse_approval_job(target + witness)
        sources = pipeline.source_payloads(question, [prose, row])
        self.assertEqual(len(sources), 1)
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "来源[1] " + witness, sources)[0], "来源[1] " + target)
        declined = pipeline.source_payloads(question, [self.inverse_approval_job(target + witness + "该规则已撤回。"), row])
        self.assertEqual(declined, [])
        self.assertEqual(pipeline.contexts([prose, row], sources=declined), [])
        table_source = {"index": 2, "id": 8, "type": "note", "evidence": row["raw_text"], "fallback_excerpt": row["raw_text"]}
        # Even a caller supplying mixed sources directly cannot route this
        # recognised prose request into the generic structured contract.
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "来源[1] " + witness, [*sources, table_source])[1], [])

    def test_inverse_approval_actual_job_uniqueness_later_origin_and_document_metadata(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        target = "单笔金额5万元以下，由部门负责人和预算负责人审批；"
        witness = "5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        pipeline = object.__new__(RagPipeline)
        first = self.inverse_approval_job(target + witness)
        for jobs in ([self.inverse_approval_job(target), self.inverse_approval_job(witness, location="正文 2")],
                     [first, self.inverse_approval_job(target + witness, location="正文 2")],
                     [first, self.inverse_approval_job(target + witness, note_id=8)],
                     [first, self.inverse_approval_job("该规则已撤回。", location="正文 2")]):
            self.assertEqual(pipeline.source_payloads(question, jobs), [])
        later = {**self.inverse_approval_job(target + witness, location="PDF 第 3 页"),
                 "source_kind": "document", "document_chunk_id": 42, "document_chunk_order": 3}
        sources = pipeline.source_payloads(question, [self.inverse_approval_job("背景说明。"), later])
        answer, selected = RagPipeline.enforce_answer_contract(question, "错答 [1]", sources)
        self.assertEqual(answer, "来源[1] " + target)
        self.assertEqual(selected[0]["location"], "PDF 第 3 页")
        self.assertEqual(selected[0]["chunk_id"], 42)
        self.assertEqual(selected[0]["url"], "/api/documents/7/file?chunk_id=42")
        self.assertEqual(len(pipeline.source_payloads(question, [first, copy.deepcopy(first)])), 1)

    def test_inverse_approval_final_requires_visible_context_scope_and_origin_consistency(self) -> None:
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        target = "单笔金额5万元以下，由部门负责人和预算负责人审批；"
        witness = "5万元（含）至20万元（不含），由部门负责人、预算负责人及采购负责人审批；"
        pipeline = object.__new__(RagPipeline)
        source = pipeline.source_payloads(question, [self.inverse_approval_job(target + witness)])[0]
        changes = [{"evidence": target}, {"fallback_excerpt": target}, {"title": "报销审批规则"},
                   {"url": "/api/notes/8"}, {"id": 8}, {"location": "正文 2"},
                   {"evidence_locations": ["正文 1", "正文 2"]}, {"evidence_alignment_anchor": None}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(RagPipeline.enforce_answer_contract(question, "5万元以内 [1]", [{**source, **change}])[1], [])
        # Even internally consistent fingerprints cannot replace semantics.
        import hashlib
        invalid = copy.deepcopy(source)
        invalid["fallback_excerpt"] += "采购负责人仍须复核。"
        invalid["evidence_alignment_anchor"]["fallback_sha256"] = hashlib.sha256(invalid["fallback_excerpt"].encode()).hexdigest()
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "5万元以下 [1]", [invalid])[1], [])
        bad_url = copy.deepcopy(source)
        bad_url["url"] = bad_url["evidence_alignment_anchor"]["url"] = "/api/notes/999"
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "5万元以下 [1]", [bad_url])[1], [])
        oversized = target + witness + "中性说明。" * 400 + "该规则已撤回。"
        self.assertEqual(pipeline.source_payloads(question, [self.inverse_approval_job(oversized)]), [])

    def test_inverse_approval_scope_excludes_other_requests_and_preserves_denial_context(self) -> None:
        for question in ("18万元采购需要谁审批？", "20万元整需要法务会签吗？", "紧急采购补齐比价依据的期限是多久？",
                         "采购什么金额以下无需采购负责人和财务负责人审批？", "采购和报销什么金额以下无需采购负责人审批？",
                         "合同 EQ-C31 什么金额以下无需采购负责人审批？"):
            self.assertIsNone(RagPipeline.inverse_approval_request(question))
        question = "小额采购走到什么金额以内可以不找采购负责人？"
        job = self.inverse_approval_job("单笔金额5万元以下，由部门负责人和预算负责人审批；")
        pipeline = object.__new__(RagPipeline)
        sources = pipeline.source_payloads(question, [job])
        self.assertEqual(sources, [])
        self.assertEqual(pipeline.contexts([job], sources=sources), [])
        self.assertTrue(pipeline.contexts([job]))
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "错误的生成", []), ("现有笔记没有足够信息。", []))

    @staticmethod
    def count_threshold_job(text: str, *, title: str = "订单系统配置", location: str = "正文 1", note_id: int = 7) -> dict:
        return {
            "note": SimpleNamespace(id=note_id), "source_id": note_id, "source_group": f"note-{note_id}", "kind": "content",
            "title": title, "raw_text": text, "excerpt": text,
            "hits": ["队列积压", "系统的", "告警", "订单", "调整"], "score": 0.72,
            "rank_score": 72.0, "lexical_score": 20.0, "reason": "正文命中", "location": location,
        }

    def test_adjusted_count_original_fixture_preserves_complete_change_and_locator(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "rag_eval_realistic_office_100.json").read_text(encoding="utf-8"))
        document = next(item for item in fixture["documents"] if item["title"] == "订单同步延迟 P1 故障复盘")
        question = next(item["question"] for item in fixture["cases"] if item["id"] == "exact-022")
        pipeline = object.__new__(RagPipeline)
        # The real default document is one chunk. The second path deliberately
        # uses a smaller test budget, without changing the frozen fixture.
        for segments in (split_note_text(document["content"]), split_note_text(document["content"], target_size=220, overlap=0)):
            with self.subTest(chunks=len(segments)):
                self.assertIn(len(segments), (1, 2))
                jobs = [self.count_threshold_job(text, title=document["title"], location=f"正文 {index}") for index, text in enumerate(segments, 1)]
                sources = pipeline.source_payloads(question, jobs)
                self.assertEqual(len(sources), 1)
                self.assertEqual(sources[0]["evidence"], "09:41 将积压阈值告警从 20 万条调整为 5 万条；")
                original = copy.deepcopy(sources)
                for generated in ("订单同步延迟最高 47 分钟 [1]。", "队列积压告警调整为20万条 [1]。", "队列积压告警调整为5万条 [1]。"):
                    answer, selected = RagPipeline.enforce_answer_contract(question, generated, sources)
                    self.assertEqual(answer, "来源[1] " + sources[0]["evidence"])
                    self.assertEqual(selected[0]["evidence"], sources[0]["evidence"])
                    self.assertEqual(selected[0]["location"], "正文 1")
                    self.assertEqual(selected[0]["evidence_locations"], ["正文 1"])
                    self.assertEqual(selected[0]["url"], "/api/notes/7")
                    self.assertNotIn("47 分钟", answer)
                self.assertEqual(sources, original)

    def test_adjusted_count_generalizes_only_complete_same_parameter_count_relation(self) -> None:
        question = "库存系统的重试队列告警被改为多少条？"
        text = "库存系统将重试队列告警从900条改为600条。"
        source = object.__new__(RagPipeline).source_payloads(question, [self.count_threshold_job(text, title="库存系统配置")])[0]
        answer, selected = RagPipeline.enforce_answer_contract(question, "900条 [1]。", [source])
        self.assertIn(text, answer)
        self.assertEqual(selected[0]["evidence"], text)
        self.assertEqual(RagPipeline.adjusted_count_proof(question, "库存系统配置", text)["new"], "600条")
        for lead in ("将", "把"):
            explicit = lead + "库存系统的重试队列告警从900条改为600条。"
            self.assertEqual(RagPipeline.adjusted_count_proof(question, "库存系统配置", explicit)["new"], "600条")

    def test_adjusted_count_does_not_borrow_subject_parameter_or_unit(self) -> None:
        question = "订单系统的队列积压告警被调整到多少条？"
        target = "将积压阈值告警从20万条调整为5万条。"
        cases = [
            ("订单备份系统配置", target), ("支付系统配置", target),
            ("订单同步复盘", "支付系统" + target),
            ("订单系统配置", "将支付系统的积压阈值告警从20万条调整为5万条。"),
            ("订单系统配置", "09:40 将支付系统的参数保存。09:41 " + target),
            ("订单同步复盘", "支付系统发生故障。" + target),
            ("订单同步复盘", "09:40 支付系统发生故障。09:41 " + target),
            ("订单同步复盘", "【支付系统】" + target),
            ("订单同步复盘", "将死信队列积压告警从20万条调整为5万条。"),
            ("订单同步复盘", "将退款队列积压告警从20万条调整为5万条。"),
            ("订单同步复盘", "将失败重试条数从20万条调整为5万条。"),
            ("订单同步复盘", "将积压阈值告警从20分钟调整为5分钟。"),
            ("订单同步复盘", "将积压阈值告警从20万元调整为5万元。"),
            ("订单同步复盘", "将积压阈值告警从20万条调整为5万条每秒。"),
            ("订单同步复盘", "积压阈值告警原来为20万条。重试条数调整为5万条。"),
            ("订单同步复盘", "将积压阈值告警从20万条；重试告警调整为5万条。"),
        ]
        pipeline = object.__new__(RagPipeline)
        for title, text in cases:
            with self.subTest(title=title, text=text):
                self.assertEqual(pipeline.source_payloads(question, [self.count_threshold_job(text, title=title)]), [])
        self.assertIsNone(RagPipeline.adjusted_count_proof("支付系统的队列积压告警被调整到多少条？", "支付宝系统配置", target))
        self.assertIsNone(RagPipeline.adjusted_count_proof("支付系统的队列积压告警被调整到多少条？", "支付宝运维复盘", target))

    def test_adjusted_count_status_is_local_to_the_change(self) -> None:
        question = "订单系统的队列积压告警被调整到多少条？"
        relation = "将积压阈值告警从20万条调整为5万条"
        blocked = [
            "拟" + relation + "。", "计划" + relation + "。",
            "【拟议方案】" + relation + "。", "【计划】\n" + relation + "。",
            relation + "，尚未生效。", relation + "的方案已被否决。",
            relation + "。该调整已撤回。", relation + "。此方案取消。",
            relation + "。本次变更已回滚。",
            relation + "。处置已完成。该调整已撤回。",
            relation + "。配置参数已保存。该调整已撤回。",
            relation + "。变更方案已归档。该调整已撤回。",
            relation + "。该调整已撤销。", relation + "。该调整未获批准。",
            relation + "。处置已完成。积压阈值告警调整已撤回。",
            "订单系统" + relation + "。订单系统的积压阈值告警调整已撤回。",
        ]
        pipeline = object.__new__(RagPipeline)
        for text in blocked:
            with self.subTest(text=text):
                self.assertEqual(pipeline.source_payloads(question, [self.count_threshold_job(text)]), [])
        active = relation + "。将重试条数从900条调整为600条。该方案已取消。"
        source = pipeline.source_payloads(question, [self.count_threshold_job(active)])[0]
        answer, selected = RagPipeline.enforce_answer_contract(question, "47分钟 [1]。", [source])
        self.assertIn(relation, answer)
        self.assertEqual(selected[0]["evidence"], relation + "。")
        for suffix in ("该调整未取消，已生效。", "该调整未撤回。", "重试告警的方案取消。该方案已撤销。"):
            with self.subTest(suffix=suffix):
                self.assertEqual(pipeline.source_payloads(question, [self.count_threshold_job(relation + "。" + suffix)])[0]["evidence"], relation + "。")

    def test_adjusted_count_conflicting_versions_jobs_and_sources_are_not_selected(self) -> None:
        question = "订单系统的队列积压告警被调整到多少条？"
        relation = "将积压阈值告警从20万条调整为5万条。"
        changed = relation.replace("5万条", "8万条")
        pipeline = object.__new__(RagPipeline)
        first = self.count_threshold_job(relation)
        cases = [
            [self.count_threshold_job(relation + changed)],
            [first, self.count_threshold_job(changed, location="正文 2")],
            [first, self.count_threshold_job(changed, note_id=8)],
            [first, self.count_threshold_job(relation, location="正文 2")],
        ]
        for jobs in cases:
            with self.subTest(jobs=jobs):
                self.assertEqual(pipeline.source_payloads(question, jobs), [])
        sources = pipeline.source_payloads(question, [first]) + pipeline.source_payloads(question, [self.count_threshold_job(changed, note_id=8)])
        sources[1]["index"] = 2
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "5万条 [1]。", sources), ("现有笔记没有足够信息。", []))
        self.assertEqual(len(pipeline.source_payloads(question, [first, dict(first)])), 1)

    def test_adjusted_count_retraction_in_another_job_cannot_be_discarded(self) -> None:
        question = "订单系统的队列积压告警被调整到多少条？"
        first = self.count_threshold_job("将积压阈值告警从20万条调整为5万条。")
        pipeline = object.__new__(RagPipeline)
        for text in ("处置已完成。该调整已撤回。", "订单系统的积压阈值告警调整已撤回。"):
            with self.subTest(text=text):
                self.assertEqual(pipeline.source_payloads(question, [first, self.count_threshold_job(text, location="正文 2")]), [])
        for text in ("支付系统的积压阈值告警调整已撤回。", "订单系统的重试告警调整已撤回。", "重试告警调整已撤回。该方案取消。"):
            with self.subTest(unrelated=text):
                self.assertEqual(len(pipeline.source_payloads(question, [first, self.count_threshold_job(text, location="正文 2")])), 1)
        self.assertEqual(len(pipeline.source_payloads(question, [first, self.count_threshold_job("该调整已撤回。", note_id=8)])), 1)
        title = "订单同步延迟 P1 故障复盘"
        scoped = self.count_threshold_job("订单中心发生延迟。将积压阈值告警从20万条调整为5万条。", title=title)
        retracted = self.count_threshold_job("积压阈值告警调整已撤回。", title=title, location="正文 2")
        self.assertEqual(pipeline.source_payloads(question, [scoped, retracted]), [])
        other_parameter = {**retracted, "raw_text": "重试告警调整已撤回。", "excerpt": "重试告警调整已撤回。"}
        self.assertEqual(len(pipeline.source_payloads(question, [scoped, other_parameter])), 1)
        other_source = self.count_threshold_job("积压阈值告警调整已撤回。", title=title, note_id=8)
        self.assertEqual(len(pipeline.source_payloads(question, [scoped, other_source])), 1)

    def test_adjusted_count_later_document_job_keeps_chunk_url_and_location(self) -> None:
        question = "订单系统的队列积压告警被调整到多少条？"
        relation = "将积压阈值告警从20万条调整为5万条。"
        jobs = [{**self.count_threshold_job(text, location=f"PDF 第 {index + 2} 页"),
                 "source_kind": "document", "source_group": "document-7", "document_chunk_id": 40 + index,
                 "document_chunk_order": index} for index, text in enumerate(("目录。", relation))]
        pipeline = object.__new__(RagPipeline)
        source = pipeline.source_payloads(question, jobs)[0]
        self.assertEqual(source["evidence"], relation)
        answer, selected = RagPipeline.enforce_answer_contract(question, "20万条 [1]。", [source])
        self.assertIn(relation, answer)
        self.assertEqual(selected[0]["location"], "PDF 第 3 页")
        self.assertEqual(selected[0]["evidence_locations"], ["PDF 第 3 页"])
        self.assertEqual((selected[0]["chunk_id"], selected[0]["chunk_ids"]), (41, [41]))
        self.assertEqual((selected[0]["chunk_order"], selected[0]["chunk_orders"]), (1, [1]))
        self.assertEqual(selected[0]["url"], "/api/documents/7/file?chunk_id=41")
        duplicate = {**jobs[1], "document_chunk_id": 42, "document_chunk_order": 2}
        self.assertEqual(pipeline.source_payloads(question, [jobs[1], duplicate]), [])

    def test_adjusted_count_contract_requires_current_visible_proof_and_consistent_locator(self) -> None:
        question = "订单系统的队列积压告警被调整到多少条？"
        relation = "将积压阈值告警从20万条调整为5万条。"
        source = object.__new__(RagPipeline).source_payloads(question, [self.count_threshold_job(relation)])[0]
        legacy = {key: value for key, value in source.items() if key != "evidence_alignment_anchor"}
        variants = [
            {**source, "evidence": relation + "修改"}, {**source, "fallback_excerpt": relation + "修改"},
            {**source, "id": 99}, {**source, "type": "document"},
            {**legacy, "location": "正文 2"}, {**legacy, "location": "", "evidence_locations": []},
            {**legacy, "evidence": "延迟47分钟。"},
            {**legacy, "type": "document"},
            {**legacy, "type": "document", "chunk_id": 41, "chunk_ids": [42], "url": "/api/documents/7/file?chunk_id=41"},
            {**legacy, "type": "document", "chunk_id": 41, "chunk_ids": [41], "url": "/api/documents/7/file?chunk_id=42"},
        ]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                self.assertEqual(RagPipeline.enforce_answer_contract(question, "5万条 [1]。", [candidate]), ("现有笔记没有足够信息。", []))
        self.assertTrue(RagPipeline.enforce_answer_contract(question, "20万条 [1]。", [legacy])[1])

    def test_adjusted_count_does_not_recognize_other_count_or_compound_requests(self) -> None:
        questions = [
            "订单系统的队列积压告警是多少条？", "订单系统的队列积压告警调整前是多少条？",
            "订单系统的队列积压告警减少了多少条？", "订单系统的队列积压告警被调整到多少分钟？",
            "订单系统的队列积压告警和重试告警被调整到多少条？",
            "订单系统的队列积压告警被调整到多少条？负责人是谁？",
            "合同 EQ-C31 的数量被调整到多少条？",
        ]
        for question in questions:
            with self.subTest(question=question):
                self.assertIsNone(RagPipeline.adjusted_count_request(question))
        row = "设备编号：EQ-C31；数量：5条；负责人：林岚"
        self.assertIsNone(RagPipeline.adjusted_count_proof("订单系统的队列积压告警被调整到多少条？", "订单系统", row))

    def test_adjusted_count_declined_sources_do_not_recreate_raw_model_context(self) -> None:
        question = "订单系统的队列积压告警被调整到多少条？"
        job = self.count_threshold_job("订单系统将积压阈值告警从20万条调整为5万条。该调整已撤回。")
        pipeline = object.__new__(RagPipeline)
        self.assertTrue(RagPipeline.has_answerable_evidence(question, [job]))
        sources = pipeline.source_payloads(question, [job])
        self.assertEqual(sources, [])
        self.assertEqual(pipeline.contexts([job], sources=sources), [])
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "告警被调整到5万条。", sources), ("现有笔记没有足够信息。", []))
        # Omitting sources is the pre-existing raw-context API, used by callers
        # which have not asked the producer to validate visible evidence.
        self.assertEqual(pipeline.contexts([job]), pipeline.contexts([job], sources=None))
        self.assertEqual(pipeline.contexts([job])[0]["text"], job["excerpt"])

    def test_boundary_fallback_states_the_negative_conclusion_and_keeps_one_source(self) -> None:
        sources = [
            {"index": 1, "title": "项目台账", "evidence": "9 月 8 日开始试点，不代表全量开放。", "matched_terms": ["北辰", "全量"]},
            {"index": 2, "title": "同名合同", "evidence": "北辰云按月服务确认。", "matched_terms": ["北辰"]},
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            "北辰 9 月 8 日是全量上线吗？", "9 月 8 日开始试点 [1]。", sources
        )

        self.assertTrue(answer.startswith("来源[1] 不是。"))
        self.assertEqual([source["title"] for source in selected], [sources[0]["title"]])
        self.assertIn("不代表全量开放", selected[0]["evidence"])

    def test_weak_generic_candidates_are_not_answerable_evidence(self) -> None:
        generic = [{"title": "古诗练习", "raw_text": "赠汪伦表达什么情感？", "hits": ["什么", "功能"], "score": 0.42}]
        focused = [{"title": "办公 Agent", "raw_text": "Agent名称：需求污染检测Agent；核心功能：识别需求偏离", "hits": ["需求污染检测"], "score": 0.42}]

        self.assertFalse(RagPipeline.has_answerable_evidence("需求污染检测Agent有什么功能？", generic))
        self.assertTrue(RagPipeline.has_answerable_evidence("需求污染检测Agent有什么功能？", focused))

    def test_explicit_identifiers_block_nearby_but_wrong_evidence(self) -> None:
        cases = [
            (
                "Claude 和 GPT-4 在当前笔记里的对比如何？",
                "Qwen3-0.6B 在本机运行较快，适合分类。",
            ),
            (
                "供应商B红色预警阈值是多少？",
                "供应商A交货延迟率大于 10% 触发红色预警。",
            ),
            (
                "MacBook 上的 OCR 速度是多少？",
                "Windows 工作站单图 OCR 延迟为 1.5-2.5 秒。",
            ),
            (
                "BGE 是否使用 HNSW？",
                "BGE-small-zh 使用线性扫描，并计划评估 sqlite-vec。",
            ),
            (
                "Qwen3-4B 显存占用是多少？",
                "Qwen3-0.6B 纯 CPU 运行，显存占用为 0。",
            ),
        ]

        for question, evidence in cases:
            with self.subTest(question=question):
                chunk = {
                    "title": "相近资料",
                    "raw_text": evidence,
                    "hits": ["模型", "OCR", "BGE", "供应商", "显存"],
                    "score": 0.9,
                }
                self.assertFalse(RagPipeline.has_answerable_evidence(question, [chunk]))

    def test_explicit_identifier_gate_keeps_direct_positive_evidence(self) -> None:
        cases = [
            (
                "Claude 和 GPT-4 在当前笔记里的对比如何？",
                "Claude 与 GPT-4 的对比结论是 Claude 长文更稳，GPT-4 工具生态更广。",
            ),
            (
                "供应商A红色预警阈值是多少？",
                "供应商A交货延迟率大于 10% 触发红色预警。",
            ),
            (
                "Windows 上的 OCR 速度是多少？",
                "Windows 工作站单图 OCR 延迟为 1.5-2.5 秒。",
            ),
            (
                "BGE 使用什么检索方式？",
                "BGE-small-zh 当前使用线性扫描。",
            ),
            (
                "Qwen3-0.6B 显存占用是多少？",
                "Qwen3-0.6B 纯 CPU 运行，显存占用为 0。",
            ),
        ]

        for question, evidence in cases:
            with self.subTest(question=question):
                chunk = {
                    "title": "直接证据",
                    "raw_text": evidence,
                    "hits": ["对比", "红色预警", "OCR", "检索", "显存"],
                    "score": 0.9,
                }
                self.assertTrue(RagPipeline.has_answerable_evidence(question, [chunk]))

    def test_identifier_matching_does_not_confuse_version_prefixes(self) -> None:
        self.assertTrue(
            RagPipeline.text_contains_identifier(
                "Qwen3-4B-Instruct-2507 Q4_K_M", "Qwen3-4B"
            )
        )
        self.assertFalse(
            RagPipeline.text_contains_identifier("Qwen3-0.6B Q8_0", "Qwen3-4B")
        )
        self.assertFalse(
            RagPipeline.text_contains_identifier("GPT-4o", "GPT-4")
        )

    def test_numeric_identifier_keeps_line_boundary_after_whitespace_normalisation(self) -> None:
        text = "用 Pillow 把图缩放到最长边 2048\n2. 设置推理线程为 2"

        self.assertTrue(RagPipeline.text_contains_identifier(text, "2048"))
        self.assertFalse(RagPipeline.text_contains_identifier(text, "20482"))

    def test_purpose_question_requires_local_operation_evidence(self) -> None:
        question = "Pillow 压图的目的是什么？"
        positive = {
            "title": "FastAPI 后端 OCR 异常排查记录",
            "raw_text": "在 OcrService.enqueue 之前用 Pillow 把图缩放到最长边 2048，减少大图处理负担。",
            "hits": ["Pillow"],
            "score": 0.45,
        }
        incidental = {
            "title": "依赖清单",
            "raw_text": "已安装 Pillow 10.0。另一个模块负责 OCR 调度。",
            "hits": ["Pillow"],
            "score": 0.9,
        }

        self.assertFalse(RagPipeline.requires_definition_evidence(question))
        self.assertTrue(RagPipeline.has_answerable_evidence(question, [positive]))
        self.assertFalse(RagPipeline.has_answerable_evidence(question, [incidental]))

    def test_failure_state_question_requires_explicit_state_assignment(self) -> None:
        question = "识别失败时状态应该是什么？"
        positive = {
            "title": "FastAPI 后端 OCR 异常排查记录",
            "raw_text": "捕获异常，失败时把 ocr_status 置为 failed，而不是让请求挂住。",
            "hits": ["失败时"],
            "score": 0.45,
        }
        incidental = {
            "title": "异常处理",
            "raw_text": "识别失败时记录日志，保留原有状态等待人工检查。",
            "hits": ["识别失败", "状态"],
            "score": 0.9,
        }

        self.assertFalse(RagPipeline.requires_definition_evidence(question))
        self.assertTrue(RagPipeline.has_answerable_evidence(question, [positive]))
        self.assertFalse(RagPipeline.has_answerable_evidence(question, [incidental]))

    def test_usage_context_accepts_exact_tool_and_numeric_boundary(self) -> None:
        question = "Pillow缩放2048是在什么问题里用的？"
        positive = {
            "title": "FastAPI 后端 OCR 异常排查记录",
            "raw_text": "在 OcrService.enqueue 之前用 Pillow 把图缩放到最长边 2048\n2. 设置线程数。",
            "hits": ["Pillow", "2048", "缩放"],
            "score": 0.51,
        }
        wrong_value = {
            "title": "图片预处理",
            "raw_text": "用 Pillow 把图缩放到最长边 4096。",
            "hits": ["Pillow", "缩放"],
            "score": 0.9,
        }

        self.assertTrue(RagPipeline.has_answerable_evidence(question, [positive]))
        self.assertFalse(RagPipeline.has_answerable_evidence(question, [wrong_value]))

    def test_declared_noise_candidate_is_filtered_from_normal_question(self) -> None:
        candidate = {
            "title": "干扰项：菜谱",
            "raw_text": "和供应商、KRI、Qwen3、BGE 都没关系，纯测试检索抗干扰能力。",
        }

        self.assertTrue(
            RagPipeline.candidate_declares_irrelevance(
                "Qwen3 与 BGE 选型时各自关注什么？", candidate
            )
        )

    def test_negative_relation_evidence_remains_for_relationship_question(self) -> None:
        candidate = {
            "title": "范围说明",
            "raw_text": "菜谱与 BGE 检索无关，这是测试边界。",
        }

        self.assertFalse(
            RagPipeline.candidate_declares_irrelevance(
                "菜谱和 BGE 检索有关系吗？", candidate
            )
        )

    def test_focused_query_terms_keep_agent_name_and_drop_question_scaffolding(self) -> None:
        terms = RagPipeline.query_terms("需求污染检测Agent有什么样的功能，什么场景有用？")

        focused = RagPipeline.focused_query_terms(terms)

        self.assertIn("需求污染检测", focused)
        self.assertNotIn("什么样的功能", focused)
        self.assertNotIn("什么场景有用", focused)

    def test_agent_entity_ignores_optional_space_before_suffix(self) -> None:
        compact = "项目风险扫描Agent 的核心功能和适用场景是什么？"
        spaced = "项目风险扫描 Agent 的核心功能和适用场景是什么？"
        source = "Agent名称：项目风险扫描 Agent；核心功能：识别项目资料中的风险；适用场景：项目复盘。"
        chunk = {"title": "Agent 场景清单", "raw_text": source, "hits": ["项目风险扫描"]}

        self.assertEqual(RagPipeline.explicit_entity_terms(compact), ["项目风险扫描Agent"])
        self.assertEqual(RagPipeline.explicit_entity_terms(spaced), ["项目风险扫描Agent"])
        self.assertEqual(RagPipeline.retrieval_anchor_terms(compact), ["项目风险扫描Agent"])
        self.assertTrue(RagPipeline.has_answerable_evidence(compact, [chunk]))
        self.assertEqual(RagPipeline.retain_focused_chunks(compact, [chunk]), [chunk])

    def test_named_agent_answer_contract_accepts_a_spaced_table_row(self) -> None:
        question = "项目风险扫描Agent 的核心功能和适用场景是什么？"
        source = {
            "index": 1,
            "title": "Agent场景清单",
            "evidence": "Agent名称：项目风险扫描Agent；核心功能：识别项目资料中的风险；适用场景：项目复盘。",
            "fallback_excerpt": "| 2 | 项目风险扫描 Agent | 识别项目资料中的风险 | 项目复盘 |",
            "matched_terms": ["项目风险扫描", "Agent"],
        }

        answer, selected = RagPipeline.enforce_answer_contract(question, "现有笔记没有足够信息。", [source])

        self.assertIn("核心功能：识别项目资料中的风险", answer)
        self.assertEqual(selected, [source])

    def test_answer_contract_keeps_field_only_legacy_evidence_usable(self) -> None:
        source = {"index": 1, "evidence": "核心功能：识别需求偏离；适用场景：产品研发", "matched_terms": ["需求污染检测"]}

        self.assertEqual(
            RagPipeline.answer_contract_anchor_terms("需求污染检测Agent有什么功能？", [source]),
            [],
        )

    def test_agent_action_phrase_is_not_mistaken_for_an_entity_name(self) -> None:
        self.assertEqual(RagPipeline.explicit_entity_terms("如何扫描低效Agent？"), [])
        self.assertEqual(RagPipeline.explicit_entity_terms("隐形低效扫描Agent适合什么场景？"), ["隐形低效扫描Agent"])

    def test_evidence_fallback_refuses_generic_question_words_as_the_only_match(self) -> None:
        sources = [{"index": 1, "evidence": "赠汪伦表达了诗人什么样的情感？", "matched_terms": ["什么样的", "有什么"]}]

        self.assertFalse(RagPipeline.can_use_evidence_fallback(sources))

    def test_focused_entity_result_does_not_keep_generic_semantic_neighbours(self) -> None:
        target = {"title": "办公Agent创意.xlsx", "raw_text": "Agent名称：需求污染检测Agent；核心功能：识别需求偏离"}
        unrelated = {"title": "古诗练习", "raw_text": "赠汪伦表达了诗人什么样的情感？"}

        selected = RagPipeline.retain_focused_chunks(
            "需求污染检测Agent有什么样的功能，什么场景有用？", [target, unrelated]
        )

        self.assertEqual(selected, [target])

    def test_broad_topic_keeps_multi_source_context(self) -> None:
        first = {"title": "供应商风险制度", "raw_text": "供应商风险阈值为 50 万元。"}
        second = {"title": "采购审批制度", "raw_text": "超过风险阈值需要升级审批。"}

        selected = RagPipeline.retain_focused_chunks("供应商风险阈值超过后怎么办？", [first, second])

        self.assertEqual(selected, [first, second])

    def test_multiple_explicit_entities_keep_multi_source_context(self) -> None:
        first = {"title": "需求污染检测", "raw_text": "Agent名称：需求污染检测Agent；核心功能：识别偏离"}
        second = {"title": "影子团队", "raw_text": "Agent名称：AI影子团队Agent；核心功能：创建数字孪生团队"}

        selected = RagPipeline.retain_focused_chunks(
            "需求污染检测Agent和AI影子团队Agent分别有什么功能？", [first, second]
        )

        self.assertEqual(selected, [first, second])

    def test_compound_question_splits_explicit_subquestions_only(self) -> None:
        clauses = RagPipeline.compound_query_clauses(
            "请分别说明：地球是距离太阳第几颗行星？；火星有几颗卫星，分别叫什么？"
        )

        self.assertEqual(clauses, ["地球是距离太阳第几颗行星？", "火星有几颗卫星，分别叫什么？"])
        self.assertEqual(RagPipeline.compound_query_clauses("火星有几颗卫星？"), ["火星有几颗卫星？"])

    def test_compound_question_splits_two_substantive_tasks_joined_by_and(self) -> None:
        clauses = RagPipeline.compound_query_clauses(
            "会议是否有效推进需要用什么工具，并且我该如何扫描低效Agent？"
        )

        self.assertEqual(clauses, ["会议是否有效推进需要用什么工具", "我该如何扫描低效Agent"])

    @staticmethod
    def parallel_check_sources() -> list[dict]:
        passages = [
            ("项目上线手册.pdf", "第 9 页", "安全检查", "上线前安全检查：完成最小权限复核，并关闭全部临时测试账号。"),
            ("项目交付清单.xlsx", "工作表：质量检查，第 12-16 行", "质量检查", "上线前质量检查：抽样核对二十条迁移记录，确认必填字段完整率达到 100%。"),
        ]
        return [
            {
                "index": index, "title": title, "location": location,
                "evidence": evidence, "fallback_excerpt": evidence,
                "matched_terms": [focus, "上线前", "检查"],
                "matched_clauses": [f"项目上线前需要完成{focus}"],
            }
            for index, (title, location, focus, evidence) in enumerate(passages, 1)
        ]

    def test_quantified_parallel_tasks_expand_only_matching_shared_task_counts(self) -> None:
        for count, topics, focuses in (
            ("两", "质量与安全", ["质量检查", "安全检查"]),
            ("2", "质量检查和安全", ["质量检查", "安全检查"]),
            ("三", "质量、安全与合规", ["质量检查", "安全检查", "合规检查"]),
        ):
            with self.subTest(count=count):
                question = f"项目上线前需要完成哪{count}项{topics}检查？"
                self.assertEqual(
                    RagPipeline.compound_query_clauses(question),
                    [f"项目上线前需要完成{focus}" for focus in focuses],
                )
        for question in (
            "项目上线前需要完成哪项安全检查？",
            "项目上线前需要完成哪三项质量与安全检查？",
            "项目上线前需要完成哪两项安全与安全检查？",
            "质量与安全检查如何结合？",
        ):
            with self.subTest(question=question):
                self.assertEqual(RagPipeline.compound_query_clauses(question), [question])

    def test_quantified_parallel_contract_recovers_one_cited_task_with_precise_locations(self) -> None:
        question = "项目上线前需要完成哪两项质量与安全检查？"
        for annotations in (True, False):
            with self.subTest(annotations=annotations):
                sources = self.parallel_check_sources()
                if not annotations:
                    for source in sources:
                        source.pop("matched_clauses")
                answer, selected = RagPipeline.enforce_answer_contract(
                    question, f"{sources[0]['evidence']} [1]", sources,
                )
                self.assertIn("最小权限复核", answer)
                self.assertIn("二十条迁移记录", answer)
                self.assertEqual(RagPipeline.cited_indexes(answer), {1, 2})
                self.assertEqual([source["location"] for source in selected], [
                    "第 9 页", "工作表：质量检查，第 12-16 行",
                ])

    def test_quantified_parallel_numeric_recovery_keeps_both_tasks(self) -> None:
        question = "项目上线前需要完成哪两项质量与安全检查？"
        sources = self.parallel_check_sources()
        generated = (
            f"{sources[0]['evidence'].rstrip('。')} [1]。\n"
            f"{sources[1]['evidence'].replace('二十条', '20条').rstrip('。')} [2]。"
        )
        self.assertTrue(RagPipeline.answer_has_uncovered_exact_values(question, generated, sources))
        answer, selected = RagPipeline.enforce_answer_contract(question, generated, sources)
        self.assertIn("二十条迁移记录", answer)
        self.assertIn("最小权限复核", answer)
        self.assertEqual(RagPipeline.cited_indexes(answer), {1, 2})
        self.assertEqual(len(selected), 2)

    def test_quantified_parallel_three_tasks_require_all_three_visible_facts(self) -> None:
        question = "项目上线前需要完成哪三项质量、安全与合规检查？"
        sources = self.parallel_check_sources()
        evidence = "上线前合规检查：核验全部授权文件均在有效期内。"
        sources.append({
            "index": 3, "title": "授权规范", "evidence": evidence,
            "fallback_excerpt": evidence, "matched_terms": ["合规检查"],
            "matched_clauses": ["项目上线前需要完成合规检查"],
        })
        answer, selected = RagPipeline.enforce_answer_contract(
            question, f"{sources[0]['evidence']} [1]", sources,
        )
        self.assertEqual(len(selected), 3)
        self.assertEqual(RagPipeline.cited_indexes(answer), {1, 2, 3})
        self.assertIn("核验全部授权文件", answer)

        # A title and stale clause annotation cannot supply the absent task.
        sources[1]["title"] = "质量检查和合规检查清单"
        sources[1]["matched_clauses"].append("项目上线前需要完成合规检查")
        answer, selected = RagPipeline.enforce_answer_contract(
            question, "三项检查全部完成，授权文件均有效 [1][2]。", sources[:2],
        )
        self.assertEqual(answer, "现有笔记没有足够信息。")
        self.assertEqual(selected, [])

    def test_quantified_parallel_one_source_can_cover_both_tasks(self) -> None:
        question = "项目上线前需要完成哪两项质量与安全检查？"
        sources = self.parallel_check_sources()
        sources[0]["evidence"] += "\n" + sources[1]["evidence"]
        sources[0]["fallback_excerpt"] = sources[0]["evidence"]
        sources[0]["matched_clauses"] += sources[1]["matched_clauses"]
        for generated in ("现有笔记没有足够信息。", "上线前安全检查：完成最小权限复核，并关闭全部临时测试账号 [1]。"):
            with self.subTest(generated=generated):
                answer, selected = RagPipeline.enforce_answer_contract(question, generated, sources[:1])
                self.assertEqual(len(selected), 1)
                self.assertEqual(RagPipeline.cited_indexes(answer), {1})
                self.assertIn("质量检查", answer)
                self.assertIn("二十条迁移记录", answer)
                self.assertIn("安全检查", answer)
                self.assertIn("二十条迁移记录", selected[0]["evidence"])

    def test_quantified_parallel_fallback_background_cannot_replace_visible_task_evidence(self) -> None:
        question = "项目上线前需要完成哪两项质量与安全检查？"
        sources = self.parallel_check_sources()
        sources[1]["evidence"] = "项目交付工作正在推进。"
        answer, selected = RagPipeline.enforce_answer_contract(
            question, "安全检查和质量检查均已完成 [1][2]。", sources,
        )
        self.assertEqual(answer, "现有笔记没有足够信息。")
        self.assertEqual(selected, [])

    def test_quantified_parallel_missing_task_is_not_covered_by_similar_chunk(self) -> None:
        question = "项目上线前需要完成哪两项质量与安全检查？"
        clauses = RagPipeline.compound_query_clauses(question)
        sources = self.parallel_check_sources()
        chunks = [
            {
                "note": SimpleNamespace(id=source["index"], updated_at=0),
                "chunk_identity": str(source["index"]), "source_group": str(source["index"]),
                "title": source["title"], "raw_text": source["evidence"], "excerpt": source["evidence"],
                "hits": source["matched_terms"], "score": 0.85, "rank_score": 20,
                "location": source["location"], "reason": "子问题命中",
            }
            for source in sources
        ]
        selected, _, reserved = RagPipeline.merge_compound_results(
            question, clauses, chunks, [chunks, chunks], limit=3,
        )
        self.assertEqual(reserved, 2)
        for chunk in selected:
            expected = "安全检查" if chunk["chunk_identity"] == "1" else "质量检查"
            self.assertEqual(chunk["compound_clauses"], [f"项目上线前需要完成{expected}"])
        self.assertTrue(RagPipeline.has_answerable_evidence(question, selected))
        self.assertFalse(RagPipeline.has_answerable_evidence(question, selected[:1]))
        # Even misleading annotations cannot turn security evidence into quality evidence.
        self.assertFalse(RagPipeline.has_answerable_evidence(question, [{
            **chunks[0], "title": "质量检查和安全检查", "compound_clauses": clauses,
        }]))
        payloads = object.__new__(RagPipeline).source_payloads(question, selected)
        self.assertEqual(len(payloads), 2)
        self.assertTrue(all(len(source["matched_clauses"]) == 1 for source in payloads))

    def test_single_check_does_not_expand_sources_and_count_mismatch_invents_no_task(self) -> None:
        sources = self.parallel_check_sources()
        question = "项目上线前需要完成哪项安全检查？"
        self.assertFalse(RagPipeline.requires_multiple_sources(question, sources))
        answer, selected = RagPipeline.enforce_answer_contract(
            question, f"{sources[0]['evidence']} [1]", sources,
        )
        self.assertEqual(len(selected), 1)
        self.assertNotIn("质量检查", answer)

        mismatch = "项目上线前需要完成哪三项质量与安全检查？"
        self.assertTrue(RagPipeline.requires_multiple_sources(mismatch, sources))
        answer, selected = RagPipeline.enforce_answer_contract(
            mismatch, "三项检查全部完成，第三项是核验授权文件 [1][2]。", sources,
        )
        self.assertEqual(len(selected), 2)
        self.assertIn("最小权限复核", answer)
        self.assertIn("二十条迁移记录", answer)
        for unsupported in ("三项检查全部", "第三项", "授权文件"):
            self.assertNotIn(unsupported, answer)

    def test_three_checks_in_two_categories_can_be_supported_by_one_source(self) -> None:
        question = "项目上线前需要完成哪三项质量与安全检查？"
        evidence = (
            "上线前三项检查：第一项质量检查为抽样核对迁移记录；"
            "第二项质量检查为复核必填字段完整率；第三项安全检查为关闭临时测试账号。"
        )
        sources = [{
            "index": 1, "title": "上线检查清单", "evidence": evidence,
            "fallback_excerpt": evidence, "matched_terms": ["质量检查", "安全检查", "上线前"],
        }]
        self.assertEqual(RagPipeline.compound_query_clauses(question), [question])
        answer, selected = RagPipeline.enforce_answer_contract(question, f"来源[1] {evidence}", sources)
        self.assertEqual(len(selected), 1)
        for supported in ("抽样核对迁移记录", "复核必填字段完整率", "关闭临时测试账号"):
            self.assertIn(supported, answer)

    def test_compound_question_decomposes_comparison_without_splitting_plain_topic(self) -> None:
        self.assertEqual(
            RagPipeline.compound_query_clauses("工业互联网和人工智能各自承担什么角色？"),
            ["工业互联网承担什么角色", "人工智能承担什么角色"],
        )
        self.assertEqual(
            RagPipeline.compound_query_clauses("工业互联网与人工智能融合水平何时提升？"),
            ["工业互联网与人工智能融合水平何时提升？"],
        )
        self.assertEqual(
            RagPipeline.compound_query_clauses("打包和模型部署的内存或体积限制分别是什么？"),
            [
                "打包的内存或体积限制是什么",
                "模型部署的内存或体积限制是什么",
            ],
        )
        self.assertEqual(
            RagPipeline.compound_query_clauses("OCR 和向量检索的性能目标分别是多少？"),
            [
                "OCR的性能目标是多少",
                "向量检索的性能目标是多少",
            ],
        )

    def test_remediation_evidence_prefers_explicit_solution_steps(self) -> None:
        question = "OCR 优化采取了什么手段？"
        generic = "OCR 完成后回调索引服务，继续执行后续任务。"
        solution = (
            "解决方案：\n"
            "1. 使用 Pillow 把图片缩放到最长边 2048\n"
            "2. 设置推理线程为 2，并捕获失败状态\n"
            "修复后延迟降到 2 秒且连续一周无复现。"
        )

        self.assertGreater(
            RagPipeline.remediation_evidence_bonus(question, solution),
            RagPipeline.remediation_evidence_bonus(question, generic),
        )

    def test_resource_limit_evidence_prefers_constraint_and_current_measurement(self) -> None:
        question = "打包和模型部署的内存或体积限制分别是什么？"
        packaging_constraint = "模型文件不能打进 exe，用 --add-data 会爆体积到 3GB。"
        packaging_summary = "最终打包结构总体积约 1.8GB。"
        current_memory = "实测数据：内存占用常驻约 900MB。"
        future_memory = "下一步试 1.7B，预期内存约 1.5GB。"

        self.assertGreater(
            RagPipeline.scalar_attribute_evidence_bonus(question, packaging_constraint),
            RagPipeline.scalar_attribute_evidence_bonus(question, packaging_summary),
        )
        self.assertGreater(
            RagPipeline.scalar_attribute_evidence_bonus(question, current_memory),
            RagPipeline.scalar_attribute_evidence_bonus(question, future_memory),
        )

    def test_attribute_subject_coverage_beats_incidental_matching_unit(self) -> None:
        question = "模型部署的内存或体积限制是什么"
        deployment = {
            "title": "Qwen 本地部署记录",
            "raw_text": "该模型采用纯 CPU 部署，实测内存占用常驻约 900MB。",
            "hits": ["模型", "部署", "内存"],
            "rank_score": 12.0,
            "score": 0.62,
        }
        incidental = {
            "title": "OCR 异常排查记录",
            "raw_text": "检测模型处理 4K 图片时会临时分配 2GB 内存。",
            "hits": ["模型", "内存"],
            "rank_score": 18.0,
            "score": 0.70,
        }

        self.assertGreater(
            RagPipeline._source_evidence_priority(question, deployment),
            RagPipeline._source_evidence_priority(question, incidental),
        )

    def test_current_resource_measurement_beats_future_plan_in_same_source(self) -> None:
        question = "模型部署的内存或体积限制是什么"
        current = {
            "title": "Qwen3-0.6B 本地部署笔记",
            "raw_text": "实测数据：内存占用常驻约 900MB。",
            "hits": ["内存", "部署"],
            "rank_score": 99.7,
            "score": 0.58,
        }
        future = {
            "title": "Qwen3-0.6B 本地部署笔记",
            "raw_text": "下一步试 1.7B 模型，预期内存约 1.5GB。",
            "hits": ["内存", "模型", "部署"],
            "rank_score": 100.0,
            "score": 0.64,
        }

        self.assertGreater(
            RagPipeline._source_evidence_priority(question, current),
            RagPipeline._source_evidence_priority(question, future),
        )

    def test_attribute_subject_coverage_downweights_broad_noun(self) -> None:
        question = "模型部署的内存或体积限制是什么"
        deployment = "Qwen 本地部署实测内存占用常驻约 900MB。"
        incidental_model = "OCR 检测模型处理图片时分配 2GB 内存。"

        self.assertGreater(
            RagPipeline.attribute_subject_coverage(question, deployment)[1],
            RagPipeline.attribute_subject_coverage(question, incidental_model)[1],
        )

    def test_compound_clause_retrieval_is_deeper_than_final_context(self) -> None:
        self.assertEqual(RagPipeline.compound_candidate_limit(5), 8)
        self.assertEqual(RagPipeline.compound_candidate_limit(10), 10)

    def test_compound_question_decomposes_cross_source_office_patterns(self) -> None:
        cases = {
            "采购审批里的紧急采购，和订单 P1 故障处置有什么共同点和区别？": [
                "采购审批里的紧急采购",
                "订单 P1 故障处置",
            ],
            "北辰项目的变更什么时候要上变更委员会，订单故障长期改进里什么时候加队列告警？": [
                "北辰项目的变更什么时候要上变更委员会",
                "订单故障长期改进里什么时候加队列告警",
            ],
            "差旅费用和采购办公耗材的合规边界是什么？": [
                "差旅费用的合规边界",
                "采购办公耗材的合规边界",
            ],
            "合同 HT-2026-021 的付款安排是否可能触发采购审批中的法务会签？": [
                "合同 HT-2026-021 的付款安排",
                "采购审批中的法务会签",
            ],
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(RagPipeline.compound_query_clauses(question), expected)

    def test_compound_question_keeps_asymmetric_left_fact_intact(self) -> None:
        self.assertEqual(
            RagPipeline.compound_query_clauses(
                "招聘面试的结构化反馈和会议纪要追踪Agent在责任人缺失时的处理有什么不同？"
            ),
            ["招聘面试的结构化反馈", "会议纪要追踪Agent在责任人缺失时的处理"],
        )

    def test_compound_clause_queries_bridge_compliance_boundary_to_rules(self) -> None:
        self.assertEqual(
            RagPipeline.compound_clause_queries("采购办公耗材的合规边界"),
            ["采购办公耗材的合规边界", "办公耗材采购审批规则"],
        )
        self.assertEqual(
            RagPipeline.compound_clause_queries("差旅费用的合规边界"),
            ["差旅费用的合规边界", "差旅费用审批规则"],
        )
        self.assertEqual(
            RagPipeline.compound_clause_queries("工业互联网与人工智能融合"),
            ["工业互联网与人工智能融合"],
        )

    def test_compound_subquery_keeps_only_its_own_formatting_aliases(self) -> None:
        aliases = ["BGE", "项目风险扫描 Agent"]

        self.assertEqual(
            RagPipeline.scope_extra_terms_for_subquery("Qwen3关注什么", aliases),
            [],
        )
        self.assertEqual(
            RagPipeline.scope_extra_terms_for_subquery("BGE 选型时关注什么", aliases),
            ["BGE"],
        )
        self.assertEqual(
            RagPipeline.scope_extra_terms_for_subquery(
                "项目风险扫描Agent的核心功能",
                aliases,
            ),
            ["项目风险扫描 Agent"],
        )

    def test_query_variant_merge_keeps_original_order_and_adds_new_evidence(self) -> None:
        first = {"chunk_identity": "a", "title": "原始命中"}
        duplicate = {"chunk_identity": "a", "title": "改写重复命中"}
        second = {"chunk_identity": "b", "title": "改写补充命中"}

        self.assertEqual(
            RagPipeline.merge_query_variants([[first], [duplicate, second]]),
            [first, second],
        )

    def test_compound_merge_reserves_one_credible_source_per_clause(self) -> None:
        def chunk(identity: str, source: str, rank: float) -> dict:
            note = type("Note", (), {"id": source, "updated_at": 0})()
            return {
                "note": note,
                "source_id": source,
                "source_group": source,
                "chunk_identity": identity,
                "kind": "content",
                "excerpt": identity,
                "hits": [identity],
                "lexical_score": rank,
                "fts_score": 0.0,
                "score": 0.7,
                "rank_score": rank,
            }

        source_a_1 = chunk("a-1", "source-a", 20.0)
        source_a_2 = chunk("a-2", "source-a", 19.0)
        source_b = chunk("b-1", "source-b", 8.0)

        selected, merged_count, reserved_count = RagPipeline.merge_compound_results(
            "主题 A 和主题 B 有什么不同？",
            ["主题 A", "主题 B"],
            [source_a_1, source_a_2],
            [[source_a_1, source_a_2], [source_a_2, source_b]],
            limit=2,
        )

        self.assertEqual({item["source_group"] for item in selected}, {"source-a", "source-b"})
        self.assertEqual(merged_count, 3)
        self.assertEqual(reserved_count, 2)
        by_source = {item["source_group"]: item for item in selected}
        self.assertEqual(by_source["source-a"]["compound_clauses"], ["主题 A"])
        self.assertEqual(by_source["source-b"]["compound_clauses"], ["主题 B"])
        self.assertTrue(by_source["source-a"]["compound_reserved"])
        self.assertTrue(by_source["source-b"]["compound_reserved"])

    def test_compound_merge_reserves_substantive_selection_evidence(self) -> None:
        def chunk(
            identity: str,
            source: str,
            raw_text: str,
            rank: float,
            hits: list[str],
        ) -> dict:
            note = type("Note", (), {"id": source, "updated_at": 0})()
            return {
                "note": note,
                "source_id": source,
                "source_group": source,
                "chunk_identity": identity,
                "kind": "content",
                "title": source,
                "raw_text": raw_text,
                "excerpt": raw_text,
                "hits": hits,
                "lexical_score": rank,
                "fts_score": 1.0,
                "score": 0.7,
                "rank_score": rank,
            }

        qwen_heading = chunk(
            "qwen-heading",
            "qwen-source",
            "Qwen3 本地部署选型：",
            120.0,
            ["Qwen3", "选型"],
        )
        qwen_criteria = chunk(
            "qwen-criteria",
            "qwen-source",
            "Qwen3 选型关注推理速度、模型大小、CPU 内存占用和回答准确度。",
            95.0,
            ["Qwen3", "选型"],
        )
        bge_heading = chunk(
            "bge-heading",
            "bge-source",
            "BGE 向量检索选型：",
            118.0,
            ["BGE", "选型"],
        )
        bge_criteria = chunk(
            "bge-criteria",
            "bge-source",
            "BGE 选型关注中文语义质量、向量维度、检索延迟和语料规模。",
            92.0,
            ["BGE", "选型"],
        )

        selected, _, reserved_count = RagPipeline.merge_compound_results(
            "Qwen3 与 BGE 选型时各自关注什么？",
            ["Qwen3 选型时关注什么", "BGE 选型时关注什么"],
            [qwen_heading, bge_heading],
            [
                [qwen_heading, qwen_criteria],
                [bge_heading, bge_criteria],
            ],
            limit=2,
        )

        self.assertEqual(
            [item["chunk_identity"] for item in selected],
            ["qwen-criteria", "bge-criteria"],
        )
        self.assertEqual(reserved_count, 2)

    def test_compound_answerability_requires_evidence_for_every_annotated_clause(self) -> None:
        question = "紧急采购有哪些要求？；订单 P1 故障如何处理？"
        clauses = RagPipeline.compound_query_clauses(question)
        procurement = {
            "title": "采购规则",
            "raw_text": "紧急采购不能跳过审批，申请后两个工作日内补齐比价依据。",
            "hits": ["紧急采购", "审批"],
            "score": 0.78,
            "compound_clauses": [clauses[0]],
        }
        incident = {
            "title": "故障手册",
            "raw_text": "订单 P1 故障需立即建群，并在三十分钟内完成影响范围确认。",
            "hits": ["订单 P1", "故障"],
            "score": 0.76,
            "compound_clauses": [clauses[1]],
        }

        self.assertTrue(RagPipeline.has_answerable_evidence(question, [procurement, incident]))
        self.assertFalse(RagPipeline.has_answerable_evidence(question, [procurement]))

    def test_compound_answerability_does_not_require_generated_filler_words(self) -> None:
        question = "采购审批里的紧急采购，和订单 P1 故障处置有什么共同点和区别？"
        clauses = RagPipeline.compound_query_clauses(question)
        procurement = {
            "title": "采购审批流重构方案",
            "raw_text": "紧急采购不能跳过审批，申请后两个工作日内补齐比价依据。",
            "hits": ["紧急采购", "采购审批"],
            "score": 0.74,
            "compound_clauses": [clauses[0]],
        }
        incident = {
            "title": "订单同步延迟 P1 故障复盘",
            "raw_text": "订单 P1 故障需立即建群，并在三十分钟内完成影响范围确认。",
            "hits": ["订单", "P1", "故障", "处置"],
            "score": 0.70,
            "compound_clauses": [clauses[1]],
        }

        self.assertTrue(RagPipeline.has_answerable_evidence(question, [procurement, incident]))

    def test_compound_source_payload_uses_clause_specific_amount_and_rule_evidence(self) -> None:
        pipeline = object.__new__(RagPipeline)
        question = "采购金额 25 万元且需要预付款，审批上要额外关注什么？"
        clauses = RagPipeline.compound_query_clauses(question)

        def chunk(note_id: int, title: str, text: str, hits: list[str], clause: str) -> dict:
            note = type("Note", (), {"id": note_id, "title": title})()
            return {
                "note": note,
                "source_id": note_id,
                "source_group": f"note-{note_id}",
                "chunk_identity": f"chunk-{note_id}",
                "kind": "content",
                "title": title,
                "raw_text": text,
                "excerpt": text,
                "hits": hits,
                "score": 0.72,
                "rank_score": 18.0,
                "reason": "复合子问题命中",
                "location": "正文 1",
                "compound_clauses": [clause],
            }

        procurement = chunk(
            1,
            "采购审批流重构方案与灰度复盘",
            "单笔含税金额 5 万元以下，由部门负责人和预算负责人审批；"
            "5 万元（含）至 20 万元（不含），增加采购负责人审批；"
            "20 万元（含）以上，增加法务与财务负责人会签。",
            ["采购金额", "25 万元", "审批"],
            clauses[0],
        )
        contract = chunk(
            2,
            "合作伙伴合同付款条款对照表",
            "合同编号：HT-2026-018；预付款比例：0%；"
            "合同编号：HT-2026-021；预付款比例：20%；"
            "任何预付款支付均需符合采购审批流中的金额会签规则。",
            ["预付款", "审批"],
            clauses[1],
        )

        sources = pipeline.source_payloads(question, [procurement, contract])

        self.assertIn("20 万元（含）以上", sources[0]["evidence"])
        self.assertNotIn("5 万元以下", sources[0]["evidence"])
        self.assertIn("任何预付款支付", sources[1]["evidence"])
        self.assertNotIn("预付款比例：0%", sources[1]["evidence"])
        self.assertEqual(sources[0]["matched_clauses"], [clauses[0]])
        self.assertEqual(sources[1]["matched_clauses"], [clauses[1]])

    def test_compound_answer_contract_replaces_plausible_but_wrong_model_blend(self) -> None:
        question = "采购金额 25 万元且需要预付款，审批上要额外关注什么？"
        clauses = RagPipeline.compound_query_clauses(question)
        sources = [
            {
                "index": 1,
                "title": "采购审批规则",
                "evidence": "20 万元（含）以上，增加法务与财务负责人会签。",
                "fallback_excerpt": "20 万元（含）以上，增加法务与财务负责人会签。",
                "matched_terms": ["采购金额", "审批"],
                "matched_clauses": [clauses[0]],
            },
            {
                "index": 2,
                "title": "合同付款规则",
                "evidence": "任何预付款支付均需符合采购审批流中的金额会签规则。",
                "fallback_excerpt": "任何预付款支付均需符合采购审批流中的金额会签规则。",
                "matched_terms": ["预付款", "审批"],
                "matched_clauses": [clauses[1]],
            },
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "采购金额 25 万元只需部门负责人审批 [1]。预付款无需额外关注 [2]。",
            sources,
        )

        self.assertIn("法务与财务负责人会签", answer)
        self.assertIn("任何预付款支付", answer)
        self.assertNotIn("只需部门负责人", answer)
        self.assertEqual(selected, sources)

    def test_compound_answer_contract_keeps_verified_two_source_answer(self) -> None:
        question = "营销活动短信触达和客户数据规范对手机号使用的要求是否一致？"
        clauses = RagPipeline.compound_query_clauses(question)
        sources = [
            {
                "index": 1,
                "title": "营销活动纪要",
                "evidence": "短信只投放已授权手机号。",
                "fallback_excerpt": "短信只投放已授权手机号。",
                "matched_terms": ["短信触达", "手机号"],
                "matched_clauses": [clauses[0]],
            },
            {
                "index": 2,
                "title": "客户数据规范",
                "evidence": "手机号保留前 3 位与后 4 位，中间替换为四个星号。",
                "fallback_excerpt": "手机号保留前 3 位与后 4 位，中间替换为四个星号。",
                "matched_terms": ["客户数据", "手机号"],
                "matched_clauses": [clauses[1]],
            },
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "营销活动要求短信只投放已授权手机号 [1]。客户数据规范要求手机号保留前 3 位与后 4 位，中间替换为四个星号 [2]。",
            sources,
        )

        self.assertIn("来源[1] 营销活动要求短信只投放已授权手机号。", answer)
        self.assertIn("来源[2] 客户数据规范要求手机号保留前 3 位与后 4 位，中间替换为四个星号。", answer)
        self.assertEqual(selected, sources)

    def test_compound_answer_contract_recovers_when_one_clause_source_is_uncited(self) -> None:
        question = "营销活动短信触达和客户数据规范对手机号使用的要求是否一致？"
        clauses = RagPipeline.compound_query_clauses(question)
        sources = [
            {
                "index": 1,
                "title": "营销活动纪要",
                "evidence": "短信只投放已授权手机号。",
                "fallback_excerpt": "短信只投放已授权手机号。",
                "matched_terms": ["短信触达", "手机号"],
                "matched_clauses": [clauses[0]],
            },
            {
                "index": 2,
                "title": "客户数据规范",
                "evidence": "手机号保留前 3 位与后 4 位，中间替换为四个星号。",
                "fallback_excerpt": "手机号保留前 3 位与后 4 位，中间替换为四个星号。",
                "matched_terms": ["客户数据", "手机号"],
                "matched_clauses": [clauses[1]],
            },
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "营销活动要求短信只投放已授权手机号 [1]。",
            sources,
        )

        self.assertIn("来源[1] 短信只投放已授权手机号。", answer)
        self.assertIn("来源[2] 手机号保留前 3 位与后 4 位，中间替换为四个星号。", answer)
        self.assertEqual(selected, sources)

    def test_relationship_question_rejects_a_speculative_shared_purpose(self) -> None:
        question = "知识库的无答案拒答要求，和数据规范禁止把未脱敏数据粘到公共工具之间有什么关系？"
        clauses = RagPipeline.compound_query_clauses(question)
        sources = [
            {
                "index": 1,
                "title": "知识库决策",
                "evidence": "无充分证据时应明确拒答。",
                "fallback_excerpt": "无充分证据时应明确拒答。",
                "matched_terms": ["无答案拒答"],
                "matched_clauses": [clauses[0]],
            },
            {
                "index": 2,
                "title": "数据规范",
                "evidence": "不得把未脱敏客户数据粘贴到公共在线工具。",
                "fallback_excerpt": "不得把未脱敏客户数据粘贴到公共在线工具。",
                "matched_terms": ["未脱敏", "公共工具"],
                "matched_clauses": [clauses[1]],
            },
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "两者均旨在保障数据安全与隐私，防止敏感信息泄露 [1][2]。",
            sources,
        )

        self.assertNotIn("两者均旨在", answer)
        self.assertIn("来源[1] 无充分证据时应明确拒答。", answer)
        self.assertIn("来源[2] 不得把未脱敏客户数据粘贴到公共在线工具。", answer)
        self.assertEqual(selected, sources)

    def test_amount_threshold_gap_explains_why_a_ratio_is_insufficient(self) -> None:
        question = "合同 HT-2026-021 的付款安排是否可能触发采购审批中的法务会签？"
        clauses = RagPipeline.compound_query_clauses(question)
        sources = [
            {
                "index": 1,
                "title": "合同付款条款",
                "evidence": "合同编号：HT-2026-021；预付款比例：20%；币种：人民币。",
                "fallback_excerpt": "合同编号：HT-2026-021；预付款比例：20%；币种：人民币。",
                "matched_terms": ["HT-2026-021", "预付款"],
                "matched_clauses": [clauses[0]],
            },
            {
                "index": 2,
                "title": "采购审批规则",
                "evidence": "20 万元（含）以上，增加法务与财务负责人会签。",
                "fallback_excerpt": "20 万元（含）以上，增加法务与财务负责人会签。",
                "matched_terms": ["法务会签", "金额"],
                "matched_clauses": [clauses[1]],
            },
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            question,
            "达到审批要求时可能触发会签 [2]。",
            sources,
        )

        self.assertIn("取决于合同金额", answer)
        self.assertIn("20 万元（含）以上", answer)
        self.assertIn("预付款比例 20%", answer)
        self.assertIn("不足以确认", answer)
        self.assertEqual([source["title"] for source in selected], ["合同付款条款", "采购审批规则"])

    def test_action_question_selects_the_action_instead_of_a_nearby_definition(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "【处置】09:28 人工扩容消费者；09:41 将积压阈值告警从 20 万条调整为 5 万条。"
            "【经验】P1 判定依据是订单核心链路受阻和预计影响超过 30 分钟。"
        )

        evidence = pipeline.evidence_sentence(
            "订单 P1 故障处置",
            text,
            ["订单", "P1", "故障", "处置"],
        )

        self.assertIn("人工扩容", evidence)
        self.assertNotIn("判定依据", evidence)

    def test_relationship_gate_does_not_capture_a_relationship_noun(self) -> None:
        sources = [{"evidence": "客户关系管理Agent用于整理客户跟进记录。"}]

        self.assertFalse(
            RagPipeline.requires_evidence_first_answer("客户关系管理Agent有什么功能？", sources)
        )
        self.assertTrue(
            RagPipeline.requires_evidence_first_answer("客户数据规范和拒答规则有什么关系？", sources)
        )

    def test_cross_source_synthesis_must_cite_both_sides(self) -> None:
        question = "采购审批里的紧急采购，和订单 P1 故障处置有什么共同点和区别？"
        clauses = RagPipeline.compound_query_clauses(question)
        sources = [
            {"index": 1, "matched_clauses": [clauses[0]]},
            {"index": 2, "matched_clauses": [clauses[1]]},
        ]

        self.assertTrue(RagPipeline.answer_has_under_cited_cross_source_synthesis(
            question,
            "来源[1] 共同点：两者都需要快速处理。\n来源[2] 故障处置需要人工扩容。",
            sources,
        ))
        self.assertFalse(RagPipeline.answer_has_under_cited_cross_source_synthesis(
            question,
            "共同点：两者都需要快速处理 [1][2]。",
            sources,
        ))

    def test_source_first_format_removes_an_orphaned_leading_conjunction(self) -> None:
        self.assertEqual(
            RagPipeline.format_answer_with_leading_sources(
                "采购金额需法务会签 [1]。\n[2]，且预付款需符合采购审批流。"
            ),
            "来源[1] 采购金额需法务会签。\n来源[2] 预付款需符合采购审批流。",
        )

    def test_amount_threshold_gap_is_limited_to_contract_payment_approval(self) -> None:
        sources = [
            {"index": 1, "evidence": "当前完成率为 20%。", "fallback_excerpt": "当前完成率为 20%。"},
            {"index": 2, "evidence": "预算达到 20 万元以上需要复盘。", "fallback_excerpt": "预算达到 20 万元以上需要复盘。"},
        ]

        self.assertEqual(
            RagPipeline.amount_threshold_gap_answer("完成率是否触发项目复盘？", sources),
            "",
        )

    def test_compound_query_does_not_apply_one_impossible_whole_query_anchor(self) -> None:
        question = "差旅费用和采购办公耗材的合规边界是什么？"

        self.assertEqual(RagPipeline.required_retrieval_topics(question), [])
        self.assertEqual(RagPipeline.answer_contract_anchor_terms(question, []), [])
        self.assertFalse(RagPipeline.requires_definition_evidence(question))
        self.assertEqual(RagPipeline.subquery_anchor_terms("差旅费用的合规边界"), [])
        self.assertEqual(
            RagPipeline.subquery_anchor_terms("会议纪要追踪Agent在责任人缺失时的处理"),
            ["会议纪要追踪Agent"],
        )

    def test_compound_answer_contract_keeps_only_source_visible_entity_anchors(self) -> None:
        question = "招聘面试的结构化反馈和会议纪要追踪Agent在责任人缺失时的处理有什么不同？"
        sources = [{
            "title": "办公智能体创意库",
            "evidence": "Agent名称：会议纪要追踪Agent；限制：责任人缺失时只标记待确认。",
        }]

        self.assertEqual(
            RagPipeline.answer_contract_anchor_terms(question, sources),
            ["会议纪要追踪Agent"],
        )

        paraphrased_field_sources = [
            {"title": "风险周报", "evidence": "供应商 A 当前综合黄色。"},
            {"title": "扫描件", "evidence": "第二产地样品只针对关键物料。"},
        ]
        self.assertEqual(
            RagPipeline.answer_contract_anchor_terms(
                "供应商 A 的风险、行动以及样品适用范围分别是什么？",
                paraphrased_field_sources,
            ),
            [],
        )

    def test_multi_facet_evidence_keeps_requested_status_and_action(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "风险评分采用交付准时率、重大质量缺陷和单一产地依赖三项指标。"
            "供应商 A 的交付准时率为 88%，当前综合黄色。"
            "行动：采购经理在 7 月 24 日前取得第二产地样品。"
        )

        evidence = pipeline.evidence_sentence(
            "供应商 A 被判什么颜色、采取什么行动？",
            text,
            ["供应商 A", "颜色", "行动"],
        )

        self.assertIn("当前综合黄色", evidence)
        self.assertIn("取得第二产地样品", evidence)
        self.assertNotIn("风险评分采用", evidence)

    def test_actor_action_evidence_binds_entity_and_keeps_all_responsible_roles(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "【重点供应商】供应商 A 的交付准时率为 88%，当前综合黄色。"
            "行动：采购经理在 7 月 24 日前取得第二产地样品；"
            "质量团队于 7 月 22 日完成现场审核。"
            "供应商 B 当前红色，由供应链团队暂停新增订单。"
            "【下周关注】供应商 C 的交期改善计划。"
        )

        evidence = pipeline.evidence_sentence(
            "那供应商 A 下一步是谁做？",
            text,
            ["供应商 A", "下一步", "谁做"],
        )

        self.assertIn("采购经理在 7 月 24 日前", evidence)
        self.assertIn("质量团队于 7 月 22 日", evidence)
        self.assertNotIn("供应链团队", evidence)
        self.assertNotIn("下周关注", evidence)

    def test_next_action_evidence_rejects_historical_validation_work(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "供应商 A 承诺于 2026-07-24 前提交第二产地样品。"
            "校验人员确认日期中的‘7’存在笔画缺损，但结合原始影像判定为 7 月。"
        )

        evidence = pipeline.evidence_sentence(
            "那供应商 A 下一步是谁做？",
            text,
            ["供应商 A", "下一步", "谁做"],
        )

        self.assertEqual(evidence, "")

    def test_actor_action_field_gate_rejects_a_deadline_without_an_owner(self) -> None:
        question = "那供应商 A 下一步是谁做？"

        self.assertFalse(RagPipeline.requested_answer_fields_are_covered(
            question,
            ["供应商 A 承诺在 7 月 24 日前提交第二产地样品。"],
        ))
        self.assertTrue(RagPipeline.requested_answer_fields_are_covered(
            question,
            ["采购经理在 7 月 24 日前取得第二产地样品；质量团队于 7 月 22 日完成现场审核。"],
        ))

    def test_actor_action_source_filter_removes_entity_only_neighbour(self) -> None:
        question = "那供应商 A 下一步是谁做？"
        sources = [
            {
                "index": 1,
                "evidence": "采购经理在 7 月 24 日前取得第二产地样品；质量团队于 7 月 22 日完成现场审核。",
                "fallback_excerpt": "供应商 A 当前综合黄色。采购经理负责取样，质量团队负责审核。",
            },
            {
                "index": 2,
                "evidence": "供应商 A 承诺于 7 月 24 日前提交第二产地样品。",
                "fallback_excerpt": "供应商 A 承诺于 7 月 24 日前提交第二产地样品。",
            },
            {
                "index": 3,
                "evidence": "由部门负责人和预算负责人审批。",
                "fallback_excerpt": "采购申请按金额由部门负责人和预算负责人审批。",
            },
            {
                "index": 4,
                "evidence": "校验人员确认日期中的‘7’存在笔画缺损。",
                "fallback_excerpt": "供应商 A 的扫描件日期已经由校验人员确认。",
            },
        ]

        retained = RagPipeline.retain_actor_action_sources(question, sources)

        self.assertEqual(len(retained), 1)
        self.assertIn("采购经理", retained[0]["evidence"])
        self.assertEqual(retained[0]["index"], 1)

    def test_actor_action_source_filter_requires_the_named_entity(self) -> None:
        question = "供应商 A 的现场审核由哪个团队在什么时候完成？"
        sources = [
            {
                "index": 1,
                "evidence": "由部门负责人和预算负责人审批。",
                "fallback_excerpt": "采购申请按金额由部门负责人和预算负责人审批。",
            },
            {
                "index": 2,
                "evidence": "质量团队于 7 月 22 日完成现场审核。",
                "fallback_excerpt": "供应商 A 当前综合黄色，质量团队于 7 月 22 日完成现场审核。",
            },
        ]

        retained = RagPipeline.retain_actor_action_sources(question, sources)

        self.assertEqual(len(retained), 1)
        self.assertIn("质量团队", retained[0]["evidence"])
        self.assertEqual(retained[0]["index"], 1)

    def test_multi_facet_evidence_prefers_explicit_no_answer_rule(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = "【回答约束】答案必须带引用；无充分证据时应明确拒答；不得引用无关段落。"

        evidence = pipeline.evidence_sentence(
            "知识库的无答案拒答要求是什么？",
            text,
            ["无答案", "拒答", "要求"],
        )

        self.assertEqual(evidence, "无充分证据时应明确拒答；")

    def test_object_requirement_evidence_binds_phone_in_marketing_rule(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "【渠道安排】短信只投放已授权手机号；站内信覆盖全部符合人群；"
            "第一批灰度 10%。【预算】任何单渠道超预算 5% 必须暂停。"
        )

        evidence = pipeline.evidence_sentence(
            "营销活动短信触达对手机号使用的要求",
            text,
            ["营销活动", "短信触达", "手机号"],
        )

        self.assertEqual(evidence, "【渠道安排】短信只投放已授权手机号；")

    def test_object_requirement_evidence_binds_phone_in_masking_rule(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "【适用范围】敏感信息包括手机号、身份证件号和银行账号。"
            "【脱敏规则】手机号保留前 3 位与后 4 位，中间替换为四个星号；"
            "不得把未脱敏客户数据粘贴到公共在线工具。"
        )

        evidence = pipeline.evidence_sentence(
            "客户数据规范对手机号使用的要求",
            text,
            ["客户数据", "手机号", "规范"],
        )

        self.assertEqual(evidence, "【脱敏规则】手机号保留前 3 位与后 4 位，中间替换为四个星号；")

    def test_explicit_identifier_keeps_its_complete_structured_record(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "合同编号：HT-2026-018；预付款比例：0%；"
            "合同编号：HT-2026-021；付款节点：签约后 15 天支付 20%预付款，验收后 45 天付尾款；预付款比例：20%。"
        )

        evidence = pipeline.evidence_sentence(
            "合同 HT-2026-021 的付款安排是什么？",
            text,
            ["HT-2026-021", "付款安排"],
        )

        self.assertIn("HT-2026-021", evidence)
        self.assertIn("20%预付款", evidence)
        self.assertNotIn("HT-2026-018", evidence)

    def test_generic_asset_row_keeps_identity_and_requested_fields_only(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "资产编号：SN-A17；设备名称：热成像仪；负责人：林岚；状态：在役；安装位置：二号线；"
            "资产编号：SN-B22；设备名称：振动仪；负责人：周宁；状态：维修；安装位置：实验室。"
        )

        evidence = pipeline.evidence_sentence(
            "SN-A17 的负责人和状态是什么？",
            text,
            ["SN-A17", "负责人", "状态"],
        )

        self.assertEqual("资产编号：SN-A17；负责人：林岚；状态：在役", evidence)
        self.assertNotIn("安装位置", evidence)
        self.assertNotIn("SN-B22", evidence)

    def test_structured_row_split_never_mixes_adjacent_asset_records(self) -> None:
        text = (
            "资产编号：SN-A17；设备名称：热成像仪；负责人：林岚；状态：在役；"
            "资产编号：SN-B22；设备名称：振动仪；负责人：周宁；状态：维修。"
        )

        units = RagPipeline.structured_record_units(text)

        self.assertEqual(2, len(units))
        self.assertIn("SN-A17", units[0][1])
        self.assertNotIn("SN-B22", units[0][1])
        self.assertIn("SN-B22", units[1][1])
        self.assertNotIn("SN-A17", units[1][1])

    def test_generic_supplier_row_selects_risk_and_action_without_neighbor_fields(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "供应商：海岳数据；风险等级：黄色；行动项：本周补齐灾备演练记录；负责人：陈琳；"
            "供应商：北辰云；风险等级：红色；行动项：暂停新增订单；负责人：周宁。"
        )

        evidence = pipeline.evidence_sentence(
            "海岳数据供应商的风险和行动项是什么？",
            text,
            ["海岳数据", "供应商", "风险", "行动项"],
        )

        self.assertIn("风险等级：黄色", evidence)
        self.assertIn("行动项：本周补齐灾备演练记录", evidence)
        self.assertNotIn("负责人", evidence)
        self.assertNotIn("北辰云", evidence)

    def test_semicolon_prose_is_not_promoted_to_a_structured_record(self) -> None:
        text = "项目本周完成导入；随后开展问答验证；最后复核引用位置。"

        self.assertEqual([], RagPipeline.structured_record_units(text))

    def test_named_rows_accept_business_states_and_multiple_requested_fields(self) -> None:
        rows = (
            "资产编号：EQ-C31；负责人：陈晨；状态：封存；安装位置：三号库\n"
            "事件编号：INC-D52；持续时间：23 分钟；损失金额：7 万元；根因：线路中断；状态：待复核"
        )
        for question in (
            "EQ-C31 的负责人和状态是什么？",
            "INC-D52 持续了多久，损失多少？",
            "INC-D52 的根因和当前状态是什么？",
        ):
            with self.subTest(question=question):
                chunks = [{"title": "设备记录", "raw_text": rows, "hits": ["EQ-C31", "INC-D52"], "score": 0.8}]
                self.assertTrue(RagPipeline.has_answerable_evidence(question, chunks))

    def test_named_row_missing_fields_never_borrow_from_adjacent_record(self) -> None:
        rows = (
            "资产编号：EQ-C31；设备名称：测温仪；状态：封存\n"
            "资产编号：EQ-C32；负责人：李清；采购价格：800 元；状态：使用中"
        )
        for question in (
            "EQ-C31 的采购价格是多少？", "EQ-C31 由谁负责？",
            "EQ-C31 的采购价格和状态是什么？", "EQ-C31 的负责人和采购价格是什么？",
            "EQ-C31 的状态是什么，并且采购价格是多少？",
            "EQ-C31 的状态是什么？采购价格是多少？",
        ):
            with self.subTest(question=question):
                self.assertFalse(RagPipeline.structured_field_coverage(question, [rows]))
                self.assertFalse(RagPipeline.has_answerable_evidence(question, [{
                    "title": "设备记录", "raw_text": rows, "hits": ["EQ-C31"], "score": 0.8,
                }]))
                answer, sources = RagPipeline.enforce_answer_contract(question, "价格为 800 元 [1]。", [{
                    "index": 1, "title": "设备记录", "evidence": rows,
                    "fallback_excerpt": rows, "matched_terms": ["EQ-C31"],
                }])
                self.assertEqual([], sources)
                self.assertIn("没有足够信息", answer)

    def test_same_identity_fragments_jointly_cover_requested_fields(self) -> None:
        texts = [
            "合同编号：CT-2030-014；风险说明：仍需审核；状态：处理中",
            "合同编号：CT-2030-014；负责人：陈晨；状态：处理中",
        ]
        question = "合同 CT-2030-014 的负责人是谁？"
        chunks = [{"title": "合同台账", "raw_text": text, "hits": ["CT-2030-014"], "score": 0.8} for text in texts]
        self.assertTrue(RagPipeline.structured_field_coverage(question, texts))
        self.assertTrue(RagPipeline.has_answerable_evidence(question, chunks))
        evidence = RagPipeline(None, None).evidence_sentence(question, "\n".join(texts), ["CT-2030-014"])
        self.assertIn("负责人：陈晨", evidence)
        self.assertNotIn("仍需审核", evidence)

    def test_two_field_named_row_can_answer_owner_but_not_neighbor_price(self) -> None:
        text = "设备编号：EQ-C31；负责人：陈晨\n设备编号：EQ-C310；设备名称：水泵；采购价格：1200元"
        chunks = [{"title": "设备台账", "raw_text": text, "hits": ["EQ-C31"], "score": 0.8}]
        self.assertTrue(RagPipeline.has_answerable_evidence("设备 EQ-C31 的负责人是谁？", chunks))
        question = "设备 EQ-C31 的采购价格是多少？"
        self.assertFalse(RagPipeline.has_answerable_evidence(question, chunks))
        _, selected = RagPipeline.enforce_answer_contract(question, "采购价格为 1200 元 [1]。", [{
            "index": 1, "title": "设备台账", "evidence": text,
            "fallback_excerpt": text, "matched_terms": ["EQ-C31"],
        }])
        self.assertEqual([], selected)

    def test_known_owner_field_does_not_bypass_missing_signing_date(self) -> None:
        text = (
            "合同编号：CT-2030-014；负责人：陈晨；状态：处理中\n"
            "合同编号：CT-2030-015；负责人：李清；签约日期：2030-09-01"
        )
        question = "合同 CT-2030-014 的负责人和签约日期是什么？"
        chunks = [{"title": "合同台账", "raw_text": text, "hits": ["CT-2030-014"], "score": 0.8}]
        self.assertFalse(RagPipeline.has_answerable_evidence(question, chunks))
        _, selected = RagPipeline.enforce_answer_contract(question, "负责人为陈晨，签约日期为 2030-09-01 [1]。", [{
            "index": 1, "title": "合同台账", "evidence": text,
            "fallback_excerpt": text, "matched_terms": ["CT-2030-014"],
        }])
        self.assertEqual([], selected)

    def test_shared_record_field_list_keeps_identifier_and_rejects_missing_neighbour_fields(self) -> None:
        question = "合同 EQ-C31 的负责人、付款状态和付款比例分别是什么？"
        clauses = RagPipeline.compound_query_clauses(question)
        self.assertEqual(clauses, [
            "合同 EQ-C31 的负责人是什么", "合同 EQ-C31 的付款状态是什么", "合同 EQ-C31 的付款比例是什么",
        ])
        for absent in ("负责人", "付款状态", "付款比例"):
            with self.subTest(absent=absent):
                fields = {"负责人": "林岚", "付款状态": "待付款", "付款比例": "0%"}
                fields.pop(absent)
                text = "合同编号：EQ-C31；" + "；".join(f"{name}：{value}" for name, value in fields.items())
                text += "\n合同编号：EQ-C310；负责人：周远；付款状态：已付款；付款比例：80%"
                chunks = [{"raw_text": text, "hits": ["EQ-C31", absent], "score": 0.8}]
                self.assertFalse(RagPipeline.has_answerable_evidence(question, chunks))
                answer, selected = RagPipeline.enforce_answer_contract(question, "负责人周远、已付款80% [1]。", [{
                    "index": 1, "evidence": text, "fallback_excerpt": text, "matched_terms": ["EQ-C31"],
                }])
                self.assertEqual(selected, [])
                self.assertIn("没有足够信息", answer)

    def test_shared_record_fields_keep_fragments_from_one_or_multiple_sources(self) -> None:
        question = "合同 EQ-C31 的负责人、付款状态和付款比例分别是什么？"
        clauses = RagPipeline.compound_query_clauses(question)
        for separate_sources in (False, True):
            with self.subTest(separate_sources=separate_sources):
                chunks = []
                for position, field in enumerate(("负责人：林岚", "付款状态：待付款", "付款比例：0%")):
                    source_id = position + 1 if separate_sources else 1
                    text = f"合同编号：EQ-C31；{field}"
                    chunks.append({
                        "note": SimpleNamespace(id=source_id, title="合同记录"), "source_group": str(source_id),
                        "source_id": source_id, "chunk_identity": str(position), "kind": "content",
                        "title": "合同记录", "raw_text": text, "excerpt": text,
                        "hits": ["EQ-C31", field.split("：")[0]], "score": 0.8, "rank_score": 20,
                        "location": f"正文 {position + 1}", "reason": "字段命中", "compound_clauses": [clauses[position]],
                    })
                self.assertTrue(RagPipeline.has_answerable_evidence(question, chunks))
                sources = object.__new__(RagPipeline).source_payloads(question, chunks)
                answer, selected = RagPipeline.enforce_answer_contract(question, "负责人是林岚 [1]。", sources)
                self.assertEqual(len(selected), 3 if separate_sources else 1)
                for value in ("林岚", "待付款", "0%"):
                    self.assertIn(value, answer)
                    self.assertTrue(any(value in source["evidence"] for source in selected))

    def test_named_row_payment_ratio_is_not_contract_amount(self) -> None:
        text = (
            "设备编号：EQ-C31；负责人：陈晨；付款状态：待付款；付款比例：0%\n"
            "设备编号：EQ-C310；负责人：李清；付款状态：已付款；付款比例：80%"
        )
        question = "合同 EQ-C31 的合同金额是多少？"
        chunks = [{"title": "采购台账", "raw_text": text, "hits": ["EQ-C31"], "score": 0.8}]
        self.assertFalse(RagPipeline.has_answerable_evidence(question, chunks))
        _, selected = RagPipeline.enforce_answer_contract(question, "合同金额为 0 元 [1]。", [{
            "index": 1, "title": "采购台账", "evidence": text,
            "fallback_excerpt": text, "matched_terms": ["EQ-C31"],
        }])
        self.assertEqual([], selected)
        positive = text.replace("付款比例：0%", "合同金额：800 元")
        self.assertTrue(RagPipeline.structured_field_coverage(question, [positive]))

    def test_secondary_identity_field_stays_with_its_primary_record(self) -> None:
        text = (
            "合同编号：CT-2030-014；供应商：青禾数据；负责人：陈晨；状态：处理中；"
            "合同编号：CT-2030-015；供应商：北辰云；负责人：李清；状态：完成"
        )
        units = RagPipeline.structured_record_units(text)
        self.assertEqual(2, len(units))
        self.assertIn("CT-2030-014", units[0][1])
        self.assertIn("供应商：青禾数据", units[0][1])
        self.assertNotIn("CT-2030-015", units[0][1])
        question = "合同 CT-2030-014 的负责人是谁？"
        self.assertTrue(RagPipeline.has_answerable_evidence(question, [{
            "title": "合同台账", "raw_text": text, "hits": ["CT-2030-014"], "score": 0.8,
        }]))

    def test_structured_name_match_does_not_accept_a_longer_neighbor_name(self) -> None:
        rows = "供应商：青禾数据；风险等级：黄色；行动项：补齐材料"
        self.assertEqual([], RagPipeline.matched_structured_rows("青禾数据云供应商的风险是什么？", [rows]))
        self.assertEqual([], RagPipeline.matched_structured_rows("新青禾数据供应商的风险是什么？", [rows]))
        self.assertEqual([rows], RagPipeline.matched_structured_rows("青禾数据供应商的风险是什么？", [rows]))
        self.assertEqual(["青禾数据"], RagPipeline.required_retrieval_topics("青禾数据供应商的风险和行动项是什么？"))

    def test_structured_identifier_match_does_not_accept_a_longer_prefix(self) -> None:
        wrong = "资产编号：EQ-C310；负责人：李清；采购价格：800 元"
        right = "资产编号：EQ-C31；负责人：陈晨；状态：封存"
        self.assertEqual([right], RagPipeline.matched_structured_rows("EQ-C31 的状态是什么？", [wrong + "\n" + right]))

    def test_explicit_organisational_role_needs_a_local_assignment(self) -> None:
        question = "公司的法定代表人是谁？"
        self.assertFalse(RagPipeline.requested_answer_fields_are_covered(question, [
            "试点对象是 20 家合作伙伴，不代表全量开放。",
            "法定代表人信息尚未填写。",
        ]))
        self.assertTrue(RagPipeline.requested_answer_fields_are_covered(question, ["公司登记信息：法定代表人为陈晨。 "]))

    def test_named_row_location_and_identity_survive_answer_alignment(self) -> None:
        pipeline = object.__new__(RagPipeline)
        row = "资产编号：EQ-C31；负责人：陈晨；状态：封存；安装位置：三号库"
        question = "EQ-C31 安装在哪里，目前是什么状态？"
        evidence = pipeline.evidence_sentence(question, row, ["EQ-C31", "状态"])
        self.assertEqual("资产编号：EQ-C31；状态：封存；安装位置：三号库", evidence)
        _, sources = RagPipeline.enforce_answer_contract(question, "安装在三号库，状态为封存 [1]。", [{
            "index": 1, "title": "设备记录", "evidence": evidence,
            "fallback_excerpt": row, "matched_terms": ["EQ-C31", "状态"],
        }])
        self.assertEqual(evidence, sources[0]["evidence"])

    def test_absent_field_handling_is_distinct_from_requesting_its_value(self) -> None:
        row = (
            "Agent名称：行动追踪Agent；核心功能：提取行动项和负责人；"
            "适用场景：工作例会；限制：责任人缺失时只标记待确认。"
        )
        chunk = {"title": "工具目录", "raw_text": row, "hits": ["行动追踪Agent"], "score": 0.8}
        for question in (
            "行动追踪Agent在责任人没有写明时应该怎么办？",
            "行动追踪Agent在责任人缺失时的处理",
        ):
            with self.subTest(question=question):
                self.assertIsNone(RagPipeline.structured_field_coverage(question, [row]))
                self.assertTrue(RagPipeline.has_answerable_evidence(question, [chunk]))
                answer, sources = RagPipeline.enforce_answer_contract(question, "只标记待确认 [1]。", [{
                    "index": 1, "title": "工具目录", "evidence": row,
                    "fallback_excerpt": row, "matched_terms": ["行动追踪Agent", "待确认"],
                }])
                self.assertTrue(sources)
                self.assertIn("待确认", answer)
        for question in ("行动追踪Agent的责任人是谁？", "行动追踪Agent由谁负责？"):
            with self.subTest(question=question):
                self.assertFalse(RagPipeline.structured_field_coverage(question, [row]))
                self.assertFalse(RagPipeline.has_answerable_evidence(question, [chunk]))

    def test_identifier_meaning_does_not_request_a_similarly_named_field(self) -> None:
        text = (
            "合同编号：HT-2028-035；合作伙伴：青禾数据；付款节点：验收后 30 天。\n"
            "说明：合同编号末尾数字不是项目风险编号。"
        )
        question = "合同编号 HT-2028-035 末尾的 035 是项目风险编号吗？"
        chunks = [{"title": "合同台账", "raw_text": text, "hits": ["HT-2028-035", "编号"], "score": 0.8}]
        self.assertIsNone(RagPipeline.structured_field_coverage(question, [text]))
        self.assertTrue(RagPipeline.has_answerable_evidence(question, chunks))
        evidence = RagPipeline(None, None).evidence_sentence(question, text, chunks[0]["hits"])
        self.assertIn("不是项目风险编号", evidence)
        self.assertFalse(RagPipeline.structured_field_coverage("HT-2028-035 的风险等级是什么？", [text]))

    def test_complete_agent_fields_precede_flattened_ocr_reconstruction(self) -> None:
        pipeline = object.__new__(RagPipeline)
        row = "编号：3；Agent名称：质量抽查Agent；核心功能：识别质量偏差；适用场景：抽检复核；状态：试用"
        evidence = pipeline.evidence_sentence("质量抽查Agent的核心功能和适用场景是什么？", row, ["质量抽查", "Agent"])
        self.assertEqual("Agent名称：质量抽查Agent；核心功能：识别质量偏差；适用场景：抽检复核", evidence)

    def test_numeric_amount_uses_interval_rule_instead_of_literal_id_gate(self) -> None:
        chunks = [{
            "title": "采购审批规则",
            "raw_text": "20 万元（含）以上，增加法务与财务负责人会签。",
            "hits": ["采购", "金额", "审批", "法务"],
            "score": 0.68,
        }]

        self.assertTrue(RagPipeline._has_answerable_evidence_single(
            "采购金额 25 万元，审批上要额外关注什么？",
            chunks,
            compound_subquery=True,
        ))

    def test_record_identifier_is_the_answer_contract_anchor(self) -> None:
        sources = [{
            "index": 1,
            "title": "合作伙伴合同付款条款对照表",
            "evidence": (
                "合同编号：HT-2026-021；合作伙伴：星港数据；"
                "付款节点：签约后 15 天支付 20%预付款，验收后 45 天付尾款；"
                "预付款比例：20%。"
            ),
            "fallback_excerpt": (
                "合同编号：HT-2026-021；合作伙伴：星港数据；"
                "付款节点：签约后 15 天支付 20%预付款，验收后 45 天付尾款；"
                "预付款比例：20%。"
            ),
            "matched_terms": ["HT-2026-021", "预付款比例", "付款节点", "尾款"],
        }]

        self.assertEqual(
            ["HT-2026-021"],
            RagPipeline.answer_contract_anchor_terms(
                "HT-2026-021 的预付款比例和尾款付款节点是什么？",
                sources,
            ),
        )
        answer, selected = RagPipeline.enforce_answer_contract(
            "HT-2026-021 的预付款比例和尾款付款节点是什么？",
            "预付款比例为 20% [1]；尾款付款节点为验收后 45 天 [1]。",
            sources,
        )

        self.assertIn("20%", answer)
        self.assertIn("验收后 45 天", answer)
        self.assertEqual(1, len(selected))
        self.assertEqual("合作伙伴合同付款条款对照表", selected[0]["title"])
        self.assertIn("20%", selected[0]["evidence"])
        self.assertIn("验收后 45 天", selected[0]["evidence"])
        self.assertIn("HT-2026-021", selected[0]["fallback_excerpt"])

    def test_record_identifier_contract_accepts_requested_text_field(self) -> None:
        sources = [{
            "index": 1,
            "title": "合作伙伴合同付款条款对照表",
            "evidence": (
                "合同编号：HT-2026-018；合作伙伴：远川科技；"
                "争议条款：适用上海仲裁。"
            ),
            "fallback_excerpt": (
                "合同编号：HT-2026-018；合作伙伴：远川科技；"
                "争议条款：适用上海仲裁。"
            ),
            "matched_terms": ["HT-2026-018", "争议"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            "HT-2026-018 的争议解决方式是什么？",
            "适用上海仲裁 [1]。",
            sources,
        )

        self.assertEqual("来源[1] 适用上海仲裁。", answer)
        self.assertEqual(1, len(selected))
        self.assertEqual("合作伙伴合同付款条款对照表", selected[0]["title"])
        self.assertIn("合同编号：HT-2026-018", selected[0]["evidence"])
        self.assertIn("争议条款：适用上海仲裁。", selected[0]["evidence"])
        self.assertIn("HT-2026-018", selected[0]["fallback_excerpt"])

    def test_which_contract_question_keeps_identity_currency_and_schedule_together(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "合同编号：HT-2026-018；合作伙伴：南川数据；付款节点：签约后 15 天；预付款比例：20%；币种：人民币。\n"
            "合同编号：HT-2026-027；合作伙伴：北辰云；付款节点：月度服务确认后 30 天；预付款比例：0%；币种：美元。"
        )

        evidence = pipeline.evidence_sentence(
            "哪份合同是美元、按月度服务确认后付款？",
            text,
            ["美元", "月度服务确认", "付款"],
        )

        self.assertIn("HT-2026-027", evidence)
        self.assertIn("月度服务确认后 30 天", evidence)
        self.assertIn("币种：美元", evidence)
        self.assertNotIn("HT-2026-018", evidence)

    def test_direct_negative_polarity_answer_uses_extractive_evidence(self) -> None:
        sources = [{
            "index": 1,
            "title": "知识库技术记录",
            "evidence": "它不是全库切片级向量索引，因此跨长文的细粒度召回仍有优化空间。",
            "fallback_excerpt": "当前采用笔记级语义向量。它不是全库切片级向量索引。",
            "matched_terms": ["全库", "切片", "向量"],
        }]

        self.assertTrue(RagPipeline.requires_evidence_first_answer(
            "当前知识库是否已经全库按切片建向量？",
            sources,
        ))
        answer, selected = RagPipeline.enforce_answer_contract(
            "当前知识库是否已经全库按切片建向量？",
            "当前采用笔记级语义向量 [1]。",
            sources,
        )

        self.assertIn("不是全库切片级向量索引", answer)
        self.assertEqual(selected[0]["title"], "知识库技术记录")

    def test_structured_fallback_keeps_requested_limitation_field(self) -> None:
        source = {
            "index": 1,
            "title": "Agent 清单",
            "evidence": (
                "Agent名称：会议纪要追踪Agent；核心功能：抽取行动项、责任人和截止日期；"
                "适用场景：项目例会；限制：责任人缺失时只标记待确认。"
            ),
            "matched_terms": ["会议纪要追踪Agent", "责任人缺失"],
            "matched_clauses": ["会议纪要追踪Agent在责任人缺失时的处理"],
        }

        answer = RagPipeline.fallback_answer(
            "会议纪要追踪Agent在责任人缺失时如何处理？",
            [source],
        )

        self.assertIn("责任人缺失时只标记待确认", answer)
        self.assertEqual(len(answer.splitlines()), 1)
        self.assertTrue(all(claim["citations"] == [1] for claim in RagPipeline.answer_claims(answer)))

    def test_compound_question_keeps_both_sources_during_entity_focusing(self) -> None:
        chunks = [
            {"title": "招聘复盘", "raw_text": "结构化反馈缺少责任人时退回补充。"},
            {"title": "Agent 清单", "raw_text": "会议纪要追踪Agent会提醒补齐责任人。"},
        ]

        retained = RagPipeline.retain_focused_chunks(
            "招聘面试的结构化反馈和会议纪要追踪Agent在责任人缺失时的处理有什么不同？",
            chunks,
        )

        self.assertEqual(retained, chunks)

    def test_model_context_starts_with_displayed_evidence(self) -> None:
        pipeline = object.__new__(RagPipeline)
        chunks = [{"title": "制度", "score": 0.8, "reason": "关键词命中", "excerpt": "一段较长的原始内容"}]
        sources = [{"evidence": "审批阈值为 50 万元。", "excerpt": "上下文包含审批阈值为 50 万元，以及后续说明。"}]
        pipeline.chunk_source_url = lambda _: "/source"

        contexts = pipeline.contexts(chunks, sources=sources)

        self.assertTrue(contexts[0]["text"].startswith("审批阈值为 50 万元。"))

    def test_lightweight_reranker_demotes_question_scaffolding_only_match(self) -> None:
        generic = {"title": "古诗练习", "raw_text": "赠汪伦表达了诗人什么样的情感？", "hits": ["什么样的", "有什么"], "rank_score": 10.0}
        entity = {"title": "办公Agent创意", "raw_text": "Agent名称：需求污染检测Agent；核心功能：识别偏离；适用场景：产品研发", "hits": ["需求污染检测", "Agent", "功能", "场景"], "rank_score": 10.0}

        ranked = RagPipeline.rerank_candidates(
            "需求污染检测Agent有什么样的功能，什么场景有用？", [generic, entity]
        )

        self.assertLess(ranked[0]["rank_score"], 10.0)
        self.assertGreater(ranked[1]["rank_score"], 10.0)

    def test_answer_contract_completes_single_source_citations(self) -> None:
        sources = [{"index": 1, "evidence": "核心功能：识别需求偏离；适用场景：产品研发", "matched_terms": ["需求污染检测"]}]

        answer, selected = RagPipeline.enforce_answer_contract(
            "需求污染检测Agent有什么功能？", "功能：识别需求偏离 [1]。\n场景：产品研发。", sources
        )

        self.assertIn("来源[1] 场景：产品研发。", answer)
        self.assertEqual(selected, sources)

    def test_answer_contract_compacts_citation_after_pruning_to_second_source(self) -> None:
        sources = [
            {"index": 1, "title": "发票规则", "evidence": "发票由财务团队复核。", "matched_terms": ["团队"]},
            {
                "index": 2,
                "title": "供应商周报",
                "evidence": "质量团队于 7 月 22 日完成供应商 A 的现场审核。",
                "fallback_excerpt": "质量团队于 7 月 22 日完成供应商 A 的现场审核。",
                "matched_terms": ["供应商 A", "现场审核", "质量团队"],
            },
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            "供应商 A 的现场审核由哪个团队在什么时候完成？",
            "质量团队于 7 月 22 日完成供应商 A 的现场审核 [2]。",
            sources,
        )

        self.assertEqual(answer, "来源[1] 质量团队于 7 月 22 日完成供应商 A 的现场审核。")
        self.assertEqual(1, len(selected))
        self.assertEqual(1, selected[0]["index"])
        self.assertEqual("供应商周报", selected[0]["title"])

    def test_answer_contract_replaces_uncited_multi_source_generation(self) -> None:
        sources = [
            {"index": 1, "title": "地球", "evidence": "地球是距离太阳第三近的行星。", "matched_terms": ["地球"]},
            {"index": 2, "title": "火星", "evidence": "火星有两颗卫星。", "matched_terms": ["火星"]},
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            "地球和火星分别有什么特点？", "地球是第三颗行星。火星有两颗卫星 [2]。", sources
        )

        self.assertIn("来源[1] 地球是距离太阳第三近的行星。", answer)
        self.assertIn("来源[2] 火星有两颗卫星。", answer)
        self.assertNotIn("根据当前笔记", answer)
        self.assertEqual(selected, sources)

    def test_broad_policy_question_keeps_all_relevant_contexts(self) -> None:
        sources = [
            {"index": 1, "title": "Agent 控制", "evidence": "执行发邮件等敏感操作前，必须人工审批确认。", "matched_terms": ["人工审批确认"]},
            {"index": 2, "title": "合同审批", "evidence": "合同金额超过50万或主框架时，由间接采购总监审批。", "matched_terms": ["合同审批", "金额"]},
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            "什么情况下需要人工审批确认", "执行敏感操作前需要人工审批确认 [1]。", sources
        )

        self.assertIn("来源[1] 执行发邮件等敏感操作前，必须人工审批确认。", answer)
        self.assertIn("来源[2] 合同金额超过50万或主框架时，由间接采购总监审批。", answer)
        self.assertEqual(selected, sources)

    def test_broad_policy_summary_uses_separate_source_evidence(self) -> None:
        sources = [
            {"index": 1, "title": "Agent 控制", "evidence": "执行发邮件等敏感操作前，必须人工审批确认。", "matched_terms": ["人工审批确认"]},
            {"index": 2, "title": "合同审批", "evidence": "合同审批需符合CoA要求，依据金额和授权签字人类型进行审批。", "matched_terms": ["合同审批"]},
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            "什么情况下需要人工审批确认",
            "执行敏感操作前需人工审批确认 [1]。合同审批需符合CoA要求，依据金额和授权签字人类型进行审批 [2]。",
            sources,
        )

        self.assertEqual(
            answer,
            "来源[1] 执行发邮件等敏感操作前，必须人工审批确认。\n来源[2] 合同审批需符合CoA要求，依据金额和授权签字人类型进行审批。",
        )
        self.assertEqual(selected, sources)

    def test_po_amount_question_accepts_document_field_aliases(self) -> None:
        sources = [{
            "index": 1,
            "title": "采购审批规则",
            "evidence": "采购合同基本信息：PO编号；合同金额；审批节点。",
        }]

        self.assertFalse(
            RagPipeline.source_lacks_requested_high_risk_field("不同的PO金额是同一层级审批吗？", sources)
        )

    def test_policy_compaction_answers_po_amount_approval_level(self) -> None:
        answer = RagPipeline.compact_policy_evidence(
            "核心风险：合同签署未经恰当审批（不符合CoA要求）；授权签字人不符合要求：间接采购总监（大于50万），Central采购经理（10万到50万）。",
            "不同的PO金额是同一层级审批吗？",
        )

        self.assertEqual(answer, "不同PO金额通常不是同一审批层级，需依据金额阈值和授权签字人类型确定。")

    def test_comparison_evidence_prefers_explicit_statement_in_structured_row(self) -> None:
        pipeline = object.__new__(RagPipeline)
        record = (
            "编号：KRI-06；核心风险：合同签署未经恰当审批；"
            "待确认事项：不同 PO 金额应该经过不同层级审批。例如金额越高，要求的审批人级别越高。"
        )

        evidence = pipeline.evidence_sentence("不同的PO金额是同一层级审批吗？", record, ["PO", "金额"])

        self.assertEqual(evidence, "不同 PO 金额应该经过不同层级审批。例如金额越高，要求的审批人级别越高。")

    def test_structured_field_lookup_does_not_require_definition_sentence(self) -> None:
        self.assertTrue(RagPipeline.is_structured_field_query("PO累积金额"))
        self.assertFalse(RagPipeline.requires_definition_evidence("PO累积金额"))

    def test_amount_boundary_prefers_the_inclusive_rule(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = "5 万元（含）至 20 万元（不含），由部门负责人审批；20 万元（含）以上，增加法务与财务负责人会签。"

        evidence = pipeline.evidence_sentence("采购金额 20 万元整还需要法务会签吗？", text, ["20", "万元", "法务"])

        self.assertIn("法务与财务负责人会签", evidence)

    def test_amount_range_rule_outranks_generic_procurement_overlap(self) -> None:
        question = "采购金额 25 万元，审批上要额外关注什么？"
        rule = {
            "title": "采购审批流",
            "raw_text": "20 万元（含）以上，增加法务与财务负责人会签。",
            "hits": ["万元", "审批", "采购", "金额"],
            "rank_score": 70.0,
            "score": 0.68,
        }
        generic = {
            "title": "供应商风险周报",
            "raw_text": "采购负责人关注付款异常和金额不一致，并复核供应商风险。",
            "hits": ["关注", "审批", "采购", "金额"],
            "rank_score": 72.0,
            "score": 0.72,
        }

        self.assertGreater(
            RagPipeline._source_evidence_priority(question, rule),
            RagPipeline._source_evidence_priority(question, generic),
        )

    def test_temporal_feasibility_does_not_turn_a_recommendation_into_permission(self) -> None:
        sources = [{
            "index": 1,
            "title": "备案准备说明",
            "evidence": "核心建议：在提交申请的当天或前一天添加 DNS A 记录，并立即测试访问。",
            "matched_terms": ["DNS A记录", "提交申请"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            "申请的前一个月添加可以吗？", "可以，提前添加即可 [1]。", sources
        )

        self.assertTrue(RagPipeline.is_temporal_feasibility_question("申请的前一个月添加可以吗？"))
        self.assertIn("当天或前一天", answer)
        self.assertIn("不能据此判断“可以”", answer)
        self.assertEqual(selected, sources)

    def test_answer_contract_recovers_a_missing_joined_approver(self) -> None:
        sources = [{
            "index": 1,
            "title": "采购制度",
            "evidence": "20 万元（含）以上，增加法务与财务负责人会签。",
            "matched_terms": ["采购金额", "法务会签"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            "采购金额 20 万元整还需要法务会签吗？", "需要法务会签 [1]。", sources
        )

        self.assertIn("法务与财务负责人会签", answer)
        self.assertEqual(selected, sources)

    def test_entity_only_evidence_cannot_answer_an_absent_high_risk_field(self) -> None:
        sources = [{
            "title": "供应商履约周报",
            "evidence": "供应商 A 的交付准时率为 88%，当前综合黄色。",
            "matched_terms": ["供应商 A"],
        }]

        answer, selected = RagPipeline.enforce_answer_contract(
            "供应商 A 的合同总金额是多少？", "合同总金额为 100 万元 [1]。", sources
        )

        self.assertEqual(answer, "现有笔记没有足够信息。")
        self.assertEqual(selected, [])

    def test_scalar_gate_accepts_visible_task_values_without_literal_field_labels(self) -> None:
        cases = [
            ("紧急采购能跳过审批吗，补比价材料的期限是多久？", "采购审批流重构方案与灰度复盘",
             "紧急采购不能跳过审批，只可在申请单上标记“紧急”，并要求申请后 2 个工作日内补齐比价依据。",
             ("不能跳过审批", "2 个工作日", "比价")),
            ("夏季会员周首批灰度比例是多少，观察多久？", "营销活动项目例会纪要（夏季会员周）",
             "第一批灰度 10%，观察 24 小时后再决定是否扩量。", ("10%", "24 小时")),
            ("补验收材料的期限是多久？", "交付规则", "申请后 3 个自然日内补交验收资料。", ("3 个自然日", "验收")),
            ("秋季活动第二批试运行比例是多少，观察多久？", "秋季活动",
             "第2批试运行 20%，观察 48 小时后复核。", ("20%", "48 小时")),
            ("夏季会员周首批灰度比例是多少，观察多久？", "夏季会员周活动规则",
             "【渠道安排】第一批灰度比例为 10%，观察 24 小时。", ("10%", "24 小时")),
            ("夏季会员周首批灰度比例是多少，观察多久？", "夏季会员周活动规则",
             "第一批灰度比例为 10%；观察 24 小时后再决定是否扩量。", ("10%", "24 小时")),
            ("夏季会员周首批灰度比例是多少，观察多久？", "夏季会员周活动规则",
             "第一批灰度比例为 10%，观察 24 小时，第二批方案取消。", ("10%", "24 小时")),
            ("紧急采购能跳过审批吗，补比价材料的期限是多久？", "紧急采购期限",
             "紧急采购不能跳过审批，申请后 2 个工作日内补齐比价依据，无需重复审批。",
             ("不能跳过审批", "2 个工作日", "比价")),
        ]
        for question, title, evidence, expected in cases:
            with self.subTest(question=question):
                sources = [{"index": 1, "title": title, "evidence": evidence}]
                self.assertFalse(RagPipeline.source_lacks_requested_high_risk_field(question, sources))
                answer, selected = RagPipeline.enforce_answer_contract(question, evidence + " [1]", sources)
                self.assertEqual(len(selected), 1)
                for value in expected:
                    self.assertIn(value, answer)

    def test_deadline_proof_does_not_borrow_waiting_time_dates_counts_or_another_task(self) -> None:
        question = "紧急采购能跳过审批吗，补比价材料的期限是多久？"
        evidence_cases = [
            "紧急采购平均等待 4.6 个工作日。补齐比价依据后归档。",
            "紧急采购补比价材料的期限待定，申请后 2 个工作日内补齐验收材料。",
            "紧急采购补比价材料的期限待定。常规采购申请后 2 个工作日内补齐比价依据。",
            "紧急采购在 2026-06-24 补齐比价材料。",
            "紧急采购共补齐 2 份比价材料。",
            "紧急采购申请后 2 个工作日内完成审批，之后补齐比价依据。",
            "紧急采购不能跳过审批，普通采购申请后 2 个工作日内补齐比价依据。",
            "紧急采购不要求申请后 2 个工作日内补齐比价依据，具体期限待确认。",
        ]
        for evidence in evidence_cases:
            with self.subTest(evidence=evidence):
                sources = [{"index": 1, "title": "紧急采购期限", "evidence": evidence}]
                self.assertTrue(RagPipeline.source_lacks_requested_high_risk_field(question, sources))
                self.assertEqual(RagPipeline.enforce_answer_contract(question, "期限为 2 个工作日 [1]。", sources),
                                 ("现有笔记没有足够信息。", []))

    def test_ratio_proof_requires_target_batch_task_and_its_duration(self) -> None:
        question = "夏季会员周首批灰度比例是多少，观察多久？"
        evidence_cases = [
            "第一批灰度比例待定，单渠道超预算 5% 必须暂停，观察 24 小时。",
            "第一批灰度比例待定，最终 ROI 为 138%，观察 24 小时。",
            "第一批灰度观察 24 小时。",
            "第二批灰度 10%，观察 24 小时。",
            "第一批抽检 10%，观察 24 小时。",
            "第一批灰度 10%。日志保存 24 小时。",
            "第一批灰度 10%，第二批观察 24 小时。",
            "第一批灰度 10%，观察 24 个用户。",
            "第一批灰度 10%，观察日期是 2026-06-24。",
            "冬季会员周第一批灰度 10%，观察 24 小时。",
            "夏季会员周复盘了冬季会员周第一批灰度 10%，观察 24 小时的配置。",
            "第一批灰度 10%，观察 24 小时的方案已被否决；当前待定。",
        ]
        for evidence in evidence_cases:
            with self.subTest(evidence=evidence):
                sources = [{"index": 1, "title": "夏季会员周灰度比例与观察", "evidence": evidence}]
                self.assertTrue(RagPipeline.source_lacks_requested_high_risk_field(question, sources))
                self.assertEqual(RagPipeline.enforce_answer_contract(question, "灰度 10%，观察 24 小时 [1]。", sources),
                                 ("现有笔记没有足够信息。", []))

    def test_scalar_proof_does_not_take_values_from_a_title_or_hidden_background(self) -> None:
        sources = [{
            "index": 1, "title": "夏季会员周第一批灰度10%，观察24小时",
            "evidence": "第一批灰度比例待定。",
            "fallback_excerpt": "第一批灰度 10%，观察 24 小时。",
        }]
        question = "夏季会员周首批灰度比例是多少，观察多久？"
        self.assertTrue(RagPipeline.source_lacks_requested_high_risk_field(question, sources))
        self.assertEqual(RagPipeline.enforce_answer_contract(question, "灰度10%，观察24小时 [1]。", sources),
                         ("现有笔记没有足够信息。", []))

    def test_scalar_proof_cannot_borrow_a_deadline_from_a_neighbour_record(self) -> None:
        question = "合同 HT-2026-021 补比价材料的期限是多久？"
        sources = [{"index": 1, "title": "合同期限", "evidence":
                    "合同 HT-2026-021 补比价材料的期限待定。合同 HT-2026-022 申请后 2 个工作日内补齐比价材料。"}]
        self.assertTrue(RagPipeline.source_lacks_requested_high_risk_field(question, sources))

    def test_deadline_does_not_convert_workdays_to_calendar_days(self) -> None:
        evidence = "紧急采购申请后 2 个工作日内补齐比价依据。"
        sources = [{"index": 1, "title": "采购规则", "evidence": evidence, "matched_terms": ["紧急采购", "比价"]}]
        question = "紧急采购补比价材料的期限是多久？"
        for fabricated in ("2 个自然日", "2 天"):
            with self.subTest(fabricated=fabricated):
                answer, selected = RagPipeline.enforce_answer_contract(
                    question, f"紧急采购需要在 {fabricated}内补齐比价依据 [1]。", sources,
                )
                self.assertNotIn(fabricated, answer)
                self.assertIn("2 个工作日", answer)
                self.assertEqual(len(selected), 1)
        self.assertTrue(RagPipeline.source_lacks_requested_high_risk_field(
            "紧急采购补比价材料的期限是多少自然日？", sources,
        ))

    def test_scalar_exemption_does_not_cover_unrecognised_fields_or_money(self) -> None:
        self.assertEqual(RagPipeline.explicit_scalar_field_proofs(
            "采购观察时间是多少？", {"title": "采购", "evidence": "观察24小时。"},
        ), {})
        labelled = {"title": "夏季会员周", "evidence": "项目名称：夏季会员周；第一批灰度比例：10%；观察时长：24小时"}
        self.assertEqual(RagPipeline.explicit_scalar_field_proofs(
            "夏季会员周首批灰度比例是多少，观察多久？", labelled,
        ), {})
        self.assertFalse(RagPipeline.source_lacks_requested_high_risk_field(
            "夏季会员周首批灰度比例是多少，观察多久？", [labelled],
        ))
        self.assertFalse(RagPipeline.source_lacks_requested_high_risk_field(
            "夏季会员周首批灰度比例是多少？",
            [{"title": "夏季会员周", "evidence": "第一批灰度 0%。"}],
        ))
        for question, evidence in (
            ("合同 EQ-C31 的合同金额是多少？", "合同编号：EQ-C31；付款比例：0%"),
            ("夏季会员周最终 ROI 是多少？", "第一批灰度 10%，观察 24 小时。"),
            ("夏季会员周首批灰度比例是多少？", "最终 ROI 为 138%。"),
        ):
            with self.subTest(question=question):
                answer, selected = RagPipeline.enforce_answer_contract(
                    question, "数值是 10 [1]。", [{"index": 1, "title": "夏季会员周", "evidence": evidence}],
                )
                self.assertEqual(selected, [])
                self.assertEqual(answer, "现有笔记没有足够信息。")

    def test_scalar_answer_cannot_borrow_a_background_day_unit(self) -> None:
        question = "紧急采购能跳过审批吗，补比价材料的期限是多久？"
        evidence = "紧急采购不能跳过审批，并要求申请后 2 个工作日内补齐比价依据。"
        sources = [{"index": 1, "title": "采购审批", "evidence": evidence,
                    "fallback_excerpt": evidence + "采购预算确认需 2 个自然日。",
                    "matched_terms": ["紧急采购", "比价"]}]
        for unit in ("2 个自然日", "2 天", "2工作日"):
            with self.subTest(unit=unit):
                answer, selected = RagPipeline.enforce_answer_contract(
                    question, f"紧急采购不能跳过审批，须在{unit}内补齐比价依据 [1]。", sources,
                )
                self.assertIn("2 个工作日", answer)
                self.assertIn("不能跳过审批", answer)
                self.assertNotIn("自然日", answer)
                self.assertNotIn("2 天", answer)
                self.assertEqual(len(selected), 1)
                self.assertNotIn("预算", selected[0]["evidence"])

    def test_scalar_answer_uses_the_source_that_proves_the_requested_batch(self) -> None:
        question = "夏季会员周首批灰度比例是多少，观察多久？"
        sources = [
            {"index": 1, "source_id": "target", "title": "夏季会员周",
             "evidence": "第一批灰度 10%，观察 24 小时后再决定是否扩量。"},
            {"index": 2, "source_id": "other-batch", "title": "夏季会员周",
             "evidence": "第二批灰度 10%，观察 24 小时后再决定是否扩量。"},
        ]
        answer, selected = RagPipeline.enforce_answer_contract(
            question, "第一批灰度 10%，观察 24 小时 [2]。", sources,
        )
        self.assertIn("第一批", answer)
        self.assertIn("10%", answer)
        self.assertIn("24 小时", answer)
        self.assertEqual([source["source_id"] for source in selected], ["target"])
        self.assertNotIn("第二批", selected[0]["evidence"])

    def test_requested_answer_field_gate_rejects_related_but_incomplete_evidence(self) -> None:
        cases = [
            ("哪位候选人最终被录用了？", "上季度 3 名候选人的沟通能力评分缺少行为证据。"),
            ("客户数据泄露是否已经发生过？", "疑似泄露应在 1 小时内通知数据保护负责人。"),
            ("订单 P1 故障造成了多少元损失？", "订单 P1 影响超过 30 分钟，09:28 完成人工扩容。"),
            ("AI影子团队Agent目前已有多少个真实客户在用？", "AI影子团队Agent适用于大型项目。"),
            ("HT-2026-021 的签约日期是什么时候？", "合同 HT-2026-021 在签约后 15 天支付预付款。"),
            ("紧急采购一共发生过多少次？", "紧急采购不能跳过审批，事后 2 个工作日内补齐材料。"),
            ("夏季会员周最终 ROI 是多少？", "夏季会员周第一批灰度 10%，观察 24 小时。"),
        ]

        for question, evidence in cases:
            with self.subTest(question=question):
                self.assertFalse(RagPipeline.requested_answer_fields_are_covered(question, [evidence]))

    def test_requested_answer_field_gate_keeps_direct_positive_evidence(self) -> None:
        cases = [
            ("哪位候选人最终被录用了？", "最终录用候选人张岚，入职日期为 8 月 3 日。"),
            ("客户数据泄露是否已经发生过？", "本月已发生 1 起客户数据泄露事件。"),
            ("订单 P1 故障造成了多少元损失？", "订单 P1 故障造成损失 12 万元。"),
            ("订单 P1 故障实际损失多少钱？", "订单 P1 故障造成损失 12 万元。"),
            ("AI影子团队Agent目前已有多少个真实客户在用？", "AI影子团队Agent已有 6 家真实客户正式在用。"),
            ("HT-2026-021 的签约日期是什么时候？", "合同 HT-2026-021 的签约日期为 2026-07-18。"),
            ("紧急采购一共发生过多少次？", "本季度紧急采购共发生 4 次。"),
            ("夏季会员周最终 ROI 是多少？", "夏季会员周最终 ROI 为 138%。"),
        ]

        for question, evidence in cases:
            with self.subTest(question=question):
                self.assertTrue(RagPipeline.requested_answer_fields_are_covered(question, [evidence]))

    def test_requested_answer_field_gate_does_not_join_a_target_with_an_unrelated_count(self) -> None:
        evidence = [
            "紧急采购不能跳过审批，事后 2 个工作日内补齐比价材料。",
            "供应商 B 本月发生 2 起重大质量缺陷，当前状态为红色。",
        ]

        self.assertFalse(RagPipeline.requested_answer_fields_are_covered(
            "紧急采购一共发生过多少次？",
            evidence,
        ))

    def test_precise_money_question_selects_loss_instead_of_other_numbers(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = (
            "订单 P1 故障持续 47 分钟，09:28 完成人工扩容。"
            "财务与业务联合核算确认，本次故障造成损失 12 万元，其中退款 7 万元。"
        )

        evidence = pipeline.evidence_sentence(
            "订单 P1 故障实际损失多少钱？",
            text,
            ["订单 P1", "故障", "损失"],
        )

        self.assertIn("损失 12 万元", evidence)
        self.assertNotIn("持续 47 分钟", evidence)

    def test_imperative_phrase_requires_exact_coverage_but_not_a_definition(self) -> None:
        phrase = "不要回退"

        self.assertEqual(RagPipeline.answer_anchor_terms(phrase), [phrase])
        self.assertFalse(RagPipeline.requires_definition_evidence(phrase))

    def test_deictic_agent_question_requires_clarification_without_history(self) -> None:
        self.assertTrue(RagPipeline.requires_clarification("这个 Agent 适合什么场景？"))
        self.assertFalse(RagPipeline.requires_clarification("AI影子团队Agent适合什么场景？"))

    def test_underspecified_project_contract_and_deadline_require_clarification(self) -> None:
        for question in (
            "这个期限怎么算？",
            "哪个项目要上线？",
            "这份合同怎么付款？",
            "谁来负责这个风险？",
        ):
            with self.subTest(question=question):
                self.assertTrue(RagPipeline.requires_clarification(question))

    def test_named_or_plural_subjects_do_not_trigger_ambiguity_gate(self) -> None:
        for question in (
            "北辰项目什么时候上线？",
            "HT-2026-018 怎么付款？",
            "采购审批期限怎么算？",
            "哪些项目计划在九月上线？",
            "项目复盘里提到的风险是什么？",
        ):
            with self.subTest(question=question):
                self.assertFalse(RagPipeline.requires_clarification(question))

    def test_business_identifier_field_lookup_is_not_treated_as_a_definition(self) -> None:
        question = "HT-2026-021 的预付款比例和尾款付款节点是什么？"

        self.assertEqual(RagPipeline.record_identifier_terms(question), ["HT-2026-021"])
        self.assertEqual(RagPipeline.explicit_identifier_terms(question), ["HT-2026-021"])
        self.assertFalse(RagPipeline.requires_definition_evidence(question))

    def test_exact_structured_row_passes_answerability_gate(self) -> None:
        question = "HT-2026-021 的预付款比例和尾款付款节点是什么？"
        text = (
            "工作表：付款条款\n"
            "合同编号：HT-2026-018；付款节点：验收后 30 天；预付款比例：0%。\n"
            "合同编号：HT-2026-021；付款节点：签约后 15 天支付 20%预付款，"
            "验收后 45 天付尾款；预付款比例：20%；币种：人民币。"
        )
        chunk = {
            "title": "采购合同台账.xlsx",
            "raw_text": text,
            "hits": ["HT-2026-021", "预付款", "付款节点"],
            "score": 1.0,
        }

        self.assertTrue(RagPipeline.has_answerable_evidence(question, [chunk]))

    def test_compound_identifier_clause_does_not_require_a_synthetic_prefix(self) -> None:
        question = "合同 HT-2026-021 的付款安排是否可能触发采购审批中的法务会签？"
        clauses = RagPipeline.compound_query_clauses(question)
        chunks = [
            {
                "title": "采购合同台账.xlsx",
                "raw_text": "合同编号：HT-2026-021；付款节点：签约后 15 天支付 20%预付款；预付款比例：20%。",
                "hits": ["HT-2026-021", "预付款"],
                "score": 1.0,
                "compound_clauses": [clauses[0]],
            },
            {
                "title": "采购审批规则",
                "raw_text": "预付款比例达到 20%时，需要法务会签。",
                "hits": ["采购审批", "法务会签"],
                "score": 1.0,
                "compound_clauses": [clauses[1]],
            },
        ]

        self.assertEqual(RagPipeline.explicit_identifier_terms(clauses[0]), ["HT-2026-021"])
        self.assertTrue(RagPipeline.has_answerable_evidence(question, chunks))

    def test_multi_fact_question_uses_both_visible_evidences(self) -> None:
        sources = [
            {"index": 1, "title": "风险周报", "evidence": "供应商 A 当前综合黄色。", "matched_terms": ["供应商 A"]},
            {"index": 2, "title": "扫描件", "evidence": "第二产地样品只针对关键物料。", "matched_terms": ["第二产地样品"]},
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            "供应商 A 的风险、行动以及样品适用范围分别是什么？", "风险为黄色，行动未明确 [1][2]。", sources
        )

        self.assertIn("当前综合黄色", answer)
        self.assertIn("只针对关键物料", answer)
        self.assertEqual(selected, sources)


if __name__ == "__main__":
    unittest.main()
