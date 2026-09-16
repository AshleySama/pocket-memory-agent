"""Run and gate a de-identified real-note release evaluation via ``/api/ask``.

The evaluator deliberately separates automatic retrieval/latency measurements
from human review of factuality and faithfulness.  A model must not be the sole
judge of its own answers.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from urllib.request import Request, urlopen


REQUIRED_SCENARIOS = {
    "precise_parameter", "multi_note_comparison", "time_query", "ocr_noise",
    "similar_concept_distraction", "no_answer", "ambiguous_question", "follow_up",
}
REVIEW_FIELDS = {
    "factually_correct",
    "faithful",
    "citation_supported",
    "correct_refusal",
    "high_risk_error",
    "opened_correct_location",
}
THRESHOLDS = {
    "recall_at_5": 0.95,
    "precision_at_5": 0.80,
    "location_recall_at_5": 0.95,
    "location_precision_at_5": 0.80,
    "factual_accuracy": 0.85,
    "faithfulness": 0.95,
    "citation_support": 0.95,
    "location_open_accuracy": 0.95,
    "refusal_accuracy": 0.95,
    "high_risk_errors": 0,
    "latency_p95_seconds": 12.0,
}


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * ratio))] if ordered else 0.0


def request(base_url: str, case: dict) -> tuple[dict, float]:
    payload = {"question": case["question"]}
    if isinstance(case.get("history"), list):
        payload["history"] = case["history"]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    with urlopen(Request(base_url.rstrip("/") + "/api/ask", data=body, method="POST",
                         headers={"Content-Type": "application/json"}), timeout=60) as response:
        return json.loads(response.read().decode("utf-8")), time.perf_counter() - started


def source_key(source: dict) -> str:
    return str(source.get("id") or source.get("title") or "")


def expected_sources(case: dict) -> set[str]:
    return {str(item) for item in case.get("expected_sources", []) if str(item)}


def source_locations(source: dict) -> set[str]:
    """Return every displayed location for a grouped source result.

    A single source card can now carry several chunks from one note or file.
    Evaluation must retain those locations instead of treating the card as one
    undifferentiated document hit.
    """
    locations = source.get("evidence_locations") or [source.get("location")]
    return {str(location) for location in locations if str(location or "").strip()}


def expected_locations(case: dict) -> set[str]:
    return {str(item) for item in case.get("expected_locations", []) if str(item)}


def collect(dataset: dict, base_url: str) -> dict:
    cases = []
    for index, case in enumerate(dataset.get("cases", []), 1):
        response, elapsed = request(base_url, case)
        sources = response.get("sources", [])
        expected = expected_sources(case)
        expected_location_set = expected_locations(case)
        retrieved = {source_key(source) for source in sources[:5]}
        retrieved_locations = set().union(*(source_locations(source) for source in sources[:5])) if sources else set()
        relevant = expected & retrieved
        recall = len(relevant) / len(expected) if expected else None
        precision = len(relevant) / len(retrieved) if retrieved else (1.0 if not expected else 0.0)
        location_relevant = expected_location_set & retrieved_locations
        location_recall = len(location_relevant) / len(expected_location_set) if expected_location_set else None
        location_precision = (
            len(location_relevant) / len(retrieved_locations)
            if retrieved_locations else (1.0 if not expected_location_set else 0.0)
        )
        cases.append({
            **case,
            "result": {
                "answer": response.get("answer", ""),
                "sources": sources,
                "kind": response.get("kind", "qa"),
                "latency_seconds": round(elapsed, 3),
                "retrieval_recall_at_5": recall,
                "retrieval_precision_at_5": precision,
                "retrieval_location_recall_at_5": location_recall,
                "retrieval_location_precision_at_5": location_precision,
            },
            "review": case.get("review", {}),
        })
        print(f"[{index}/{len(dataset['cases'])}] {case['id']} {elapsed:.2f}s")
    return {"dataset": dataset.get("dataset", {}), "cases": cases, "thresholds": THRESHOLDS}


def rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def gate(report: dict) -> dict:
    cases = report.get("cases", [])
    metadata = report.get("dataset", {})
    failures: list[str] = []
    missing_results = [case.get("id", "?") for case in cases if not isinstance(case.get("result"), dict)]
    if missing_results:
        failures.append(f"未采集 /api/ask 结果：{len(missing_results)} 条")
    if len(cases) < 100:
        failures.append(f"样本不足：{len(cases)}/100")
    if metadata.get("source") != "deidentified-real-notes":
        failures.append("dataset.source 必须为 deidentified-real-notes")
    scenarios = {case.get("scenario") for case in cases}
    missing = REQUIRED_SCENARIOS - scenarios
    if missing:
        failures.append("缺少场景：" + ", ".join(sorted(missing)))
    review_missing = [case.get("id", "?") for case in cases if not REVIEW_FIELDS <= set(case.get("review", {}))]
    if review_missing:
        failures.append(f"人工复核字段不完整：{len(review_missing)} 条")
    location_missing = [case.get("id", "?") for case in cases if not expected_locations(case)]
    if location_missing:
        failures.append(f"缺少 expected_locations：{len(location_missing)} 条")
    if not metadata.get("target_hardware"):
        failures.append("缺少 target_hardware，不能判定目标硬件延迟")

    measured = [case for case in cases if isinstance(case.get("result"), dict)]
    recalls = [item["result"].get("retrieval_recall_at_5") for item in measured if item["result"].get("retrieval_recall_at_5") is not None]
    precisions = [item["result"].get("retrieval_precision_at_5", 0.0) for item in measured]
    location_recalls = [
        item["result"].get("retrieval_location_recall_at_5")
        for item in measured
        if item["result"].get("retrieval_location_recall_at_5") is not None
    ]
    location_precisions = [
        item["result"].get("retrieval_location_precision_at_5", 0.0)
        for item in measured
        if item["result"].get("retrieval_location_precision_at_5") is not None
    ]
    reviewed = [case for case in cases if REVIEW_FIELDS <= set(case.get("review", {}))]
    refusal = [case["review"]["correct_refusal"] for case in reviewed if case.get("expects_refusal")]
    metrics = {
        "sample_count": len(cases),
        "recall_at_5": rate(recalls),
        "precision_at_5": rate(precisions),
        "location_recall_at_5": rate(location_recalls),
        "location_precision_at_5": rate(location_precisions),
        "factual_accuracy": rate([case["review"]["factually_correct"] for case in reviewed]),
        "faithfulness": rate([case["review"]["faithful"] for case in reviewed]),
        "citation_support": rate([case["review"]["citation_supported"] for case in reviewed]),
        "location_open_accuracy": rate([case["review"]["opened_correct_location"] for case in reviewed]),
        "refusal_accuracy": rate(refusal),
        "high_risk_errors": sum(bool(case["review"]["high_risk_error"]) for case in reviewed),
        "latency_p95_seconds": round(percentile([case["result"].get("latency_seconds", 0.0) for case in measured], .95), 3),
    }
    for key, threshold in THRESHOLDS.items():
        value = metrics.get(key)
        if value is None:
            failures.append(f"无法计算 {key}")
        elif key == "high_risk_errors" and value != threshold:
            failures.append(f"{key}={value}，要求 0")
        elif key == "latency_p95_seconds" and value > threshold:
            failures.append(f"{key}={value}，要求 <= {threshold}")
        elif key not in {"high_risk_errors", "latency_p95_seconds"} and value < threshold:
            failures.append(f"{key}={value}，要求 >= {threshold}")
    return {"passed": not failures, "metrics": metrics, "failures": failures, "thresholds": THRESHOLDS}


def main() -> int:
    parser = argparse.ArgumentParser(description="真实脱敏笔记 /api/ask 发布门槛评测")
    parser.add_argument("--dataset", required=True, type=Path, help="问题集或已采集结果 JSON")
    parser.add_argument("--api-base", help="例如 http://127.0.0.1:8767；提供后直接采集")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-only", action="store_true", help="只校验已包含 result/review 的结果文件")
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    if args.gate_only:
        report = data
    else:
        if not args.api_base:
            parser.error("采集时必须提供 --api-base")
        report = collect(data, args.api_base)
    report["release_gate"] = gate(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["release_gate"], ensure_ascii=False, indent=2))
    return 0 if report["release_gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
