# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Iterable

from scripts.rag_eval_matching import contains_eval_term


REFUSAL_PATTERNS = (
    "没有足够信息",
    "没有找到",
    "无法基于",
    "无法确认",
    "资料不足",
    "证据不足",
    "现有资料未提及",
    "现有笔记未提及",
)

CLARIFICATION_PATTERNS = (
    "请明确",
    "请说明",
    "请指定",
    "具体是",
    "哪一个",
    "哪个项目",
    "哪份合同",
)


def mean(values: Iterable[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def percentile(values: Iterable[float | int], ratio: float) -> float | None:
    clean = sorted(float(value) for value in values)
    if not clean:
        return None
    index = max(0, min(len(clean) - 1, math.ceil(ratio * len(clean)) - 1))
    return clean[index]


def _normalise(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _matching_expected(title: str, expected_titles: list[str]) -> str | None:
    actual = _normalise(title)
    for expected in expected_titles:
        target = _normalise(expected)
        if target and (
            target == actual
            or (len(target) >= 4 and contains_eval_term(actual, target))
            or (len(actual) >= 4 and contains_eval_term(target, actual))
        ):
            return expected
    return None


def retrieval_metrics(case: dict, ranked_items: list[dict], k: int = 5) -> dict:
    """Score ranked chunks or sources against source-title ground truth.

    Current public fixtures label source titles, not individual chunk ids. Chunk
    precision is therefore a deterministic source-title proxy and is named as
    such in the report instead of being presented as human relevance judgment.
    """

    expected_titles = [str(title) for title in case.get("expected_titles", [])]
    if not expected_titles:
        return {
            "eligible": False,
            "ground_truth_level": "none",
            "precision_at_k": None,
            "precision_at_returned": None,
            "recall_at_k": None,
            "hit_at_k": None,
            "mrr": None,
            "ndcg_at_k": None,
            "duplicate_source_ratio": None,
            "returned": min(k, len(ranked_items)),
        }

    top = ranked_items[:k]
    labels: list[int] = []
    unique_source_labels: list[int] = []
    matched_expected: set[str] = set()
    titles: list[str] = []
    for item in top:
        title = str(item.get("title", ""))
        titles.append(title)
        matched = _matching_expected(title, expected_titles)
        labels.append(1 if matched else 0)
        unique_source_labels.append(1 if matched and matched not in matched_expected else 0)
        if matched:
            matched_expected.add(matched)

    relevant = sum(labels)
    precision_at_k = relevant / k if k else 0.0
    precision_at_returned = relevant / len(top) if top else 0.0
    recall_at_k = len(matched_expected) / len(expected_titles)
    first_rank = next((index + 1 for index, label in enumerate(labels) if label), None)
    mrr = 1.0 / first_rank if first_rank else 0.0
    dcg = sum(
        label / math.log2(index + 2)
        for index, label in enumerate(unique_source_labels)
    )
    # Source-title labels cannot tell how many chunks from one source are truly
    # relevant. The conservative ideal contains one item per expected source.
    ideal_count = min(k, len(expected_titles))
    ideal_dcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    ndcg = dcg / ideal_dcg if ideal_dcg else 0.0
    unique_titles = {_normalise(title) for title in titles if title}
    duplicate_ratio = 1.0 - (len(unique_titles) / len(titles)) if titles else 0.0

    return {
        "eligible": True,
        "ground_truth_level": "source_title",
        "precision_at_k": round(precision_at_k, 6),
        "precision_at_returned": round(precision_at_returned, 6),
        "recall_at_k": round(recall_at_k, 6),
        "hit_at_k": int(bool(matched_expected)),
        "mrr": round(mrr, 6),
        "ndcg_at_k": round(ndcg, 6),
        "duplicate_source_ratio": round(duplicate_ratio, 6),
        "matched_titles": sorted(matched_expected),
        "missing_titles": [title for title in expected_titles if title not in matched_expected],
        "returned": len(top),
    }


def passage_metrics(case: dict, ranked_items: list[dict], k: int = 5) -> dict:
    """Score exact evidence passages and their source locations.

    Passage labels are independent of database chunk ids so the same frozen
    fixture can be rebuilt on every run. A passage matches only when its source
    title matches and every labelled evidence term is present in that ranked
    item. Location metrics are reported separately because finding the right
    sentence and opening the right page/worksheet are different product duties.
    """

    expected = [dict(passage) for passage in case.get("expected_passages", [])]
    if not expected:
        return {
            "eligible": False,
            "ground_truth_level": "none",
            "precision_at_k": None,
            "precision_at_returned": None,
            "recall_at_k": None,
            "hit_at_k": None,
            "mrr": None,
            "ndcg_at_k": None,
            "location_eligible": False,
            "location_precision_at_k": None,
            "location_precision_at_returned": None,
            "location_recall_at_k": None,
            "returned": min(k, len(ranked_items)),
        }

    top = ranked_items[:k]
    matched_passages: set[str] = set()
    matched_locations: set[str] = set()
    item_labels: list[int] = []
    location_labels: list[int] = []
    graded_labels: list[int] = []
    for item in top:
        all_matches = [
            passage
            for passage in expected
            if _item_matches_passage(item, passage)
        ]
        new_matches = [
            passage for passage in all_matches if str(passage["id"]) not in matched_passages
        ]
        item_labels.append(1 if new_matches else 0)
        graded_labels.append(max((int(passage.get("relevance", 1)) for passage in new_matches), default=0))
        location_hit = False
        for passage in all_matches:
            passage_id = str(passage["id"])
            if passage in new_matches:
                matched_passages.add(passage_id)
            if (
                passage_id not in matched_locations
                and passage.get("locations")
                and _item_matches_location(item, passage["locations"])
            ):
                matched_locations.add(passage_id)
                location_hit = True
        location_labels.append(1 if location_hit else 0)

    passage_ids = [str(passage["id"]) for passage in expected]
    location_passage_ids = [str(passage["id"]) for passage in expected if passage.get("locations")]
    first_rank = next((index + 1 for index, label in enumerate(item_labels) if label), None)
    dcg = sum(
        ((2 ** grade) - 1) / math.log2(index + 2)
        for index, grade in enumerate(graded_labels)
        if grade
    )
    ideal_grades = sorted(
        (int(passage.get("relevance", 1)) for passage in expected),
        reverse=True,
    )[:k]
    ideal_dcg = sum(
        ((2 ** grade) - 1) / math.log2(index + 2)
        for index, grade in enumerate(ideal_grades)
    )

    return {
        "eligible": True,
        "ground_truth_level": "evidence_passage",
        "precision_at_k": round(sum(item_labels) / k, 6) if k else 0.0,
        "precision_at_returned": round(sum(item_labels) / len(top), 6) if top else 0.0,
        "recall_at_k": round(len(matched_passages) / len(expected), 6),
        "hit_at_k": int(bool(matched_passages)),
        "mrr": round(1.0 / first_rank, 6) if first_rank else 0.0,
        "ndcg_at_k": round(dcg / ideal_dcg, 6) if ideal_dcg else 0.0,
        "location_eligible": bool(location_passage_ids),
        "location_precision_at_k": (
            round(sum(location_labels) / k, 6) if location_passage_ids and k else None
        ),
        "location_precision_at_returned": (
            round(sum(location_labels) / len(top), 6) if location_passage_ids and top else None
        ),
        "location_recall_at_k": (
            round(len(matched_locations) / len(location_passage_ids), 6)
            if location_passage_ids else None
        ),
        "matched_passages": [passage_id for passage_id in passage_ids if passage_id in matched_passages],
        "missing_passages": [passage_id for passage_id in passage_ids if passage_id not in matched_passages],
        "matched_locations": [
            passage_id for passage_id in location_passage_ids if passage_id in matched_locations
        ],
        "missing_locations": [
            passage_id for passage_id in location_passage_ids if passage_id not in matched_locations
        ],
        "returned": len(top),
    }


def _item_matches_passage(item: dict, passage: dict) -> bool:
    if _matching_expected(str(item.get("title", "")), [str(passage.get("title", ""))]) is None:
        return False
    text = "\n".join(
        str(item.get(field, ""))
        for field in ("evidence", "excerpt", "fallback_excerpt", "text")
    )
    return all(contains_eval_term(text, term) for term in passage.get("required_terms", []))


def _item_matches_location(item: dict, expected_locations: list[str]) -> bool:
    actual_locations = [str(item.get("location", ""))]
    extra_locations = item.get("evidence_locations", [])
    if isinstance(extra_locations, list):
        actual_locations.extend(str(location) for location in extra_locations)
    return any(
        contains_eval_term(actual, expected)
        for expected in expected_locations
        for actual in actual_locations
    )


def term_recall(expected_terms: list[str], text: str) -> float | None:
    if not expected_terms:
        return None
    hits = sum(1 for term in expected_terms if contains_eval_term(text, term))
    return hits / len(expected_terms)


def cited_indexes(answer: str) -> list[int]:
    indexes = {
        int(value)
        for value in re.findall(r"(?:来源\s*)?\[(\d+)\]", str(answer or ""))
        if value.isdigit()
    }
    return sorted(indexes)


def answer_metrics(case: dict, result: dict) -> dict:
    answer = str(result.get("answer", ""))
    sources = list(result.get("source_details") or [])
    if not sources and result.get("sources"):
        sources = [{"title": title, "evidence": "", "location": ""} for title in result["sources"]]
    evidence_text = "\n".join(
        str(source.get("evidence", "")) + "\n" + str(source.get("excerpt", ""))
        for source in sources
    )
    focused_evidence_text = "\n".join(str(source.get("evidence", "")) for source in sources)
    answer_term_recall = term_recall(case.get("expected_answer_terms", []), answer)
    evidence_term_recall = term_recall(case.get("expected_evidence_terms", []), evidence_text)
    visible_evidence_term_recall = term_recall(case.get("expected_evidence_terms", []), focused_evidence_text)
    forbidden = case.get("forbidden_terms", [])
    forbidden_ok = not any(
        term and (contains_eval_term(answer, term) or contains_eval_term(focused_evidence_text, term))
        for term in forbidden
    )

    indexes = cited_indexes(answer)
    valid_indexes = [index for index in indexes if 1 <= index <= len(sources)]
    citation_validity = len(valid_indexes) / len(indexes) if indexes else (1.0 if not sources else 0.0)
    citation_presence = 1.0 if (not sources or indexes) else 0.0

    claim_mapping: list[dict] = []
    if answer and sources:
        # Import locally so the standalone metric helpers stay lightweight and
        # avoid making dataset validation depend on model/runtime imports.
        from pocket_memory.rag import RagPipeline

        claim_mapping = RagPipeline.answer_evidence_mapping(
            str(case.get("question", "")),
            answer,
            sources,
        )
    claim_count = len(claim_mapping)
    claim_citation_coverage = None
    claim_support_proxy = None
    if claim_count:
        claim_citation_coverage = sum(
            1
            for claim in claim_mapping
            if claim.get("valid_citations") and not claim.get("invalid_citations")
        ) / claim_count
        claim_support_proxy = sum(
            1 for claim in claim_mapping if claim.get("status") == "supported"
        ) / claim_count

    expects_refusal = bool(case.get("expected_refusal"))
    refusal_text = any(pattern in answer for pattern in REFUSAL_PATTERNS)
    refusal_correct = None
    if expects_refusal:
        refusal_correct = bool(not sources and refusal_text)

    expects_clarification = bool(case.get("expected_clarification"))
    clarification_text = any(pattern in answer for pattern in CLARIFICATION_PATTERNS)
    clarification_correct = None
    if expects_clarification:
        clarification_correct = bool(not sources and clarification_text)

    return {
        "answer_term_recall": None if answer_term_recall is None else round(answer_term_recall, 6),
        "evidence_term_recall": None if evidence_term_recall is None else round(evidence_term_recall, 6),
        "visible_evidence_term_recall": (
            None if visible_evidence_term_recall is None else round(visible_evidence_term_recall, 6)
        ),
        "forbidden_ok": forbidden_ok,
        "citation_presence": citation_presence,
        "citation_validity": round(citation_validity, 6),
        "cited_indexes": indexes,
        "claim_count": claim_count,
        "claim_citation_coverage": (
            None if claim_citation_coverage is None else round(claim_citation_coverage, 6)
        ),
        "claim_support_proxy": None if claim_support_proxy is None else round(claim_support_proxy, 6),
        "unsupported_claim_count": sum(
            1 for claim in claim_mapping if claim.get("status") == "unsupported"
        ),
        "refusal_correct": refusal_correct,
        "clarification_correct": clarification_correct,
        "source_count": len(sources),
        "evaluation_type": {
            "answer_term_recall": "heuristic",
            "evidence_term_recall": "heuristic",
            "forbidden_ok": "heuristic",
            "citation_presence": "deterministic",
            "citation_validity": "deterministic",
            "claim_citation_coverage": "deterministic",
            "claim_support_proxy": "heuristic_lexical",
            "refusal_correct": "deterministic+pattern",
            "clarification_correct": "deterministic+pattern",
            "answer_correctness": "human_required",
            "faithfulness": "human_required",
        },
    }


def failure_attribution(
    case: dict,
    retrieval: dict | None,
    final_retrieval: dict | None,
    answer: dict | None,
    elapsed_seconds: float | None,
    latency_gate_seconds: float = 12.0,
    retrieval_passages: dict | None = None,
    final_passages: dict | None = None,
) -> list[str]:
    failures: list[str] = []
    if case.get("expected_refusal"):
        if answer is not None and answer.get("refusal_correct") is not True:
            failures.append("VERIFY")
    elif case.get("expected_clarification"):
        if answer is not None and answer.get("clarification_correct") is not True:
            failures.append("QUERY")
    else:
        if retrieval and retrieval.get("recall_at_k") is not None and retrieval["recall_at_k"] < 1.0:
            failures.append("RECALL")
        else:
            if retrieval_passages and retrieval_passages.get("recall_at_k") is not None:
                if retrieval_passages["recall_at_k"] < 1.0:
                    failures.append("LOCATION")
                elif (
                    retrieval_passages.get("location_recall_at_k") is not None
                    and retrieval_passages["location_recall_at_k"] < 1.0
                ):
                    failures.append("LOCATION")
            if final_retrieval and final_retrieval.get("recall_at_k") is not None and final_retrieval["recall_at_k"] < 1.0:
                failures.append("CONTEXT")
            elif final_passages and final_passages.get("recall_at_k") is not None:
                if final_passages["recall_at_k"] < 1.0:
                    failures.append("CONTEXT")
                elif (
                    final_passages.get("location_recall_at_k") is not None
                    and final_passages["location_recall_at_k"] < 1.0
                ):
                    failures.append("LOCATION")
        if answer and answer.get("answer_term_recall") is not None and answer["answer_term_recall"] < 1.0:
            failures.append("GENERATE")
        if answer and answer.get("forbidden_ok") is False:
            failures.append("GENERATE")
        if answer and (answer.get("citation_presence", 1.0) < 1.0 or answer.get("citation_validity", 1.0) < 1.0):
            failures.append("VERIFY")
        if answer and answer.get("claim_support_proxy") is not None and answer["claim_support_proxy"] < 1.0:
            failures.append("VERIFY")
    if elapsed_seconds is not None and elapsed_seconds > latency_gate_seconds:
        failures.append("PERF")
    return list(dict.fromkeys(failures))


def aggregate_case_metrics(cases: list[dict]) -> dict:
    evaluated_cases = [case for case in cases if not case.get("evaluation_excluded")]
    retrieval_rows = [case.get("retrieval", {}) for case in evaluated_cases]
    retrieval_passage_rows = [case.get("retrieval_passages", {}) for case in evaluated_cases]
    final_rows = [case.get("final_retrieval", {}) for case in evaluated_cases]
    final_passage_rows = [case.get("final_passages", {}) for case in evaluated_cases]
    visible_passage_rows = [case.get("final_visible_passages", {}) for case in evaluated_cases]
    answer_rows = [case.get("answer_metrics", {}) for case in evaluated_cases]
    latency = [
        case["elapsed_seconds"]
        for case in evaluated_cases
        if case.get("elapsed_seconds") is not None
    ]

    failure_counts: defaultdict[str, int] = defaultdict(int)
    for case in evaluated_cases:
        for failure in case.get("failure_attribution", []):
            failure_counts[failure] += 1

    return {
        "case_count": len(evaluated_cases),
        "case_count_total": len(cases),
        "case_count_evaluated": len(evaluated_cases),
        "case_count_excluded": len(cases) - len(evaluated_cases),
        "scored_case_counts": {
            "raw_source": sum(row.get("recall_at_k") is not None for row in retrieval_rows),
            "final_source": sum(row.get("recall_at_k") is not None for row in final_rows),
            "final_passage": sum(row.get("recall_at_k") is not None for row in final_passage_rows),
            "final_location": sum(row.get("location_recall_at_k") is not None for row in final_passage_rows),
            "refusal": sum(row.get("refusal_correct") is not None for row in answer_rows),
        },
        "retrieval": {
            "precision_at_k": _rounded_mean(row.get("precision_at_k") for row in retrieval_rows),
            "precision_at_returned": _rounded_mean(row.get("precision_at_returned") for row in retrieval_rows),
            "recall_at_k": _rounded_mean(row.get("recall_at_k") for row in retrieval_rows),
            "hit_at_k": _rounded_mean(row.get("hit_at_k") for row in retrieval_rows),
            "mrr": _rounded_mean(row.get("mrr") for row in retrieval_rows),
            "ndcg_at_k": _rounded_mean(row.get("ndcg_at_k") for row in retrieval_rows),
            "duplicate_source_ratio": _rounded_mean(row.get("duplicate_source_ratio") for row in retrieval_rows),
        },
        "retrieval_passages": _aggregate_passage_rows(retrieval_passage_rows),
        "final_sources": {
            "precision_at_k": _rounded_mean(row.get("precision_at_k") for row in final_rows),
            "precision_at_returned": _rounded_mean(row.get("precision_at_returned") for row in final_rows),
            "recall_at_k": _rounded_mean(row.get("recall_at_k") for row in final_rows),
            "hit_at_k": _rounded_mean(row.get("hit_at_k") for row in final_rows),
            "mrr": _rounded_mean(row.get("mrr") for row in final_rows),
            "ndcg_at_k": _rounded_mean(row.get("ndcg_at_k") for row in final_rows),
        },
        "final_passages": _aggregate_passage_rows(final_passage_rows),
        "final_visible_passages": _aggregate_passage_rows(visible_passage_rows),
        "answer": {
            "answer_term_recall": _rounded_mean(row.get("answer_term_recall") for row in answer_rows),
            "evidence_term_recall": _rounded_mean(row.get("evidence_term_recall") for row in answer_rows),
            "visible_evidence_term_recall": _rounded_mean(row.get("visible_evidence_term_recall") for row in answer_rows),
            "forbidden_ok": _rounded_mean(
                float(row["forbidden_ok"]) for row in answer_rows if "forbidden_ok" in row
            ),
            "citation_presence": _rounded_mean(row.get("citation_presence") for row in answer_rows),
            "citation_validity": _rounded_mean(row.get("citation_validity") for row in answer_rows),
            "claim_citation_coverage": _rounded_mean(
                row.get("claim_citation_coverage") for row in answer_rows
            ),
            "claim_support_proxy": _rounded_mean(row.get("claim_support_proxy") for row in answer_rows),
            "unsupported_claim_count": sum(
                int(row.get("unsupported_claim_count", 0) or 0) for row in answer_rows
            ),
            "refusal_accuracy": _rounded_mean(row.get("refusal_correct") for row in answer_rows),
            "clarification_accuracy": _rounded_mean(row.get("clarification_correct") for row in answer_rows),
            "answer_correctness": None,
            "faithfulness": None,
        },
        "latency_seconds": {
            "p50": _rounded(percentile(latency, 0.50)),
            "p95": _rounded(percentile(latency, 0.95)),
            "max": _rounded(max(latency) if latency else None),
        },
        "failure_attribution": dict(sorted(failure_counts.items())),
    }


def aggregate_by_group(cases: list[dict]) -> dict:
    groups: defaultdict[str, list[dict]] = defaultdict(list)
    for case in cases:
        case_id = str(case.get("id", ""))
        prefix = case_id.split("-", 1)[0] if "-" in case_id else str(case.get("kind", "other"))
        groups[prefix].append(case)
    return {name: aggregate_case_metrics(rows) for name, rows in sorted(groups.items())}


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _rounded_mean(values: Iterable[float | int | bool | None]) -> float | None:
    return _rounded(mean(values))


def _aggregate_passage_rows(rows: list[dict]) -> dict:
    return {
        "precision_at_k": _rounded_mean(row.get("precision_at_k") for row in rows),
        "precision_at_returned": _rounded_mean(row.get("precision_at_returned") for row in rows),
        "recall_at_k": _rounded_mean(row.get("recall_at_k") for row in rows),
        "hit_at_k": _rounded_mean(row.get("hit_at_k") for row in rows),
        "mrr": _rounded_mean(row.get("mrr") for row in rows),
        "ndcg_at_k": _rounded_mean(row.get("ndcg_at_k") for row in rows),
        "location_precision_at_k": _rounded_mean(row.get("location_precision_at_k") for row in rows),
        "location_precision_at_returned": _rounded_mean(
            row.get("location_precision_at_returned") for row in rows
        ),
        "location_recall_at_k": _rounded_mean(row.get("location_recall_at_k") for row in rows),
    }
