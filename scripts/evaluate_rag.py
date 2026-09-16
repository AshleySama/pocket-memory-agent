from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pocket_memory.embedding import EmbeddingService
from pocket_memory.rag import RagPipeline
from pocket_memory.storage import NoteStore


class LexicalEmbeddingEngine:
    available = True
    unavailable_reason = ""

    def embed(self, text: str):
        import hashlib
        import numpy as np

        vector = np.zeros(128, dtype=np.float32)
        for token in set(text[i:i + 2] for i in range(max(0, len(text) - 1))):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            vector[digest[0] % vector.size] += 1.0
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_store(fixture: dict):
    temp = tempfile.TemporaryDirectory()
    store = NoteStore(Path(temp.name) / "data")
    embedding = EmbeddingService(store, LexicalEmbeddingEngine())
    for document in fixture["documents"]:
        note = store.create_note(document["title"], document["content"], category="学习", subcategory="古诗")
        embedding._process(note.id)
    return temp, store, embedding


def answer_from_sources(question: str, sources: list[dict]) -> str:
    return RagPipeline.fallback_answer(question, sources)


def score_case(case: dict, sources: list[dict], answer: str) -> dict:
    source_titles = [source["title"] for source in sources]
    source_text = "\n".join(source.get("excerpt", "") for source in sources)
    evidence_text = "\n".join(source.get("evidence", "") for source in sources)
    retrieval_hits = sum(1 for title in case.get("expected_titles", []) if any(title in actual for actual in source_titles))
    retrieval_total = max(1, len(case.get("expected_titles", [])))
    answer_hits = sum(1 for term in case.get("expected_answer_terms", []) if term in answer)
    answer_total = max(1, len(case.get("expected_answer_terms", [])))
    evidence_hits = sum(1 for term in case.get("expected_evidence_terms", []) if term in evidence_text or term in source_text)
    evidence_total = max(1, len(case.get("expected_evidence_terms", [])))
    forbidden_ok = not any(term in answer or term in source_text for term in case.get("forbidden_terms", []))
    retrieval_score = retrieval_hits / retrieval_total
    answer_score = answer_hits / answer_total
    evidence_score = evidence_hits / evidence_total if case.get("expected_evidence_terms") else 1.0
    if case.get("expected_refusal"):
        # A correct abstention must not be penalized for lacking a source.  The
        # safety condition is that it does not surface unrelated citations.
        no_irrelevant_source = not sources
        total = 0.65 * answer_score + 0.35 * float(no_irrelevant_source)
        retrieval_score = float(no_irrelevant_source)
        evidence_score = float(no_irrelevant_source)
    elif case.get("expected_clarification"):
        # Ambiguous questions are a routing test, not a retrieval contest.
        no_premature_source = not sources
        total = 0.70 * answer_score + 0.30 * float(no_premature_source)
        retrieval_score = float(no_premature_source)
        evidence_score = float(no_premature_source)
    else:
        total = 0.45 * retrieval_score + 0.35 * answer_score + 0.15 * evidence_score + 0.05 * float(forbidden_ok)
    return {
        "id": case["id"],
        "kind": case.get("kind", "generic"),
        "score": round(total, 3),
        "retrieval_score": round(retrieval_score, 3),
        "answer_score": round(answer_score, 3),
        "evidence_score": round(evidence_score, 3),
        "forbidden_ok": forbidden_ok,
        "sources": source_titles,
        "source_details": [
            {
                "title": source.get("title", ""),
                "location": source.get("location", ""),
                "evidence": source.get("evidence", ""),
            }
            for source in sources
        ],
        "answer": answer,
    }


def run_custom(fixture: dict) -> dict:
    temp, store, embedding = build_store(fixture)
    try:
        pipeline = RagPipeline(store, embedding)
        results = []
        for case in fixture["cases"]:
            chunks = pipeline.retrieve(case["question"], limit=8)
            sources = pipeline.source_payloads(case["question"], chunks)
            answer = answer_from_sources(case["question"], sources)
            results.append(score_case(case, sources, answer))
        average = sum(item["score"] for item in results) / len(results)
        return {"name": "custom_pipeline", "average_score": round(average, 3), "cases": results}
    finally:
        embedding.shutdown()
        store.close()
        temp.cleanup()


def framework_status() -> list[dict]:
    frameworks = [
        {
            "name": "llama_index",
            "import": "llama_index.core",
            "notes": "RAG/document indexing fit is strong, but dependency and packaging impact must be measured before adoption.",
        },
        {
            "name": "haystack",
            "import": "haystack",
            "notes": "Pipeline abstraction fits retriever/ranker/generator design well; packaging impact is likely larger than custom code.",
        },
    ]
    rows = []
    for framework in frameworks:
        try:
            available = importlib.util.find_spec(framework["import"]) is not None
        except ModuleNotFoundError:
            available = False
        rows.append(
            {
                "name": framework["name"],
                "available": available,
                "functional_score": None if not available else "not_run_in_minimal_offline_eval",
                "feasibility_score": 0.58 if framework["name"] == "llama_index" else 0.48,
                "packaging_risk": "medium-high" if framework["name"] == "llama_index" else "high",
                "recommendation": "adapter_experiment_only",
                "notes": framework["notes"],
            }
        )
    return rows


def main() -> int:
    # Windows PowerShell may still use a GBK console. The fixture contains
    # scientific symbols, so keep report printing deterministic and UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="Evaluate Pocket Memory RAG retrieval quality.")
    parser.add_argument("--fixture", type=Path, default=Path("tests/fixtures/rag_eval_cases.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/rag_eval_latest.json"))
    args = parser.parse_args()

    fixture = load_fixture(args.fixture)
    report = {
        "baseline_commit": "38f308f Show evidence sentences for RAG sources",
        "custom": run_custom(fixture),
        "frameworks": framework_status(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
