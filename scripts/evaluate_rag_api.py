"""Evaluate the production /api/ask path against a local RAG fixture.

Unlike the older LLM benchmark, this script exercises intent routing, query
rewriting, context construction and the same HTTP response consumed by the UI.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pocket_memory.config import AppConfig
from pocket_memory.embedding import BgeEmbeddingEngine, EmbeddingService
from pocket_memory.intelligence import IntelligenceService, LlamaCppTextEngine
from pocket_memory.ocr import OcrService
from pocket_memory.server import PocketMemoryServer
from pocket_memory.storage import NoteStore
from scripts.evaluate_rag_llm import score_case


class UnavailableOcrEngine:
    available = False
    unavailable_reason = "RAG evaluation does not need OCR"


def request(base_url: str, question: str, history: list[dict] | None = None) -> tuple[dict, float]:
    started = time.perf_counter()
    payload = {"question": question}
    if isinstance(history, list):
        payload["history"] = history
    payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_obj = Request(
        base_url + "/api/ask",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request_obj, timeout=45) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body, time.perf_counter() - started


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * ratio))
    return ordered[index]


def evaluate(fixture_path: Path, case_ids: set[str] | None = None) -> dict:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    store = NoteStore(root / "data")
    embedding_engine = BgeEmbeddingEngine()
    if not embedding_engine.available:
        raise RuntimeError(f"BGE unavailable: {embedding_engine.unavailable_reason}")
    embedding = EmbeddingService(store, embedding_engine)
    llm = LlamaCppTextEngine()
    if not llm.runtime_status()["available"]:
        embedding.shutdown()
        store.close()
        temporary.cleanup()
        raise RuntimeError(f"LLM unavailable: {llm.runtime_status()['reason']}")
    intelligence = IntelligenceService(store, engine=llm)
    ocr = OcrService(store, UnavailableOcrEngine())
    server = None
    try:
        for document in fixture["documents"]:
            note = store.create_note(
                document["title"],
                document["content"],
                category=document.get("category", "其他"),
                subcategory=document.get("subcategory", ""),
            )
            embedding._process(note.id)
            # Fixture documents are already curated.  Do not let startup recovery
            # enqueue background classification jobs that contend with /api/ask.
            store.update_ai_status(note.id, "done", "")

        config = AppConfig(root / "config.json", store.data_dir)
        server = PocketMemoryServer(
            ("127.0.0.1", 0), store, config, lambda _: None,
            ocr_service=ocr, embedding_service=embedding, intelligence_service=intelligence,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        cases = []
        elapsed = []
        selected_cases = [
            case for case in fixture["cases"]
            if not case_ids or case["id"] in case_ids
        ]
        if not selected_cases:
            raise ValueError("没有匹配 --case 的评测题")
        total = len(selected_cases)
        for index, case in enumerate(selected_cases, 1):
            print(f"[{index}/{total}] {case['id']}", file=sys.stderr, flush=True)
            response, seconds = request(base_url, case["question"], case.get("history"))
            result = score_case(case, response.get("sources", []), response.get("answer", ""))
            result["kind"] = response.get("kind", result["kind"])
            result["elapsed_seconds"] = round(seconds, 3)
            cases.append(result)
            elapsed.append(seconds)
            print(f"  {seconds:.2f}s score={result['score']}", file=sys.stderr, flush=True)
        return {
            "fixture": fixture_path.name,
            "pipeline": "production_api",
            "model": llm.model_path.name,
            "average_score": round(sum(item["score"] for item in cases) / len(cases), 3),
            "latency_seconds": {
                "p50": round(statistics.median(elapsed), 3),
                "p95": round(percentile(elapsed, 0.95), 3),
                "max": round(max(elapsed), 3),
            },
            "cases": cases,
        }
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        else:
            ocr.shutdown()
            embedding.shutdown()
            intelligence.shutdown()
        store.close()
        temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the real Pocket Memory /api/ask RAG path.")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", help="只运行指定 case id；可重复传入")
    args = parser.parse_args()
    report = evaluate(args.fixture, set(args.case or []))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
