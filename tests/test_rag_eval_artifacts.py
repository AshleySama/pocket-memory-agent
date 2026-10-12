# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.rag_eval.dataset import file_sha256
from tests.rag_eval.harness import load_api_report, run_evaluation


class FrozenApiArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture.json"
        self.artifact = self.root / "api.json"
        self.cases = [
            {"id": "one", "question": "Question one?", "expected_titles": ["Source"]},
            {"id": "two", "question": "Question two?", "expected_titles": ["Source"]},
        ]
        self.fixture.write_text(json.dumps({
            "documents": [{"title": "Source", "content": "Fixture evidence."}],
            "cases": self.cases,
        }), encoding="utf-8")

    def write_artifact(self, **changes) -> None:
        payload = {
            "fixture": self.fixture.name,
            "cases": [{"id": case["id"], "question": case["question"], "answer": ""} for case in self.cases],
        }
        payload.update(changes)
        self.artifact.write_text(json.dumps(payload), encoding="utf-8")

    def load(self, **kwargs) -> dict:
        return load_api_report(
            self.artifact, fixture_path=self.fixture, expected_cases=self.cases, **kwargs
        )

    def test_missing_case_is_rejected_before_retrieval_or_scoring(self) -> None:
        self.write_artifact(cases=[{"id": "one"}])
        with patch("tests.rag_eval.harness.evaluate_retrieval") as retrieve:
            with self.assertRaisesRegex(ValueError, "missing required case ids: two"):
                run_evaluation(self.root, self.fixture, mode="both", api_report_path=self.artifact)
        retrieve.assert_not_called()

    def test_duplicate_ids_are_rejected_even_outside_selected_cases(self) -> None:
        self.write_artifact(cases=[{"id": "one"}, {"id": "two"}, {"id": "two"}])
        with self.assertRaisesRegex(ValueError, "duplicate case id: two"):
            load_api_report(self.artifact, {"one"})

    def test_requested_missing_ids_are_rejected_without_fixture_context(self) -> None:
        self.write_artifact(cases=[{"id": "one"}])
        with self.assertRaisesRegex(ValueError, "missing required case ids: two"):
            load_api_report(self.artifact, {"one", "two"})

    def test_unknown_case_ids_are_rejected(self) -> None:
        self.write_artifact(cases=[{"id": "one"}, {"id": "two"}, {"id": "foreign"}])
        with self.assertRaisesRegex(ValueError, "unknown case ids: foreign"):
            self.load()

    def test_conflicting_fixture_name_hash_and_question_are_rejected(self) -> None:
        for changes, message in [
            ({"fixture": "other.json"}, "fixture name"),
            ({"fixture_sha256": "0" * 64}, "SHA-256"),
            ({"fingerprint": {"fixture": {"sha256": "0" * 64}}}, "SHA-256"),
            ({"fingerprint": {"fixture": {"path": "old/other.json"}}}, "fixture name"),
            ({"cases": [{"id": "one", "question": "Different question?"}, {"id": "two"}]}, "question conflicts"),
        ]:
            with self.subTest(changes=changes):
                self.write_artifact(**changes)
                with self.assertRaisesRegex(ValueError, message):
                    self.load()

    def test_same_fixture_complete_legacy_artifact_is_explicitly_unverified(self) -> None:
        self.write_artifact(cases=[{"id": "one"}, {"id": "two"}])
        loaded = self.load()
        validation = loaded["artifact_validation"]
        self.assertEqual(validation["status"], "legacy_unverified")
        self.assertFalse(validation["fixture_sha256_verified"])
        self.assertTrue(validation["case_ids_verified"])
        self.assertEqual(validation["question_count_verified"], 0)

    def test_verified_fixture_hash_does_not_claim_original_runtime_provenance(self) -> None:
        self.write_artifact(fixture_sha256=file_sha256(self.fixture).lower())
        loaded = self.load()
        validation = loaded["artifact_validation"]
        self.assertEqual(validation["status"], "fixture_verified")
        self.assertTrue(validation["fixture_sha256_verified"])
        self.assertEqual(validation["question_count_verified"], 2)
        self.assertFalse(validation["runtime_source_provenance_verified"])

    def test_complete_artifact_supports_selected_fixture_subset(self) -> None:
        self.write_artifact()
        loaded = load_api_report(
            self.artifact, {"two"}, fixture_path=self.fixture, expected_cases=[self.cases[1]]
        )
        self.assertEqual([case["id"] for case in loaded["cases"]], ["two"])

    def test_report_propagates_unverified_provenance(self) -> None:
        self.write_artifact()
        with patch("tests.rag_eval.harness.environment_fingerprint", return_value={}):
            report = run_evaluation(self.root, self.fixture, mode="api", api_report_path=self.artifact)
        self.assertEqual(report["api_validation"]["status"], "legacy_unverified")
        self.assertEqual(report["aggregate"]["scored_case_counts"]["final_source"], 2)


if __name__ == "__main__":
    unittest.main()
