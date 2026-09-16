from __future__ import annotations

import unittest

from scripts.release_quality_gate import gate, source_locations


class ReleaseQualityGateTests(unittest.TestCase):
    def test_grouped_source_keeps_every_evidence_location(self) -> None:
        self.assertEqual(
            source_locations({"evidence_locations": ["页签1·切片 2", "页签1·切片 4"]}),
            {"页签1·切片 2", "页签1·切片 4"},
        )

    def test_gate_requires_location_gold_standard_and_open_check(self) -> None:
        report = {
            "dataset": {"source": "deidentified-real-notes", "target_hardware": "test"},
            "cases": [{
                "id": "case-1",
                "scenario": "precise_parameter",
                "expected_sources": ["note-1"],
                "expected_locations": ["页签1·切片 2"],
                "result": {
                    "latency_seconds": 1.0,
                    "retrieval_recall_at_5": 1.0,
                    "retrieval_precision_at_5": 1.0,
                    "retrieval_location_recall_at_5": 1.0,
                    "retrieval_location_precision_at_5": 1.0,
                },
                "review": {
                    "factually_correct": True,
                    "faithful": True,
                    "citation_supported": True,
                    "correct_refusal": True,
                    "high_risk_error": False,
                    "opened_correct_location": True,
                },
            }],
        }

        outcome = gate(report)

        self.assertEqual(outcome["metrics"]["location_recall_at_5"], 1.0)
        self.assertEqual(outcome["metrics"]["location_open_accuracy"], 1.0)
        self.assertIn("样本不足：1/100", outcome["failures"])
