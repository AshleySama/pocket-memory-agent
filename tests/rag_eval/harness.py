# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from pocket_memory.embedding import BgeEmbeddingEngine, EmbeddingService
from pocket_memory.rag import QueryResolver, RagPipeline
from pocket_memory.storage import NoteStore
from scripts.rag_eval_fixture import populate_fixture
from scripts.rag_eval_matching import METRIC_VERSION

from .dataset import file_sha256, load_dataset, select_cases
from .fingerprint import environment_fingerprint
from .metrics import (
    aggregate_by_group,
    aggregate_case_metrics,
    answer_metrics,
    failure_attribution,
    passage_metrics,
    retrieval_metrics,
)


def run_evaluation(
    root: Path,
    fixture_path: Path,
    mode: str = "both",
    case_ids: set[str] | None = None,
    top_k: int = 5,
    command: list[str] | None = None,
    api_report_path: Path | None = None,
) -> dict:
    if mode not in {"retrieval", "api", "both"}:
        raise ValueError("mode must be retrieval, api, or both")

    fixture_path = fixture_path.resolve()
    fixture = load_dataset(fixture_path)
    cases = select_cases(fixture["cases"], case_ids)
    if api_report_path is not None and mode == "retrieval":
        raise ValueError("api_report_path requires api or both mode")
    # Validate a reused artifact before starting any model work. Missing cases
    # must not disappear from metric denominators and look like successful runs.
    api_results = (
        load_api_report(
            api_report_path, case_ids, fixture_path=fixture_path, expected_cases=cases
        )
        if api_report_path is not None else None
    )
    raw_results = evaluate_retrieval(fixture, cases, top_k=top_k) if mode in {"retrieval", "both"} else {}
    if mode in {"api", "both"}:
        if api_results is None:
            api_results = _validate_api_report(
                evaluate_api(fixture_path, case_ids), case_ids,
                fixture_path=fixture_path, expected_cases=cases,
            )
    api_by_id = {case["id"]: case for case in (api_results or {}).get("cases", [])}

    rows = []
    for case in cases:
        case_id = case["id"]
        raw = raw_results.get(case_id)
        api = api_by_id.get(case_id)
        final_items = []
        if api:
            details = list(api.get("source_details") or [])
            if details:
                final_items = details
            else:
                final_items = [{"title": title} for title in api.get("sources", [])]
        final_ranking = retrieval_metrics(case, final_items, k=top_k) if api else {}
        final_passage_ranking = passage_metrics(case, final_items, k=top_k) if api else {}
        visible_passage_ranking = passage_metrics(case, [
            {key: item.get(key) for key in ("title", "evidence", "location", "evidence_locations")}
            for item in final_items
        ], k=top_k) if api else {}
        evaluated_answer = answer_metrics(case, api) if api else {}
        elapsed = api.get("elapsed_seconds") if api else (raw or {}).get("elapsed_seconds")
        raw_metrics = (raw or {}).get("metrics", {})
        raw_passage_metrics = (raw or {}).get("passage_metrics", {})
        evaluation_excluded = bool(case.get("evaluation_excluded"))
        attributed_failures = [] if evaluation_excluded else failure_attribution(
            case,
            raw_metrics or None,
            final_ranking or None,
            evaluated_answer or None,
            elapsed,
            retrieval_passages=raw_passage_metrics or None,
            final_passages=final_passage_ranking or None,
        )
        rows.append(
            {
                "id": case_id,
                "kind": case.get("kind", "generic"),
                "priority": case.get("priority", ""),
                "question": case["question"],
                "retrieval": raw_metrics,
                "retrieval_passages": raw_passage_metrics,
                "final_retrieval": final_ranking,
                "final_passages": final_passage_ranking,
                "final_visible_passages": visible_passage_ranking,
                "answer_metrics": evaluated_answer,
                "elapsed_seconds": elapsed,
                "evaluation_excluded": evaluation_excluded,
                "evaluation_exclusion_reason": case.get(
                    "evaluation_exclusion_reason", ""
                ),
                "failure_attribution": attributed_failures,
                "raw_candidates": (raw or {}).get("candidates", []),
                "final_sources": final_items,
                "answer": api.get("answer", "") if api else "",
                "legacy_proxy_score": api.get("score") if api else None,
                "retrieval_diagnostics": (raw or {}).get("diagnostics", {}),
                "api_timing": api.get("timing", {}) if api else {},
                "api_diagnostics": api.get("diagnostics", {}) if api else {},
            }
        )

    report = {
        "schema_version": 2,
        "metric_version": METRIC_VERSION,
        "mode": mode,
        "top_k": top_k,
        "fingerprint": environment_fingerprint(root, fixture_path, command=command),
        "dataset": fixture.get("dataset", {}),
        "aggregate": aggregate_case_metrics(rows),
        "by_group": aggregate_by_group(rows),
        "legacy_api_summary": {
            "average_score": (api_results or {}).get("average_score"),
            "latency_seconds": (api_results or {}).get("latency_seconds"),
        },
        "cases": rows,
    }
    if api_results is not None:
        report["api_validation"] = api_results["artifact_validation"]
    if api_report_path is not None:
        report["api_artifact"] = {
            "path": str(api_report_path.resolve()),
            "sha256": file_sha256(api_report_path),
        }
    return report


def evaluate_retrieval(fixture: dict, cases: list[dict], top_k: int = 5) -> dict[str, dict]:
    temporary = tempfile.TemporaryDirectory()
    store = NoteStore(Path(temporary.name) / "data")
    engine = BgeEmbeddingEngine()
    if not engine.available:
        store.close()
        temporary.cleanup()
        raise RuntimeError(f"BGE unavailable: {engine.unavailable_reason}")
    embedding = EmbeddingService(store, engine)
    try:
        populate_fixture(store, embedding, fixture)

        pipeline = RagPipeline(store, embedding)
        output: dict[str, dict] = {}
        total = len(cases)
        for index, case in enumerate(cases, 1):
            print(f"[retrieval {index}/{total}] {case['id']}", file=sys.stderr, flush=True)
            started = time.perf_counter()
            diagnostics: dict = {}
            understanding = QueryResolver.resolve(case["question"], case.get("history"))
            diagnostics["query_understanding"] = understanding.diagnostics()
            chunks = pipeline.retrieve(
                understanding.retrieval_query,
                limit=max(8, top_k),
                extra_terms=list(understanding.aliases),
                diagnostics=diagnostics,
            )
            elapsed = time.perf_counter() - started
            candidates = [_serialise_candidate(chunk) for chunk in chunks]
            output[case["id"]] = {
                "elapsed_seconds": round(elapsed, 6),
                "metrics": retrieval_metrics(case, candidates, k=top_k),
                "passage_metrics": passage_metrics(case, candidates, k=top_k),
                "candidates": candidates,
                "diagnostics": diagnostics,
            }
        return output
    finally:
        embedding.shutdown()
        store.close()
        temporary.cleanup()


def evaluate_api(fixture_path: Path, case_ids: set[str] | None = None) -> dict:
    # Keep the production HTTP path in one place. The legacy evaluator already
    # starts PocketMemoryServer and calls /api/ask exactly as the UI does.
    from scripts.evaluate_rag_api import evaluate

    return evaluate(fixture_path, case_ids)


def load_api_report(
    path: Path,
    case_ids: set[str] | None = None,
    *,
    fixture_path: Path | None = None,
    expected_cases: list[dict] | None = None,
) -> dict:
    payload = json.loads(path.resolve().read_text(encoding="utf-8"))
    return _validate_api_report(
        payload, case_ids, fixture_path=fixture_path, expected_cases=expected_cases
    )


def _validate_api_report(
    payload: dict,
    case_ids: set[str] | None = None,
    *,
    fixture_path: Path | None = None,
    expected_cases: list[dict] | None = None,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("API report root must be an object")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("API report must contain a cases list")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"].strip():
            raise ValueError("API report cases must contain non-empty string ids")
        if case["id"] in seen:
            raise ValueError(f"API report contains duplicate case id: {case['id']}")
        seen.add(case["id"])
    if case_ids:
        cases = [case for case in cases if case.get("id") in case_ids]
    if not cases:
        raise ValueError("API report has no cases matching the requested ids")

    expected_by_id = {case["id"]: case for case in expected_cases} if expected_cases is not None else None
    requested_ids = set(expected_by_id) if expected_by_id is not None else set(case_ids or [])
    actual_ids = {case["id"] for case in cases}
    missing = requested_ids - actual_ids
    if missing:
        raise ValueError("API report is missing required case ids: " + ", ".join(sorted(missing)))
    if expected_by_id is not None and actual_ids - requested_ids:
        raise ValueError("API report contains unknown case ids: " + ", ".join(sorted(actual_ids - requested_ids)))

    questions_verified = 0
    if expected_by_id is not None:
        for case in cases:
            if "question" in case:
                if case["question"] != expected_by_id[case["id"]]["question"]:
                    raise ValueError(f"API report question conflicts with fixture for case: {case['id']}")
                questions_verified += 1

    hash_verified = False
    name_verified = False
    if fixture_path is not None:
        fixture_sha256 = file_sha256(fixture_path)
        fingerprint = payload.get("fingerprint")
        fingerprint_fixture = fingerprint.get("fixture", {}) if isinstance(fingerprint, dict) else {}
        if not isinstance(fingerprint_fixture, dict):
            fingerprint_fixture = {}
        names = [payload.get("fixture"), fingerprint_fixture.get("path")]
        for name in names:
            if name is not None:
                if not isinstance(name, str) or name.replace("\\", "/").rsplit("/", 1)[-1] != fixture_path.name:
                    raise ValueError("API report fixture name conflicts with selected fixture")
                name_verified = True
        hashes = [payload.get("fixture_sha256"), fingerprint_fixture.get("sha256")]
        for digest in hashes:
            if digest is not None:
                if not isinstance(digest, str) or digest.upper() != fixture_sha256:
                    raise ValueError("API report fixture SHA-256 conflicts with selected fixture")
                hash_verified = True

    selected = dict(payload)
    selected["cases"] = cases
    selected["artifact_validation"] = {
        "status": "fixture_verified" if hash_verified and expected_by_id is not None else "legacy_unverified",
        "fixture_sha256_verified": hash_verified,
        "fixture_name_verified": name_verified,
        "case_ids_verified": expected_by_id is not None,
        "question_count_verified": questions_verified,
        "selected_case_count": len(cases),
        "runtime_source_provenance_verified": False,
        "note": (
            "Fixture identity checked; original runtime/model/source provenance is not attested."
            if hash_verified else
            "Legacy artifact has no verified fixture fingerprint; matching ids/names are not proof of provenance."
        ),
    }
    return selected


def _serialise_candidate(chunk: dict) -> dict:
    return {
        "title": str(chunk.get("title", "")),
        "location": str(chunk.get("location", "")),
        "kind": str(chunk.get("kind", "")),
        "chunk_identity": str(chunk.get("chunk_identity", "")),
        "source_group": str(chunk.get("source_group", "")),
        "score": _number(chunk.get("score")),
        "rank_score": _number(chunk.get("rank_score")),
        "lexical_score": _number(chunk.get("lexical_score")),
        "fts_score": _number(chunk.get("fts_score")),
        "rerank_adjustment": _number(chunk.get("rerank_adjustment")),
        "fusion_score": _number(chunk.get("fusion_score")),
        "fusion_channels": dict(chunk.get("fusion_channels") or {}),
        "fusion_contributions": dict(chunk.get("fusion_contributions") or {}),
        "hits": [str(hit) for hit in chunk.get("hits", [])],
        "compound_clauses": [str(clause) for clause in chunk.get("compound_clauses", [])],
        "compound_reserved": bool(chunk.get("compound_reserved", False)),
        "evidence": str(chunk.get("excerpt", ""))[:1000],
        "excerpt": str(chunk.get("excerpt", ""))[:1000],
        "fallback_excerpt": str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))[:1800],
        "evidence_locations": [str(chunk.get("location", ""))] if chunk.get("location") else [],
        "chunk_id": chunk.get("document_chunk_id") or chunk.get("note_chunk_id"),
        "chunk_order": chunk.get("document_chunk_order") or chunk.get("note_chunk_order"),
    }


def _number(value) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None
