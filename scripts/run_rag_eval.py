# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Run the reproducible Pocket Memory RAG evaluation harness."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
for path in (ROOT, TESTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rag_eval.harness import run_evaluation
from rag_eval.reporting import write_reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Pocket Memory RAG retrieval and API evaluation.")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--mode", choices=("retrieval", "api", "both"), default="both")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--case", action="append", help="Run one case id; may be repeated")
    parser.add_argument(
        "--api-report",
        type=Path,
        help="Reuse a frozen evaluate_rag_api JSON artifact instead of rerunning the model",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")

    report = run_evaluation(
        ROOT,
        args.fixture,
        mode=args.mode,
        case_ids=set(args.case or []),
        top_k=args.top_k,
        command=[sys.executable, *sys.argv],
        api_report_path=args.api_report,
    )
    write_reports(report, args.output_json, args.output_md)
    summary = {
        "json": str(args.output_json),
        "markdown": str(args.output_md),
        "aggregate": report["aggregate"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
