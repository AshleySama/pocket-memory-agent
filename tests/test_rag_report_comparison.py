# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.compare_rag_reports import compare_reports, render_markdown, write_comparison
from scripts.rag_eval_matching import METRIC_VERSION
from rag_eval.dataset import file_sha256


class RagReportComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture_path = self.root / "fixture.json"
        self.before_path = self.root / "before.json"
        self.after_path = self.root / "after.json"
        self.fixture = {
            "documents": [{"title": "Contract", "content": "Prepayment: 20%."}],
            "cases": [{
                "id": "exact-001", "question": "What is the prepayment?", "priority": "P0",
                "expected_titles": ["Contract"], "expected_answer_terms": ["20%"],
                "expected_evidence_terms": ["20%"], "forbidden_terms": ["0%"],
                "expected_passages": [{"id": "payment", "title": "Contract",
                                       "required_terms": ["20%"], "locations": ["page 2"]}],
            }],
        }
        self._write(self.fixture_path, self.fixture)
        source = {"title": "Contract", "evidence": "20%", "excerpt": "20%", "location": "page 2"}
        self.before = {
            "mode": "both", "top_k": 5,
            "fingerprint": {
                "fixture": {"sha256": file_sha256(self.fixture_path)},
                "models": [{"path": "model.gguf", "sha256": "A" * 64}],
                "runtime": {"python": "3.11.8", "processor": "CPU", "physical_memory_bytes": 16},
                "dependencies": {"numpy": "2.4.6"},
            },
            "cases": [{
                "id": "exact-001", "question": "What is the prepayment?",
                "raw_candidates": [source], "final_sources": [source],
                "answer": "来源[1] 20%", "elapsed_seconds": 1.0,
                # Intentionally wrong historical proxy: must be rescored.
                "answer_metrics": {"forbidden_ok": False, "answer_term_recall": 0.0,
                                   "claim_count": 1, "claim_citation_coverage": 1.0,
                                   "claim_support_proxy": 0.75, "unsupported_claim_count": 1},
            }],
        }
        self.after = copy.deepcopy(self.before)

    @staticmethod
    def _write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def compare(self):
        self._write(self.before_path, self.before)
        self._write(self.after_path, self.after)
        return compare_reports(self.fixture_path, self.before_path, self.after_path)

    def test_same_fixture_rescores_both_sides_and_preserves_claim_metrics(self):
        report = self.compare()
        self.assertEqual(METRIC_VERSION, report["metric_version"])
        self.assertEqual([], report["summary"]["regressed_case_ids"])
        self.assertEqual([], report["summary"]["improved_case_ids"])
        for side in ("before", "after"):
            metrics = report["cases"][0][side]["answer_metrics"]
            self.assertEqual(1.0, metrics["answer_term_recall"])
            self.assertTrue(metrics["forbidden_ok"])
            self.assertEqual(0.75, metrics["claim_support_proxy"])
            self.assertEqual(1, metrics["unsupported_claim_count"])
        self.assertIn("not human factual accuracy", render_markdown(report))

    def test_numeric_zero_percent_is_not_twenty_percent_and_regression_is_visible(self):
        self.after["cases"][0]["answer"] = "来源[1] 0%"
        self.after["cases"][0]["final_sources"][0]["evidence"] = "0%"
        self.after["cases"][0]["final_sources"][0]["excerpt"] = "0%"
        report = self.compare()
        case = report["cases"][0]
        self.assertTrue(case["before"]["answer_metrics"]["forbidden_ok"])
        self.assertFalse(case["after"]["answer_metrics"]["forbidden_ok"])
        self.assertEqual(0.0, case["after"]["answer_metrics"]["answer_term_recall"])
        self.assertEqual(0.0, case["after"]["final_passages"]["recall_at_k"])
        self.assertEqual(["exact-001"], report["summary"]["p0_regressed_case_ids"])
        self.assertTrue(all(change["direction"] == "decrease" for change in case["regressions"]))

    def test_expected_zero_percent_does_not_match_twenty_percent(self):
        self.fixture["cases"][0]["expected_answer_terms"] = ["0%"]
        self.fixture["cases"][0]["expected_passages"][0]["required_terms"] = ["0%"]
        self._write(self.fixture_path, self.fixture)
        for report in (self.before, self.after):
            report["fingerprint"]["fixture"]["sha256"] = file_sha256(self.fixture_path)
        report = self.compare()
        self.assertEqual(0.0, report["cases"][0]["before"]["answer_metrics"]["answer_term_recall"])
        self.assertEqual(0.0, report["cases"][0]["before"]["retrieval_passages"]["recall_at_k"])

    def test_visible_passage_does_not_get_credit_from_hidden_excerpt(self):
        self.before["cases"][0]["final_sources"][0]["evidence"] = "Contract record"
        report = self.compare()
        before = report["cases"][0]["before"]
        after = report["cases"][0]["after"]
        self.assertEqual(1.0, before["final_passages"]["recall_at_k"])
        self.assertEqual(0.0, before["final_visible_passages"]["recall_at_k"])
        self.assertEqual(1.0, after["final_visible_passages"]["recall_at_k"])
        self.assertEqual(["exact-001"], report["summary"]["improved_case_ids"])

    def test_wrong_fixture_hash_rejected_even_with_identical_case_ids(self):
        self.after["fingerprint"]["fixture"]["sha256"] = "F" * 64
        with self.assertRaisesRegex(ValueError, "fixture SHA-256 mismatch"):
            self.compare()

    def test_duplicate_missing_extra_and_changed_question_rejected(self):
        for mutation, message in (
            (lambda r: r["cases"].append(copy.deepcopy(r["cases"][0])), "duplicate case ID"),
            (lambda r: r["cases"].clear(), "case ID mismatch"),
            (lambda r: r["cases"][0].update(id="other-001"), "case ID mismatch"),
            (lambda r: r["cases"][0].update(question="Unrelated question"), "question mismatch"),
        ):
            with self.subTest(message=message):
                self.after = copy.deepcopy(self.before)
                mutation(self.after)
                with self.assertRaisesRegex(ValueError, message):
                    self.compare()

    def test_model_runtime_hardware_and_dependency_mismatches_rejected(self):
        for section, key, value in (
            ("runtime", "processor", "other CPU"),
            ("runtime", "physical_memory_bytes", 32),
            ("runtime", "python", "3.12"),
            ("dependencies", "numpy", "other-version"),
        ):
            with self.subTest(key=key):
                self.after = copy.deepcopy(self.before)
                self.after["fingerprint"][section][key] = value
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    self.compare()
        self.after = copy.deepcopy(self.before)
        self.after["fingerprint"]["models"][0]["sha256"] = "B" * 64
        with self.assertRaisesRegex(ValueError, "model SHA-256 mismatch"):
            self.compare()

    def test_missing_environment_warns_and_unequal_top_k_fails(self):
        self.after["fingerprint"].pop("runtime")
        report = self.compare()
        self.assertTrue(any("runtime" in warning for warning in report["warnings"]))
        self.after["top_k"] = 3
        with self.assertRaisesRegex(ValueError, "top_k"):
            self.compare()

    def test_original_reports_and_fixture_unchanged_including_output_collision(self):
        report = self.compare()
        paths = (self.fixture_path, self.before_path, self.after_path)
        original = {path: path.read_bytes() for path in paths}
        write_comparison(report, self.root / "comparison.json", self.root / "comparison.md")
        for protected in paths:
            with self.subTest(path=protected):
                with self.assertRaisesRegex(ValueError, "must not overwrite"):
                    write_comparison(report, protected, self.root / "another.md")
        for path, content in original.items():
            self.assertEqual(content, path.read_bytes())
        self.assertEqual(file_sha256(self.before_path), report["artifacts"]["before"]["sha256"])


if __name__ == "__main__":
    unittest.main()
