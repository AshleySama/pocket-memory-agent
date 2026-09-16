from __future__ import annotations

import unittest

from pocket_memory.rag.pipeline import RagPipeline
from pocket_memory.rag.intent import IntentRouter


class RagRankingTests(unittest.TestCase):
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
                "score": 0.6,
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
        self.assertEqual(selected, sources)

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
            "项目演示页面无法打开会影响验收。\n"
            "💡 核心建议：在演示前一天完成访问配置，并立即测试确认页面能够正常打开。"
        )

        evidence = pipeline.evidence_sentence("演示页面打不开怎么办", text, ["演示", "页面", "打不开"])

        self.assertEqual(
            evidence,
            "在演示前一天完成访问配置，并立即测试确认页面能够正常打开。",
        )

    def test_evidence_keeps_the_explicit_negative_boundary(self) -> None:
        pipeline = object.__new__(RagPipeline)
        text = "72 小时内形成处置记录。这里的 72 小时是处置记录时限，不是对外通知承诺。"

        evidence = pipeline.evidence_sentence("72 小时具体指什么，不是对外通知承诺吗？", text, ["72", "小时", "对外通知"])

        self.assertIn("形成处置记录", evidence)
        self.assertIn("不是对外通知承诺", evidence)

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
        self.assertEqual(selected, sources)

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

    def test_boundary_fallback_states_the_negative_conclusion_and_keeps_one_source(self) -> None:
        sources = [
            {"index": 1, "title": "项目台账", "evidence": "9 月 8 日开始试点，不代表全量开放。", "matched_terms": ["北辰", "全量"]},
            {"index": 2, "title": "同名合同", "evidence": "北辰云按月服务确认。", "matched_terms": ["北辰"]},
        ]

        answer, selected = RagPipeline.enforce_answer_contract(
            "北辰 9 月 8 日是全量上线吗？", "9 月 8 日开始试点 [1]。", sources
        )

        self.assertTrue(answer.startswith("不是。"))
        self.assertEqual(selected, [sources[0]])

    def test_weak_generic_candidates_are_not_answerable_evidence(self) -> None:
        generic = [{"title": "古诗练习", "raw_text": "赠汪伦表达什么情感？", "hits": ["什么", "功能"], "score": 0.42}]
        focused = [{"title": "办公 Agent", "raw_text": "Agent名称：需求污染检测Agent；核心功能：识别需求偏离", "hits": ["需求污染检测"], "score": 0.42}]

        self.assertFalse(RagPipeline.has_answerable_evidence("需求污染检测Agent有什么功能？", generic))
        self.assertTrue(RagPipeline.has_answerable_evidence("需求污染检测Agent有什么功能？", focused))

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

    def test_imperative_phrase_requires_exact_coverage_but_not_a_definition(self) -> None:
        phrase = "不要回退"

        self.assertEqual(RagPipeline.answer_anchor_terms(phrase), [phrase])
        self.assertFalse(RagPipeline.requires_definition_evidence(phrase))

    def test_deictic_agent_question_requires_clarification_without_history(self) -> None:
        self.assertTrue(RagPipeline.requires_clarification("这个 Agent 适合什么场景？"))
        self.assertFalse(RagPipeline.requires_clarification("AI影子团队Agent适合什么场景？"))

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
