from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("HAYSTACK_TELEMETRY_ENABLED", "False")
os.environ.setdefault("HAYSTACK_HOME", str(ROOT / ".haystack-test"))

from pocket_memory.rag import RagPipeline
from scripts.evaluate_rag import (
    answer_from_sources,
    load_fixture,
    run_custom,
    score_case,
)


def lexical_vector(text: str, dimension: int = 128) -> list[float]:
    import hashlib

    vector = [0.0] * dimension
    text = str(text or "")
    for token in set(text[index:index + 2] for index in range(max(0, len(text) - 1))):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        vector[digest[0] % dimension] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def segment_title(default_title: str, text: str) -> str:
    import re

    first_line = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    match = re.search(r"《[^》]+》[^\n，。；;]*", first_line)
    return match.group(0).strip() if match else default_title


def fixture_segments(fixture: dict) -> list[dict]:
    segments = []
    for document in fixture["documents"]:
        for chunk in RagPipeline.split_text(document["content"]):
            segments.append(
                {
                    "id": f"{document['title']}::{len(segments)}",
                    "document_title": document["title"],
                    "title": segment_title(document["title"], chunk),
                    "text": chunk,
                }
            )
    return segments


def source_from_hit(index: int, title: str, text: str, score: float | None = None) -> dict:
    return {
        "index": index,
        "id": index,
        "title": title,
        "score": float(score or 0.0),
        "reason": "framework adapter",
        "excerpt": text,
        "evidence": "",
        "url": "",
        "location": "framework",
        "matched_terms": [],
        "query_kind": "framework",
    }


def evaluate_framework(name: str, fixture: dict, retrieve) -> dict:
    start = time.perf_counter()
    cases = []
    for case in fixture["cases"]:
        sources = retrieve(case["question"])
        answer = answer_from_sources(case["question"], sources)
        cases.append(score_case(case, sources, answer))
    elapsed = time.perf_counter() - start
    average = sum(item["score"] for item in cases) / len(cases)
    return {
        "name": name,
        "average_score": round(average, 3),
        "elapsed_seconds": round(elapsed, 3),
        "cases": cases,
    }


def run_llama_index(fixture: dict) -> dict:
    from llama_index.core import Document, Settings, VectorStoreIndex
    from llama_index.core.base.embeddings.base import BaseEmbedding

    class LexicalLlamaEmbedding(BaseEmbedding):
        @classmethod
        def class_name(cls) -> str:
            return "LexicalLlamaEmbedding"

        def _get_query_embedding(self, query: str) -> list[float]:
            return lexical_vector(query)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return lexical_vector(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return lexical_vector(text)

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return lexical_vector(text)

    # LlamaIndex core needs an embedding model; this keeps the experiment offline
    # and uses the same deterministic lexical embedding as the custom/Haystack runs.
    Settings.embed_model = LexicalLlamaEmbedding(model_name="local-lexical")
    Settings.llm = None

    segments = fixture_segments(fixture)
    documents = [
        Document(text=segment["text"], metadata={"title": segment["title"], "id": segment["id"]})
        for segment in segments
    ]
    start = time.perf_counter()
    index = VectorStoreIndex.from_documents(documents)
    build_seconds = time.perf_counter() - start
    retriever = index.as_retriever(similarity_top_k=8)

    def retrieve(question: str) -> list[dict]:
        hits = retriever.retrieve(question)
        sources = []
        for index, hit in enumerate(hits, start=1):
            node = hit.node
            title = node.metadata.get("title", "LlamaIndex source")
            sources.append(source_from_hit(index, title, node.get_content(), hit.score))
        return sources

    result = evaluate_framework("llama_index_core_minimal", fixture, retrieve)
    result["build_seconds"] = round(build_seconds, 3)
    result["notes"] = "Uses llama-index-core VectorStoreIndex with the same deterministic lexical embedding; no external model/API."
    return result


def run_haystack(fixture: dict) -> dict:
    from haystack import Document
    from haystack.components.retrievers.in_memory import InMemoryBM25Retriever, InMemoryEmbeddingRetriever
    from haystack.document_stores.in_memory import InMemoryDocumentStore

    segments = fixture_segments(fixture)
    store = InMemoryDocumentStore(embedding_similarity_function="cosine")
    documents = [
        Document(
            content=segment["text"],
            meta={"title": segment["title"], "id": segment["id"]},
            embedding=lexical_vector(segment["text"]),
        )
        for segment in segments
    ]
    start = time.perf_counter()
    store.write_documents(documents)
    build_seconds = time.perf_counter() - start
    bm25 = InMemoryBM25Retriever(document_store=store, top_k=8)
    dense = InMemoryEmbeddingRetriever(document_store=store, top_k=8)

    def retrieve(question: str) -> list[dict]:
        merged = []
        seen = set()
        for doc in bm25.run(query=question, top_k=8)["documents"]:
            key = doc.meta.get("id")
            if key not in seen:
                merged.append(doc)
                seen.add(key)
        for doc in dense.run(query_embedding=lexical_vector(question), top_k=8)["documents"]:
            key = doc.meta.get("id")
            if key not in seen:
                merged.append(doc)
                seen.add(key)
        sources = []
        for index, doc in enumerate(merged[:8], start=1):
            sources.append(source_from_hit(index, doc.meta.get("title", "Haystack source"), doc.content, doc.score))
        return sources

    result = evaluate_framework("haystack_inmemory_bm25_plus_embedding", fixture, retrieve)
    result["build_seconds"] = round(build_seconds, 3)
    result["notes"] = "Uses Haystack InMemoryDocumentStore with BM25 + custom lexical embeddings; no external model/API."
    return result


def dependency_snapshot() -> dict:
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    size = 0
    if site_packages.exists():
        for path in site_packages.rglob("*"):
            if path.is_file():
                size += path.stat().st_size
    freeze = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    return {
        "python": sys.executable,
        "site_packages_mb": round(size / 1024 / 1024, 1),
        "package_count": len(freeze),
        "key_packages": [
            line for line in freeze
            if line.lower().startswith(("llama-index", "haystack", "openai", "nltk", "tiktoken", "posthog"))
        ],
    }


def main() -> int:
    fixture = load_fixture(ROOT / "tests" / "fixtures" / "rag_eval_cases.json")
    report = {
        "custom": run_custom(fixture),
        "llama_index": run_llama_index(fixture),
        "haystack": run_haystack(fixture),
        "dependency_snapshot": dependency_snapshot(),
    }
    output = ROOT / "reports" / "framework_rag_eval.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
