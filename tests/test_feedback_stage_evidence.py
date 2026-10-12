# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace
import unittest

from pocket_memory.rag.pipeline import RagPipeline


class StageEvidenceFeedbackTests(unittest.TestCase):
    question = "AI 在制造业的应用有哪些阶段"
    paragraph = (
        "AI 在制造业的应用应从一个明确、可验证的小场景起步。"
        "研发设计阶段，可以整理历史需求、试验记录和设计规范；"
        "生产阶段，可以分析设备报警、工艺参数和质量检查记录；"
        "运营管理阶段，可以集中管理会议纪要、项目风险和行动项。"
    )
    labels = ["研发设计阶段", "生产阶段", "运营管理阶段"]

    def chunk(self, source_id: int, title: str, text: str, *, parent_id=None, score=20):
        note = SimpleNamespace(id=parent_id or source_id, title=title)
        return {
            "note": note, "source_id": source_id, "source_group": f"note-{source_id}",
            "title": title, "raw_text": text, "excerpt": text,
            "location": "页签2·正文 1" if parent_id else "正文 1",
            "hits": RagPipeline.matched_terms(text, RagPipeline.query_terms(self.question)),
            "score": 0.7, "rank_score": score, "lexical_score": 8,
            "reason": "正文命中", "kind": "content",
        }

    def test_named_stage_group_keeps_every_source_visible_item(self):
        pipeline = RagPipeline(None, None)
        evidence = pipeline.enumerated_evidence(self.question, self.paragraph)
        self.assertEqual(evidence, self.paragraph)
        self.assertEqual(pipeline.enumeration_item_count(self.question, evidence), 3)
        self.assertEqual(pipeline.stage_enumeration_block(self.question, evidence)[1], self.labels)

    def test_direct_stage_tab_displaces_readiness_neighbour_and_keeps_locator(self):
        pipeline = RagPipeline(None, None)
        readiness = self.chunk(20, "AI 赋能制造业项目", "制造业 AI 项目启动前，先把资料、场景、责任和验证写清楚。", score=100)
        direct = self.chunk(11, "页签2", self.paragraph, parent_id=10, score=80)
        sources = pipeline.source_payloads(self.question, [readiness, direct])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], 10)
        self.assertEqual(sources[0]["url"], "/api/notes/10?tab_id=11")
        self.assertEqual(sources[0]["location"], "页签2·正文 1")
        self.assertEqual(sources[0]["evidence"], self.paragraph)
        self.assertEqual(sources[0]["index"], 1)

    def test_requested_stage_group_survives_generic_title_filter_after_rerank(self):
        # Production query aliases can boost the generic titled note above a
        # directly answering child whose title is only "页签2".
        readiness = self.chunk(20, "制造业AI落地检查单", "制造业 AI 项目启动前先整理资料。", score=99)
        direct = self.chunk(11, "页签2", self.paragraph, parent_id=10, score=77)
        direct["lexical_score"] = 16
        ranked = RagPipeline.rerank_candidates(self.question, [readiness, direct])
        ranked.sort(key=lambda c:c["rank_score"], reverse=True)
        self.assertGreater(direct["rank_score"], readiness["rank_score"])
        retained = RagPipeline.filter_weak_chunks(ranked, question=self.question)
        self.assertIn(direct, retained)
        self.assertEqual(RagPipeline.requested_field_evidence_bonus(
            "AI在制造业的应用阶段分别需要多少预算？", self.paragraph), 0)

    def test_contract_recovers_all_three_stages_from_incomplete_answer(self):
        pipeline = RagPipeline(None, None)
        sources = pipeline.source_payloads(self.question, [self.chunk(11, "页签2", self.paragraph, parent_id=10)])
        incomplete = "AI 在制造业的应用包括研发设计阶段。[1]"
        self.assertTrue(pipeline.answer_omits_enumerated_evidence(self.question, incomplete, sources))
        answer, selected = pipeline.enforce_answer_contract(self.question, incomplete, sources)
        for label in self.labels:
            self.assertIn(label, answer)
            self.assertIn(label, selected[0]["evidence"])
        self.assertEqual(selected[0]["url"], "/api/notes/10?tab_id=11")

    def test_question_variant_and_another_subject_use_source_labels(self):
        variant = "关于AI在制造业的应用，具体有哪些阶段？"
        self.assertEqual(RagPipeline.enumerated_evidence(variant, self.paragraph), self.paragraph)
        other = "软件交付采用逐步确认方式。需求阶段，确认范围；测试阶段，检查验收条件。"
        self.assertEqual(RagPipeline.stage_enumeration_block("软件交付有哪些阶段？", other)[1], ["需求阶段", "测试阶段"])

    def test_single_stage_attribute_negation_and_missing_budget_do_not_trigger(self):
        for question in (
            "AI在制造业的应用在生产阶段需要哪些输入资料？",
            "AI在制造业的应用阶段分别需要多少预算？",
            "AI在制造业的应用有哪些阶段，各需多少预算？",
            "AI在制造业的应用不包括哪些阶段？",
            "AI在制造业的应用不是研发设计阶段吗？",
        ):
            with self.subTest(question=question):
                self.assertEqual(RagPipeline.stage_enumeration_block(question, self.paragraph), ("", []))

    def test_unrelated_subject_and_single_label_cannot_establish_group(self):
        unrelated = self.paragraph.replace("AI 在制造业的应用", "AI 在建筑业的应用")
        self.assertEqual(RagPipeline.stage_enumeration_block(self.question, unrelated), ("", []))
        one = "AI 在制造业的应用应明确边界。研发设计阶段，可以检索资料。"
        self.assertEqual(RagPipeline.stage_enumeration_block(self.question, one), ("", []))

    def test_labels_from_separate_sources_are_not_combined(self):
        first = "AI 在制造业的应用分批探索。研发设计阶段，整理设计规范。"
        second = "AI 在制造业的应用分批探索。生产阶段，整理设备报警。"
        for text in (first, second):
            self.assertEqual(RagPipeline.stage_enumeration_block(self.question, text), ("", []))

    def test_explicitly_rejected_stage_scheme_is_not_promoted_to_complete_list(self):
        rejected = "AI 在制造业的应用并不采用这套划分。研发设计阶段，整理设计规范；生产阶段，整理设备报警。"
        self.assertEqual(RagPipeline.stage_enumeration_block(self.question, rejected), ("", []))


if __name__ == "__main__":
    unittest.main()
