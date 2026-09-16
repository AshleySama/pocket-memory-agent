"""阶段 1 验证脚本：绕过 fallback_answer，直接用 LLM 答题，测真实水平。

用法：
    python scripts/evaluate_rag_llm.py --fixture tests/fixtures/rag_eval_constructed_100.json
    python scripts/evaluate_rag_llm.py --fixture tests/fixtures/rag_eval_cases.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pocket_memory.embedding import BgeEmbeddingEngine, EmbeddingService
from pocket_memory.intelligence import LlamaCppTextEngine
from pocket_memory.rag import RagPipeline
from pocket_memory.storage import NoteStore
from scripts.evaluate_rag import score_case


class LexicalEmbeddingEngine:
    """无模型依赖的词法嵌入，用于在没有 BGE 模型时也能跑评测。"""

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
    # 优先用真实 BGE，没有则回退到词法嵌入
    try:
        embed_engine = BgeEmbeddingEngine()
        if not embed_engine.available:
            raise RuntimeError("BGE 不可用")
    except Exception:
        print("[warn] BGE 不可用，回退到词法嵌入（检索质量会下降）", file=sys.stderr)
        embed_engine = LexicalEmbeddingEngine()
    embedding = EmbeddingService(store, embed_engine)
    for document in fixture["documents"]:
        note = store.create_note(
            document["title"],
            document["content"],
            category=document.get("category", "其他"),
            subcategory=document.get("subcategory", ""),
        )
        embedding._process(note.id)
    return temp, store, embedding


def run_llm_direct(fixture: dict, llm_engine: LlamaCppTextEngine) -> dict:
    temp, store, embedding = build_store(fixture)
    try:
        pipeline = RagPipeline(store, embedding)
        results = []
        for case in fixture["cases"]:
            chunks = pipeline.retrieve(case["question"], limit=8)
            contexts = pipeline.contexts(chunks)
            sources = pipeline.source_payloads(case["question"], chunks)
            if not contexts:
                answer = "没有找到足够相似的笔记，暂时无法基于笔记回答。"
            else:
                # 关键：直接调 LLM，不走 fallback_answer
                try:
                    answer = llm_engine.answer(case["question"], contexts)
                except Exception as exc:
                    answer = f"[LLM 调用失败: {exc}]"
            results.append(score_case(case, sources, answer))
        average = sum(item["score"] for item in results) / len(results)
        return {"name": "llm_direct", "average_score": round(average, 3), "cases": results}
    finally:
        embedding.shutdown()
        store.close()
        temp.cleanup()


def run_fallback_baseline(fixture: dict) -> dict:
    """对照：原来的 fallback_answer 分数（不调 LLM）。"""
    temp, store, embedding = build_store(fixture)
    try:
        pipeline = RagPipeline(store, embedding)
        results = []
        for case in fixture["cases"]:
            chunks = pipeline.retrieve(case["question"], limit=8)
            sources = pipeline.source_payloads(case["question"], chunks)
            answer = RagPipeline.fallback_answer(case["question"], sources)
            results.append(score_case(case, sources, answer))
        average = sum(item["score"] for item in results) / len(results)
        return {"name": "fallback_only", "average_score": round(average, 3), "cases": results}
    finally:
        embedding.shutdown()
        store.close()
        temp.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 1 验证：LLM 直接答题 vs fallback 规则。")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--skip-fallback", action="store_true", help="跳过 fallback 对照（省时间）")
    args = parser.parse_args()

    fixture = load_fixture(args.fixture)
    print(f"评测集: {args.fixture.name}", file=sys.stderr)
    print(f"文档数: {len(fixture['documents'])}, 题目数: {len(fixture['cases'])}", file=sys.stderr)

    llm_engine = LlamaCppTextEngine()
    if not llm_engine.available:
        print(f"[error] LLM 不可用: {llm_engine.unavailable_reason}", file=sys.stderr)
        return 1
    print(f"LLM: {llm_engine.model_path.name}", file=sys.stderr)

    report = {"fixture": args.fixture.name}

    print("\n[1/2] 跑 LLM 直接答题...", file=sys.stderr)
    report["llm_direct"] = run_llm_direct(fixture, llm_engine)
    print(f"  平均分: {report['llm_direct']['average_score']}", file=sys.stderr)

    if not args.skip_fallback:
        print("\n[2/2] 跑 fallback 规则对照...", file=sys.stderr)
        report["fallback_only"] = run_fallback_baseline(fixture)
        print(f"  平均分: {report['fallback_only']['average_score']}", file=sys.stderr)
        delta = report["llm_direct"]["average_score"] - report["fallback_only"]["average_score"]
        print(f"\n差异: {delta:+.3f}（正数=LLM 更好，负数=fallback 更好）", file=sys.stderr)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
