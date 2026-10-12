# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Rescore frozen RAG artifacts under one metric version, without model calls."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from rag_eval.dataset import file_sha256, load_dataset
from rag_eval.metrics import (
    CLARIFICATION_PATTERNS, REFUSAL_PATTERNS, aggregate_case_metrics,
    cited_indexes, failure_attribution, passage_metrics, retrieval_metrics, term_recall,
)
from scripts.rag_eval_matching import METRIC_VERSION, contains_eval_term

FROZEN_CLAIM_KEYS = (
    "claim_count", "claim_citation_coverage", "claim_support_proxy", "unsupported_claim_count",
)
RANKING_KEYS = (
    "precision_at_k", "precision_at_returned", "recall_at_k", "hit_at_k", "mrr",
    "ndcg_at_k", "duplicate_source_ratio", "location_precision_at_k",
    "location_precision_at_returned", "location_recall_at_k",
)
ANSWER_KEYS = (
    "answer_term_recall", "evidence_term_recall", "visible_evidence_term_recall",
    "forbidden_ok", "citation_presence", "citation_validity", "refusal_correct",
    "clarification_correct", "claim_citation_coverage", "claim_support_proxy",
    "unsupported_claim_count",
)
NOTICE = (
    "Expected-term, forbidden-term and passage matching are literal heuristic proxies, "
    "not human factual accuracy or semantic faithfulness. Both sides are rescored with "
    "the same current metric code. Claim metrics are preserved from each original "
    "artifact: their runtime validator may differ, so they are not rescored or proof "
    "of equivalent semantic validation. Latency is an observed sample, not significance."
)


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"report root must be an object: {path}")
    return value


def _index_report(report: dict, fixture: dict, fixture_hash: str, label: str) -> dict:
    actual_hash = report.get("fingerprint", {}).get("fixture", {}).get("sha256")
    if not isinstance(actual_hash, str) or actual_hash.upper() != fixture_hash:
        raise ValueError(f"{label}: fixture SHA-256 mismatch or missing fingerprint")
    if report.get("mode") != "both":
        raise ValueError(f"{label}: requires a combined harness report (mode=both)")
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"{label}: cases must be a list")
    index = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError(f"{label}: invalid case row")
        case_id = case["id"]
        if case_id in index:
            raise ValueError(f"{label}: duplicate case ID {case_id}")
        index[case_id] = case
        for field in ("raw_candidates", "final_sources"):
            if not isinstance(case.get(field), list) or not all(
                isinstance(item, dict) for item in case[field]
            ):
                raise ValueError(f"{label}: {case_id} missing/invalid {field}")
        if not isinstance(case.get("answer"), str):
            raise ValueError(f"{label}: {case_id} missing/invalid answer")
    fixture_index = {case["id"]: case for case in fixture["cases"]}
    if set(index) != set(fixture_index):
        raise ValueError(
            f"{label}: case ID mismatch; missing={sorted(set(fixture_index) - set(index))}, "
            f"extra={sorted(set(index) - set(fixture_index))}"
        )
    for case_id, case in index.items():
        if case.get("question") != fixture_index[case_id]["question"]:
            raise ValueError(f"{label}: question mismatch for {case_id}")
    return index


def _check_environment(before: dict, after: dict) -> list[str]:
    warnings = []
    left, right = before.get("fingerprint", {}), after.get("fingerprint", {})
    for section in ("runtime", "dependencies"):
        left_values, right_values = left.get(section) or {}, right.get(section) or {}
        for key in sorted(set(left_values) | set(right_values)):
            a, b = left_values.get(key), right_values.get(key)
            if a is not None and b is not None and a != b:
                raise ValueError(f"{section} mismatch for {key}: {a!r} != {b!r}")
            if a is None or b is None:
                warnings.append(f"{section}.{key} unavailable on one side; not verified")
        if not left_values or not right_values:
            warnings.append(f"{section} fingerprint missing on one side; not verified")
    hashes = []
    for fingerprint in (left, right):
        models = fingerprint.get("models") or []
        hashes.append(sorted(str(item["sha256"]).upper() for item in models if item.get("sha256")))
    if hashes[0] and hashes[1] and hashes[0] != hashes[1]:
        raise ValueError("model SHA-256 mismatch")
    if not hashes[0] or not hashes[1]:
        warnings.append("model hashes missing on one side; model equivalence not verified")
    return warnings


def _answer_metrics(case: dict, stored: dict) -> dict:
    """Mirror lightweight metric rules without invoking the product claim validator."""
    answer, sources = stored["answer"], stored["final_sources"]
    focused = "\n".join(str(source.get("evidence", "")) for source in sources)
    evidence = "\n".join(
        str(source.get("evidence", "")) + "\n" + str(source.get("excerpt", ""))
        for source in sources
    )
    result = {}
    for key, labels, text in (
        ("answer_term_recall", "expected_answer_terms", answer),
        ("evidence_term_recall", "expected_evidence_terms", evidence),
        ("visible_evidence_term_recall", "expected_evidence_terms", focused),
    ):
        score = term_recall(case.get(labels, []), text)
        result[key] = round(score, 6) if score is not None else None
    indexes = cited_indexes(answer)
    result.update({
        "forbidden_ok": not any(
            contains_eval_term(answer, term) or contains_eval_term(focused, term)
            for term in case.get("forbidden_terms", [])
        ),
        "citation_presence": 1.0 if not sources or indexes else 0.0,
        "citation_validity": (
            round(sum(1 <= index <= len(sources) for index in indexes) / len(indexes), 6)
            if indexes else (1.0 if not sources else 0.0)
        ),
        "refusal_correct": (
            bool(not sources and any(pattern in answer for pattern in REFUSAL_PATTERNS))
            if case.get("expected_refusal") else None
        ),
        "clarification_correct": (
            bool(not sources and any(pattern in answer for pattern in CLARIFICATION_PATTERNS))
            if case.get("expected_clarification") else None
        ),
    })
    for key in FROZEN_CLAIM_KEYS:
        result[key] = stored.get("answer_metrics", {}).get(key)
    return result


def _rescore(fixture: dict, index: dict, top_k: int) -> list[dict]:
    rows = []
    for case in fixture["cases"]:
        stored = index[case["id"]]
        raw, final = stored["raw_candidates"], stored["final_sources"]
        visible = [
            {key: item.get(key) for key in ("title", "evidence", "location", "evidence_locations")}
            for item in final
        ]
        row = {
            "id": case["id"], "question": case["question"],
            "priority": case.get("priority", ""),
            "evaluation_excluded": bool(case.get("evaluation_excluded")),
            "evaluation_exclusion_reason": case.get("evaluation_exclusion_reason", ""),
            "retrieval": retrieval_metrics(case, raw, top_k),
            "retrieval_passages": passage_metrics(case, raw, top_k),
            "final_retrieval": retrieval_metrics(case, final, top_k),
            "final_passages": passage_metrics(case, final, top_k),
            "final_visible_passages": passage_metrics(case, visible, top_k),
            "answer_metrics": _answer_metrics(case, stored),
            "elapsed_seconds": stored.get("elapsed_seconds"),
        }
        row["failure_attribution"] = [] if row["evaluation_excluded"] else failure_attribution(
            case, row["retrieval"], row["final_retrieval"], row["answer_metrics"],
            row["elapsed_seconds"], retrieval_passages=row["retrieval_passages"],
            final_passages=row["final_passages"],
        )
        rows.append(row)
    return rows


def _changes(before: dict, after: dict) -> list[dict]:
    changes = []
    for section in ("retrieval", "retrieval_passages", "final_retrieval", "final_passages",
                    "final_visible_passages", "answer_metrics"):
        keys = ANSWER_KEYS if section == "answer_metrics" else RANKING_KEYS
        for key in keys:
            a, b = before[section].get(key), after[section].get(key)
            if a is None or b is None or a == b:
                continue
            delta = round(float(b) - float(a), 6)
            lower_better = key in {"duplicate_source_ratio", "unsupported_claim_count"}
            changes.append({
                "metric": f"{section}.{key}", "before": a, "after": b, "delta": delta,
                "direction": "increase" if delta > 0 else "decrease",
                "outcome": "regression" if ((delta > 0) == lower_better) else "improvement",
                "provenance": "frozen_original_claim_metric" if key in FROZEN_CLAIM_KEYS else "rescored",
            })
    return changes


def compare_reports(fixture_path: Path, before_path: Path, after_path: Path) -> dict:
    fixture = load_dataset(fixture_path)
    fixture_hash = file_sha256(fixture_path)
    before, after = _read(before_path), _read(after_path)
    left = _index_report(before, fixture, fixture_hash, "before")
    right = _index_report(after, fixture, fixture_hash, "after")
    top_k = before.get("top_k")
    if type(top_k) is not int or top_k < 1 or type(after.get("top_k")) is not int or after["top_k"] != top_k:
        raise ValueError("top_k must be the same positive integer in both reports")
    warnings = _check_environment(before, after)
    before_rows, after_rows = _rescore(fixture, left, top_k), _rescore(fixture, right, top_k)
    aggregates = {}
    claim_metric_counts = {}
    for name, rows in (("before", before_rows), ("after", after_rows)):
        evaluated_rows = [row for row in rows if not row["evaluation_excluded"]]
        aggregates[name] = aggregate_case_metrics(rows)
        claim_metric_counts[name] = {
            key: sum(row["answer_metrics"].get(key) is not None for row in evaluated_rows)
            for key in FROZEN_CLAIM_KEYS
        }
        for key, count in claim_metric_counts[name].items():
            if count < len(evaluated_rows):
                warnings.append(f"{name}: frozen {key} available in {count}/{len(evaluated_rows)} evaluated cases")
        if claim_metric_counts[name]["unsupported_claim_count"] < len(evaluated_rows):
            # A missing frozen count is unknown, never evidence of zero claims.
            aggregates[name]["answer"]["unsupported_claim_count"] = None
    cases = []
    for a, b in zip(before_rows, after_rows):
        changes = _changes(a, b)
        cases.append({
            "id": a["id"], "question": a["question"], "priority": a["priority"],
            "evaluation_excluded": a["evaluation_excluded"],
            "before": a, "after": b, "changes": changes,
            "regressions": [change for change in changes if change["outcome"] == "regression"],
            "improvements": [change for change in changes if change["outcome"] == "improvement"],
        })
    evaluated = [case for case in cases if not case["evaluation_excluded"]]
    return {
        "schema_version": 1, "comparison_scoring_version": f"artifact-comparison-v1/{METRIC_VERSION}",
        "metric_version": METRIC_VERSION, "notice": NOTICE, "warnings": warnings,
        "scoring_source_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): file_sha256(path)
            for path in (Path(__file__).resolve(), ROOT / "tests/rag_eval/metrics.py",
                         ROOT / "tests/rag_eval/dataset.py", ROOT / "scripts/rag_eval_matching.py")
        },
        "fixture": {"path": str(fixture_path.resolve()), "sha256": fixture_hash},
        "artifacts": {
            name: {"path": str(path.resolve()), "sha256": file_sha256(path),
                   "original_metric_version": report.get("metric_version", "unversioned")}
            for name, path, report in (("before", before_path, before), ("after", after_path, after))
        },
        "top_k": top_k,
        "summary": {
            "case_count": len(cases), "evaluated_case_count": len(evaluated),
            "regressed_case_ids": [case["id"] for case in evaluated if case["regressions"]],
            "improved_case_ids": [case["id"] for case in evaluated if case["improvements"]],
            "p0_regressed_case_ids": [case["id"] for case in evaluated if case["regressions"] and case["priority"] == "P0"],
            "frozen_claim_metric_counts": claim_metric_counts,
            "before": aggregates["before"],
            "after": aggregates["after"],
        },
        "cases": cases,
    }


def render_markdown(report: dict) -> str:
    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["# Frozen RAG Report Comparison", "", report["notice"], "",
             f"- Scoring: `{report['comparison_scoring_version']}`",
             f"- Fixture SHA-256: `{report['fixture']['sha256']}`"]
    for name, artifact in report["artifacts"].items():
        lines.append(f"- {name.title()} artifact: `{artifact['path']}`; SHA-256 `{artifact['sha256']}`")
    lines.extend(["", "## Before / After", "", "| Metric | Before | After | Delta |", "|---|---:|---:|---:|"])
    summary = report["summary"]
    for section in ("retrieval", "retrieval_passages", "final_sources", "final_passages",
                    "final_visible_passages", "answer", "latency_seconds"):
        for key, before in summary["before"][section].items():
            after = summary["after"][section].get(key)
            delta = round(after - before, 6) if before is not None and after is not None else "N/A"
            label = f"{section}.{key}"
            if key in FROZEN_CLAIM_KEYS:
                label += " (frozen)"
            lines.append(f"| {label} | {before if before is not None else 'N/A'} | {after if after is not None else 'N/A'} | {delta} |")
    lines.extend(["", f"Evaluated cases: {summary['evaluated_case_count']}/{summary['case_count']}. "
                  f"Cases with decreases that indicate regression: {len(summary['regressed_case_ids'])}; "
                  f"cases with improvements: {len(summary['improved_case_ids'])}. "
                  "A case may appear in both groups. Lower duplicate ratio and unsupported-claim count are better."])
    for title, field in (("Per-case regressions", "regressions"), ("Per-case improvements", "improvements")):
        lines.extend(["", f"## {title}", "", "| Case | Priority | Question | Metric | Before | After | Delta | Provenance |", "|---|---|---|---|---:|---:|---:|---|"])
        rows = 0
        for case in report["cases"]:
            if case["evaluation_excluded"]:
                continue
            for change in case[field]:
                rows += 1
                values = [case["id"], case["priority"], case["question"], change["metric"],
                          change["before"], change["after"], change["delta"], change["provenance"]]
                lines.append("| " + " | ".join(cell(value) for value in values) + " |")
        if not rows:
            lines.append("| None | | | | | | | |")
    if report["warnings"]:
        lines.extend(["", "## Unverified fingerprint fields", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"


def write_comparison(report: dict, json_path: Path, markdown_path: Path) -> None:
    protected = [Path(report["fixture"]["path"]), *(Path(row["path"]) for row in report["artifacts"].values())]
    outputs = [json_path.resolve(), markdown_path.resolve()]
    for output in outputs:
        for source in protected:
            if output == source.resolve() or (output.exists() and output.samefile(source)):
                raise ValueError("output must not overwrite an original report or fixture")
    if outputs[0] == outputs[1] or (all(path.exists() for path in outputs) and outputs[0].samefile(outputs[1])):
        raise ValueError("JSON and Markdown outputs must be different files")
    for output in outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
    outputs[0].write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outputs[1].write_text(render_markdown(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "before", "after", "output-json", "output-md"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = compare_reports(args.fixture, args.before, args.after)
        write_comparison(report, args.output_json, args.output_md)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"json": str(args.output_json), "markdown": str(args.output_md),
                      "regressed_case_ids": report["summary"]["regressed_case_ids"],
                      "improved_case_ids": report["summary"]["improved_case_ids"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
