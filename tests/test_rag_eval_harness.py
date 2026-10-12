# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from rag_eval.dataset import (
    DatasetValidationError,
    load_dataset,
    normalize_legacy_dataset,
    validate_dataset,
)
from rag_eval.harness import load_api_report
from rag_eval.metrics import (
    aggregate_case_metrics,
    answer_metrics,
    failure_attribution,
    passage_metrics,
    retrieval_metrics,
)
from rag_eval.reporting import render_markdown
from pocket_memory.storage import NoteStore
from scripts.rag_eval_fixture import populate_fixture
from scripts.rag_eval_matching import contains_eval_term
from scripts.evaluate_rag import score_case


class RagEvalDatasetTests(unittest.TestCase):
    def sample(self) -> dict:
        return {
            "dataset": {"name": "sample"},
            "documents": [
                {"title": "Project Review", "content": "Risk one. Risk two."},
                {"title": "Decision Log", "content": "Approved on Monday."},
            ],
            "cases": [
                {
                    "id": "case-001",
                    "question": "What are the risks?",
                    "expected_titles": ["Project Review"],
                    "expected_answer_terms": ["Risk one"],
                    "expected_evidence_terms": ["Risk one"],
                }
            ],
        }

    def test_valid_dataset_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(self.sample()), encoding="utf-8")
            loaded = load_dataset(path)
            self.assertEqual("sample", loaded["dataset"]["name"])

    def test_unknown_expected_title_is_rejected(self):
        payload = self.sample()
        payload["cases"][0]["expected_titles"] = ["Missing"]
        with self.assertRaises(DatasetValidationError):
            validate_dataset(payload)

    def test_empty_expected_titles_require_negative_or_ambiguous_label(self):
        payload = self.sample()
        payload["cases"][0]["expected_titles"] = []
        with self.assertRaises(DatasetValidationError):
            validate_dataset(payload)

    def test_legacy_no_answer_is_loaded_as_refusal_without_editing_fixture(self):
        payload = self.sample()
        payload["cases"][0]["expected_titles"] = []
        payload["cases"][0]["allow_insufficient"] = True
        payload["cases"][0]["scenario"] = "no_answer"
        normalize_legacy_dataset(payload)
        validate_dataset(payload)
        self.assertTrue(payload["cases"][0]["expected_refusal"])

    def test_unlabeled_answerable_legacy_case_is_excluded_from_scoring(self):
        payload = self.sample()
        payload["cases"][0]["expected_titles"] = []
        payload["cases"][0]["allow_insufficient"] = True
        payload["cases"][0]["scenario"] = "time_query"
        normalize_legacy_dataset(payload)
        validate_dataset(payload)
        case = payload["cases"][0]
        self.assertNotIn("expected_refusal", case)
        self.assertTrue(case["evaluation_excluded"])
        self.assertIn("ground truth", case["evaluation_exclusion_reason"])

    def test_legacy_string_history_is_loaded_as_user_message(self):
        payload = self.sample()
        payload["cases"][0]["history"] = ["上一轮用户问题"]
        normalize_legacy_dataset(payload)
        validate_dataset(payload)
        self.assertEqual(
            [{"role": "user", "content": "上一轮用户问题"}],
            payload["cases"][0]["history"],
        )

    def test_explicit_refusal_label_is_not_overridden_by_legacy_field(self):
        payload = self.sample()
        payload["cases"][0]["expected_titles"] = []
        payload["cases"][0]["allow_insufficient"] = True
        payload["cases"][0]["expected_refusal"] = False
        normalize_legacy_dataset(payload)
        self.assertFalse(payload["cases"][0]["expected_refusal"])
        with self.assertRaises(DatasetValidationError):
            validate_dataset(payload)

    def test_excluded_case_requires_reason(self):
        payload = self.sample()
        payload["cases"][0]["expected_titles"] = []
        payload["cases"][0]["evaluation_excluded"] = True
        with self.assertRaises(DatasetValidationError):
            validate_dataset(payload)

    def test_portable_user_feedback_fixture_is_valid(self):
        fixture = TESTS_ROOT / "fixtures" / "rag_regression_user_feedback_portable.json"
        loaded = load_dataset(fixture)
        self.assertEqual(8, len(loaded["cases"]))
        self.assertEqual(7, len(loaded["documents"]))

    def test_location_fixture_is_valid(self):
        fixture = TESTS_ROOT / "fixtures" / "rag_eval_location_coverage.json"
        loaded = load_dataset(fixture)
        self.assertEqual(5, len(loaded["cases"]))
        self.assertTrue(all(item.get("source_type") == "document" for item in loaded["documents"]))

    def test_structured_table_fixture_is_valid(self):
        fixture = TESTS_ROOT / "fixtures" / "rag_eval_structured_tables.json"
        loaded = load_dataset(fixture)
        self.assertEqual(12, len(loaded["cases"]))
        self.assertEqual(6, len(loaded["documents"]))
        self.assertEqual(2, sum(bool(item.get("expected_refusal")) for item in loaded["cases"]))

    def test_invalid_passage_label_is_rejected(self):
        payload = self.sample()
        payload["cases"][0]["expected_passages"] = [{
            "id": "risk",
            "title": "Missing",
            "required_terms": [],
            "locations": [],
            "relevance": 4,
        }]
        with self.assertRaises(DatasetValidationError):
            validate_dataset(payload)


class RagEvalMetricTests(unittest.TestCase):
    def test_visible_evidence_does_not_borrow_an_answer_from_background(self):
        metrics = answer_metrics({"expected_evidence_terms": ["林岚", "在役"]}, {
            "answer": "来源[1] 状态：在役。", "source_details": [
                {"title": "资产", "evidence": "状态：在役", "excerpt": "负责人：林岚；状态：在役"}]
        })
        self.assertEqual(1.0, metrics["evidence_term_recall"])
        self.assertEqual(0.5, metrics["visible_evidence_term_recall"])
        aggregate = aggregate_case_metrics([
            {"final_retrieval": {"recall_at_k": 1.0}, "final_passages": {"recall_at_k": 0.0,
             "location_recall_at_k": 0.0}, "answer_metrics": metrics},
            {"final_retrieval": {"recall_at_k": 1.0}, "final_passages": {"recall_at_k": None}}
        ])
        self.assertEqual(2, aggregate["scored_case_counts"]["final_source"])
        self.assertEqual(1, aggregate["scored_case_counts"]["final_passage"])
        self.assertEqual(1, aggregate["scored_case_counts"]["final_location"])

    def test_numeric_labels_do_not_match_parts_of_other_values(self):
        for text, label in (("20%", "0%"), ("18 万元", "8万元"), ("20482", "2048"),
                            ("0.5%", "5%"), ("1.25", "1.2"), ("INC-A170", "INC-A17")):
            with self.subTest(text=text, label=label):
                self.assertFalse(contains_eval_term(text, label))
        for text, label in (("预付款：20 %；", "20%"), ("损失 8 万元。", "8万元"), ("Count is 8.", "8"),
                            ("INC-A17 的根因", "inc-a17"), ("采购负责人审批", "负责人")):
            with self.subTest(text=text, label=label):
                self.assertTrue(contains_eval_term(text, label))

    def test_correct_percentage_does_not_trigger_zero_percent_forbidden_label(self):
        case = {"id": "number", "question": "预付款比例是多少？", "expected_titles": ["合同"],
                "expected_answer_terms": ["20%"], "forbidden_terms": ["0%"]}
        sources = [{"title": "合同", "evidence": "预付款比例：20%", "excerpt": "其他合同：0%"}]
        answer = "来源[1] 预付款比例：20%。"
        self.assertTrue(answer_metrics(case, {"answer": answer, "source_details": sources})["forbidden_ok"])
        self.assertTrue(score_case(case, sources, answer)["forbidden_ok"])
        sources[0]["evidence"] = "预付款比例：0%"
        self.assertFalse(answer_metrics(case, {"answer": answer, "source_details": sources})["forbidden_ok"])
        self.assertFalse(score_case(case, sources, answer)["forbidden_ok"])

    def test_numeric_mismatch_cannot_pass_answer_or_passage_coverage(self):
        case = {"expected_answer_terms": ["8 万元"], "expected_passages": [
            {"id": "loss", "title": "故障复盘", "required_terms": ["8万元"]}]}
        result = answer_metrics(case, {"answer": "损失18万元", "sources": []})
        self.assertEqual(0.0, result["answer_term_recall"])
        passage = passage_metrics(case, [{"title": "故障复盘", "evidence": "损失18万元"}])
        self.assertEqual(0.0, passage["recall_at_k"])

    def test_location_cell_numbers_are_not_prefix_matches(self):
        case = {"expected_passages": [{"id": "row", "title": "Inventory",
                "required_terms": ["approved"], "locations": ["A3:C3"]}]}
        result = passage_metrics(case, [{"title": "Inventory", "evidence": "approved",
                                       "location": "Sheet 1 A3:C30"}])
        self.assertEqual(1.0, result["recall_at_k"])
        self.assertEqual(0.0, result["location_recall_at_k"])
        result = passage_metrics(case, [{"title": "Inventory", "evidence": "approved",
                                       "location": "Sheet 1 A3:C3"}])
        self.assertEqual(1.0, result["location_recall_at_k"])

    def test_separate_numeric_fields_are_not_concatenated(self):
        self.assertTrue(contains_eval_term("8\n8", "8"))
        self.assertFalse(contains_eval_term("8\n8", "88"))
        case = {"expected_passages": [{"id": "count", "title": "counts", "required_terms": ["8"]}]}
        result = passage_metrics(case, [{"title": "counts", "evidence": "8", "excerpt": "8"}])
        self.assertEqual(1.0, result["recall_at_k"])

    def test_numbered_source_titles_do_not_match_longer_identifiers(self):
        result = retrieval_metrics({"expected_titles": ["合同 HT-2026-021"]},
                                   [{"title": "合同 HT-2026-0210"}])
        self.assertEqual(0.0, result["recall_at_k"])

    def test_retrieval_metrics_capture_rank_and_missing_source(self):
        case = {"expected_titles": ["Alpha", "Beta"]}
        result = retrieval_metrics(
            case,
            [{"title": "Alpha"}, {"title": "Noise"}, {"title": "Alpha"}],
            k=3,
        )
        self.assertEqual(0.5, result["recall_at_k"])
        self.assertEqual(1.0, result["mrr"])
        self.assertEqual(["Beta"], result["missing_titles"])
        self.assertAlmostEqual(1 / 3, result["duplicate_source_ratio"], places=6)
        self.assertLessEqual(result["ndcg_at_k"], 1.0)

    def test_short_source_names_do_not_match_by_substring(self):
        result = retrieval_metrics(
            {"expected_titles": ["AI"]},
            [{"title": "RAIL policy"}],
            k=1,
        )
        self.assertEqual(0.0, result["recall_at_k"])

    def test_passage_and_location_metrics_are_independent(self):
        case = {
            "expected_passages": [{
                "id": "limit",
                "title": "Manual",
                "required_terms": ["maximum vibration", "4.5 mm/s"],
                "locations": ["page 3"],
                "relevance": 3,
            }]
        }
        result = passage_metrics(
            case,
            [
                {"title": "Manual", "evidence": "vibration overview", "location": "page 1"},
                {"title": "Manual", "evidence": "maximum vibration is 4.5 mm/s", "location": "page 2"},
                {"title": "Manual", "evidence": "maximum vibration is 4.5 mm/s", "location": "page 3"},
            ],
            k=3,
        )
        self.assertEqual(1.0, result["recall_at_k"])
        self.assertEqual(0.5, result["mrr"])
        self.assertEqual(1.0, result["location_recall_at_k"])
        self.assertEqual(["limit"], result["matched_locations"])

    def test_passage_miss_is_attributed_to_location_stage(self):
        failures = failure_attribution(
            {"expected_titles": ["A"]},
            {"recall_at_k": 1.0},
            None,
            None,
            0.1,
            retrieval_passages={"recall_at_k": 0.0, "location_recall_at_k": 0.0},
        )
        self.assertEqual(["LOCATION"], failures)

    def test_refusal_requires_no_sources_and_refusal_text(self):
        case = {
            "expected_refusal": True,
            "expected_answer_terms": ["没有足够信息"],
        }
        correct = answer_metrics(case, {"answer": "现有资料没有足够信息。", "sources": []})
        wrong = answer_metrics(
            case,
            {"answer": "来源[1] 这是相关内容。", "source_details": [{"title": "Noise"}]},
        )
        self.assertTrue(correct["refusal_correct"])
        self.assertFalse(wrong["refusal_correct"])

    def test_citation_indexes_must_exist(self):
        metrics = answer_metrics(
            {"expected_answer_terms": []},
            {
                "answer": "来源[2] 结论。",
                "source_details": [{"title": "Only", "evidence": "结论"}],
            },
        )
        self.assertEqual(0.0, metrics["citation_validity"])

    def test_claim_metrics_separate_valid_citation_from_evidence_support(self):
        supported = answer_metrics(
            {"question": "设备振动上限是多少？", "expected_answer_terms": []},
            {
                "answer": "来源[1] 设备振动上限为 4.5 mm/s。",
                "source_details": [{
                    "index": 1,
                    "title": "设备手册",
                    "evidence": "设备振动上限为 4.5 mm/s。",
                }],
            },
        )
        unsupported = answer_metrics(
            {"question": "设备振动上限是多少？", "expected_answer_terms": []},
            {
                "answer": "来源[1] 设备振动上限为 9.0 mm/s。",
                "source_details": [{
                    "index": 1,
                    "title": "设备手册",
                    "evidence": "设备振动上限为 4.5 mm/s。",
                }],
            },
        )

        self.assertEqual(1.0, supported["claim_citation_coverage"])
        self.assertEqual(1.0, supported["claim_support_proxy"])
        self.assertEqual(1.0, unsupported["claim_citation_coverage"])
        self.assertEqual(0.0, unsupported["claim_support_proxy"])
        self.assertEqual(1, unsupported["unsupported_claim_count"])

    def test_answer_terms_ignore_layout_whitespace(self):
        metrics = answer_metrics(
            {"expected_answer_terms": ["6 家"], "forbidden_terms": ["9 家"]},
            {"answer": "来源[1] 6家", "source_details": [{"title": "试点", "evidence": "6 家在用"}]},
        )

        self.assertEqual(1.0, metrics["answer_term_recall"])
        self.assertTrue(metrics["forbidden_ok"])

    def test_forbidden_terms_ignore_context_but_not_focused_evidence(self):
        context_only = answer_metrics(
            {"forbidden_terms": ["干扰实体"]},
            {
                "answer": "来源[1] 目标实体的结论。",
                "source_details": [{
                    "index": 1,
                    "evidence": "目标实体的结论。",
                    "excerpt": "目标实体的结论。附近还有干扰实体的其他记录。",
                }],
            },
        )
        focused = answer_metrics(
            {"forbidden_terms": ["干扰实体"]},
            {
                "answer": "来源[1] 目标实体的结论。",
                "source_details": [{
                    "index": 1,
                    "evidence": "干扰实体的其他记录。",
                    "excerpt": "目标实体的结论。",
                }],
            },
        )

        self.assertTrue(context_only["forbidden_ok"])
        self.assertFalse(focused["forbidden_ok"])

    def test_failure_attribution_distinguishes_recall_and_generate(self):
        failures = failure_attribution(
            {"expected_titles": ["A"]},
            {"recall_at_k": 0.0},
            {"recall_at_k": 0.0},
            {"answer_term_recall": 0.0, "citation_presence": 1.0, "citation_validity": 1.0},
            1.0,
        )
        self.assertEqual(["RECALL", "GENERATE"], failures)

    def test_retrieval_only_does_not_fail_unexecuted_answer_stage(self):
        self.assertEqual(
            [],
            failure_attribution(
                {"expected_refusal": True},
                {},
                None,
                None,
                0.1,
            ),
        )

    def test_aggregate_does_not_invent_human_scores(self):
        aggregate = aggregate_case_metrics(
            [
                {
                    "retrieval": {"recall_at_k": 1.0},
                    "final_retrieval": {"recall_at_k": 1.0},
                    "answer_metrics": {"answer_term_recall": 1.0},
                    "elapsed_seconds": 2.0,
                    "failure_attribution": [],
                }
            ]
        )
        self.assertIsNone(aggregate["answer"]["answer_correctness"])
        self.assertIsNone(aggregate["answer"]["faithfulness"])

    def test_aggregate_includes_claim_support_without_inventing_human_faithfulness(self):
        aggregate = aggregate_case_metrics(
            [{
                "answer_metrics": {
                    "claim_citation_coverage": 1.0,
                    "claim_support_proxy": 0.5,
                    "unsupported_claim_count": 1,
                },
                "failure_attribution": ["VERIFY"],
            }]
        )

        self.assertEqual(1.0, aggregate["answer"]["claim_citation_coverage"])
        self.assertEqual(0.5, aggregate["answer"]["claim_support_proxy"])
        self.assertEqual(1, aggregate["answer"]["unsupported_claim_count"])
        self.assertIsNone(aggregate["answer"]["faithfulness"])

    def test_aggregate_keeps_passage_and_location_metrics_separate(self):
        aggregate = aggregate_case_metrics(
            [{
                "retrieval_passages": {
                    "recall_at_k": 1.0,
                    "location_recall_at_k": 0.5,
                },
                "failure_attribution": [],
            }]
        )
        self.assertEqual(1.0, aggregate["retrieval_passages"]["recall_at_k"])
        self.assertEqual(0.5, aggregate["retrieval_passages"]["location_recall_at_k"])

    def test_aggregate_excludes_unlabeled_cases_but_reports_their_count(self):
        aggregate = aggregate_case_metrics(
            [
                {
                    "retrieval": {"recall_at_k": 1.0},
                    "answer_metrics": {"refusal_correct": True},
                    "elapsed_seconds": 1.0,
                    "failure_attribution": [],
                },
                {
                    "evaluation_excluded": True,
                    "retrieval": {"recall_at_k": 0.0},
                    "answer_metrics": {"refusal_correct": False},
                    "elapsed_seconds": 99.0,
                    "failure_attribution": ["RECALL", "PERF"],
                },
            ]
        )

        self.assertEqual(2, aggregate["case_count_total"])
        self.assertEqual(1, aggregate["case_count_evaluated"])
        self.assertEqual(1, aggregate["case_count_excluded"])
        self.assertEqual(1.0, aggregate["retrieval"]["recall_at_k"])
        self.assertEqual(1.0, aggregate["latency_seconds"]["p95"])
        self.assertEqual({}, aggregate["failure_attribution"])

    def test_markdown_labels_human_review(self):
        report = {
            "mode": "retrieval",
            "fingerprint": {"git": {}, "fixture": {}},
            "aggregate": aggregate_case_metrics([]),
            "by_group": {},
            "cases": [],
        }
        rendered = render_markdown(report)
        self.assertIn("human review required", rendered)


class RagEvalArtifactTests(unittest.TestCase):
    def test_fixture_loader_creates_locatable_document_chunks(self):
        class RecordingEmbedding:
            def __init__(self):
                self.notes = []
                self.documents = []

            def _process(self, note_id):
                self.notes.append(note_id)

            def _process_document(self, document_id):
                self.documents.append(document_id)

        fixture = {
            "documents": [{
                "source_type": "document",
                "title": "Manual",
                "original_name": "Manual.pdf",
                "content": "fixture",
                "chunks": [{"text": "limit 4.5", "location": "page 3", "page": 3}],
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            store = NoteStore(Path(directory) / "data")
            embedding = RecordingEmbedding()
            try:
                populate_fixture(store, embedding, fixture)
                documents = store.list_documents()
                chunks = store.list_document_chunks(documents[0].id)
            finally:
                store.close()
        self.assertEqual(["Manual"], [document.title for document in documents])
        self.assertEqual("page 3", chunks[0].location)
        self.assertEqual([documents[0].id], embedding.documents)

    def test_frozen_api_report_can_be_filtered(self):
        payload = {
            "average_score": 0.5,
            "cases": [{"id": "one"}, {"id": "two"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_api_report(path, {"two"})
        self.assertEqual(["two"], [case["id"] for case in loaded["cases"]])

    def test_frozen_api_report_preserves_structured_diagnostics(self):
        payload = {
            "average_score": 1.0,
            "cases": [{
                "id": "one",
                "timing": {"total_seconds": 1.2},
                "diagnostics": {"schema_version": 1, "intent": {"type": "qa"}},
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_api_report(path)

        self.assertEqual(1, loaded["cases"][0]["diagnostics"]["schema_version"])
        self.assertEqual(1.2, loaded["cases"][0]["timing"]["total_seconds"])


if __name__ == "__main__":
    unittest.main()
