# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path


def render_markdown(report: dict) -> str:
    aggregate = report.get("aggregate", {})
    retrieval = aggregate.get("retrieval", {})
    retrieval_passages = aggregate.get("retrieval_passages", {})
    final_sources = aggregate.get("final_sources", {})
    final_passages = aggregate.get("final_passages", {})
    answer = aggregate.get("answer", {})
    latency = aggregate.get("latency_seconds", {})
    fingerprint = report.get("fingerprint", {})
    git = fingerprint.get("git", {})
    fixture = fingerprint.get("fixture", {})

    lines = [
        "# Pocket Memory RAG Evaluation Report",
        "",
        f"- Generated: `{fingerprint.get('generated_at', '')}`",
        f"- Git: `{git.get('head', '')}` ({'dirty' if git.get('dirty') else 'clean'})",
        f"- Fixture: `{fixture.get('path', '')}`",
        f"- Fixture SHA-256: `{fixture.get('sha256', '')}`",
        f"- Mode: `{report.get('mode', '')}`",
        f"- Metric version: `{report.get('metric_version', 'legacy-substring')}`",
        f"- Cases: `{aggregate.get('case_count', 0)}`",
        f"- Scored case counts (unlabelled cases are not passes): `{aggregate.get('scored_case_counts', {})}`",
        "",
        "## Metric Classification",
        "",
        "- Source, evidence-passage and source-location ranking: deterministic against frozen labels.",
        "- Claim citation coverage: deterministic citation-to-visible-source validation.",
        "- Claim support proxy: heuristic lexical/exact-marker mapping, not semantic entailment.",
        "- Expected-term and forbidden-term checks: heuristic.",
        "- Answer correctness and faithfulness: human review required; this report does not invent them.",
        "",
        "## Aggregate",
        "",
        "| Metric | Value | Type |",
        "|---|---:|---|",
        _row("Raw Precision@K", retrieval.get("precision_at_k"), "deterministic source-title proxy"),
        _row("Raw Recall@K", retrieval.get("recall_at_k"), "deterministic"),
        _row("Raw Hit@K", retrieval.get("hit_at_k"), "deterministic"),
        _row("Raw MRR", retrieval.get("mrr"), "deterministic"),
        _row("Raw nDCG@K", retrieval.get("ndcg_at_k"), "deterministic source-title proxy"),
        _row("Raw passage Precision@K", retrieval_passages.get("precision_at_k"), "deterministic"),
        _row("Raw passage Recall@K", retrieval_passages.get("recall_at_k"), "deterministic"),
        _row("Raw passage MRR", retrieval_passages.get("mrr"), "deterministic"),
        _row("Raw location Precision@K", retrieval_passages.get("location_precision_at_k"), "deterministic"),
        _row("Raw location Recall@K", retrieval_passages.get("location_recall_at_k"), "deterministic"),
        _row("Final source Recall@K", final_sources.get("recall_at_k"), "deterministic"),
        _row("Final passage Recall@K", final_passages.get("recall_at_k"), "deterministic"),
        _row("Final location Recall@K", final_passages.get("location_recall_at_k"), "deterministic"),
        _row("Visible evidence passage Recall@K", aggregate.get("final_visible_passages", {}).get("recall_at_k"), "deterministic; excludes background excerpts"),
        _row("Answer expected-term recall", answer.get("answer_term_recall"), "heuristic"),
        _row("Evidence expected-term recall", answer.get("evidence_term_recall"), "heuristic"),
        _row("Visible evidence expected-term recall", answer.get("visible_evidence_term_recall"), "heuristic; excludes background excerpts"),
        _row("Citation presence", answer.get("citation_presence"), "deterministic"),
        _row("Citation validity", answer.get("citation_validity"), "deterministic"),
        _row("Claim citation coverage", answer.get("claim_citation_coverage"), "deterministic"),
        _row("Claim support proxy", answer.get("claim_support_proxy"), "heuristic lexical"),
        _row("Unsupported claim count", answer.get("unsupported_claim_count"), "heuristic lexical"),
        _row("Refusal accuracy", answer.get("refusal_accuracy"), "deterministic + pattern"),
        _row("Clarification accuracy", answer.get("clarification_accuracy"), "deterministic + pattern"),
        _row("Answer correctness", answer.get("answer_correctness"), "human required"),
        _row("Faithfulness", answer.get("faithfulness"), "human required"),
        _row("Latency P50 (s)", latency.get("p50"), "deterministic"),
        _row("Latency P95 (s)", latency.get("p95"), "deterministic"),
        "",
        "## Failure Attribution",
        "",
        "| Stage | Cases |",
        "|---|---:|",
    ]
    failures = aggregate.get("failure_attribution", {})
    if failures:
        lines.extend(f"| {name} | {count} |" for name, count in failures.items())
    else:
        lines.append("| None | 0 |")

    lines.extend(["", "## Group Summary", ""])
    lines.append("| Group | Cases | Source Recall@K | Passage Recall@K | Location Recall@K | Answer terms | Claim support | Refusal | P95 (s) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, group in report.get("by_group", {}).items():
        lines.append(
            "| {name} | {count} | {recall} | {passage} | {location} | {answer} | {claims} | {refusal} | {p95} |".format(
                name=name,
                count=group.get("case_count", 0),
                recall=_format(group.get("final_sources" if report.get("mode") == "api" else "retrieval", {}).get("recall_at_k")),
                passage=_format(group.get("final_passages" if report.get("mode") == "api" else "retrieval_passages", {}).get("recall_at_k")),
                location=_format(group.get("final_passages" if report.get("mode") == "api" else "retrieval_passages", {}).get("location_recall_at_k")),
                answer=_format(group.get("answer", {}).get("answer_term_recall")),
                claims=_format(group.get("answer", {}).get("claim_support_proxy")),
                refusal=_format(group.get("answer", {}).get("refusal_accuracy")),
                p95=_format(group.get("latency_seconds", {}).get("p95")),
            )
        )

    lines.extend(["", "## Cases Requiring Review", ""])
    review = [case for case in report.get("cases", []) if case.get("failure_attribution")]
    if not review:
        lines.append("No automatically attributed failures.")
    else:
        lines.append("| Case | Question | Stages | Missing sources |")
        lines.append("|---|---|---|---|")
        for case in review:
            missing = case.get("retrieval", {}).get("missing_titles", [])
            lines.append(
                f"| {case.get('id', '')} | {_escape(case.get('question', ''))} | "
                f"{', '.join(case.get('failure_attribution', []))} | {_escape(', '.join(missing))} |"
            )

    lines.extend(
        [
            "",
            "## Release Interpretation",
            "",
            "This report is a regression instrument, not a claim of production accuracy. "
            "Release still requires frozen holdout results, target-hardware performance, "
            "and human review of factual correctness and source faithfulness.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(report: dict, json_path: Path, markdown_path: Path) -> None:
    import json

    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


def _row(label: str, value, evaluation_type: str) -> str:
    return f"| {label} | {_format(value)} | {evaluation_type} |"


def _format(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
