"""Summarize a constructed API benchmark without mistaking it for release proof."""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    result = json.loads(args.result.read_text(encoding="utf-8"))
    definitions = {case["id"]: case for case in fixture["cases"]}
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in result["cases"]:
        groups[definitions[item["id"]]["scenario"]].append(item)
    rows = []
    for scenario, items in sorted(groups.items()):
        rows.append({"scenario": scenario, "count": len(items), "average_score": mean([i["score"] for i in items]),
                     "retrieval": mean([i["retrieval_score"] for i in items]), "answer": mean([i["answer_score"] for i in items])})
    citations = [bool(re.search(r"\[\d+\]", item["answer"])) for item in result["cases"] if item["sources"]]
    output = {
        "label": "constructed regression benchmark; not release evidence",
        "case_count": len(result["cases"]),
        "overall_proxy_score": result["average_score"],
        "latency_seconds": result["latency_seconds"],
        "citation_marker_rate": mean([float(value) for value in citations]),
        "scenario_breakdown": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
