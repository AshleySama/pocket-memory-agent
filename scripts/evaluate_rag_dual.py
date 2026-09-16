from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_rag import load_fixture, run_custom, score_case
from pocket_memory.storage import NoteStore


def build_fixture_store(fixture: dict):
    temp = tempfile.TemporaryDirectory()
    store = NoteStore(Path(temp.name) / "data")
    try:
        for document in fixture["documents"]:
            store.create_note(document["title"], document["content"], category="学习", subcategory="古诗")
    finally:
        store.close()
    return temp, Path(temp.name) / "data"


def run_llama_index_case(data_dir: Path, question: str) -> dict:
    python = ROOT / ".venv-rag-frameworks" / "Scripts" / "python.exe"
    script = ROOT / "scripts" / "llamaindex_query.py"
    if not python.is_file():
        raise RuntimeError("Missing .venv-rag-frameworks; install LlamaIndex experiment dependencies first.")
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("HAYSTACK_TELEMETRY_ENABLED", "False")
    env.setdefault("HAYSTACK_HOME", str(ROOT / ".haystack-test"))
    completed = subprocess.run(
        [
            str(python),
            str(script),
            "--data-dir",
            str(data_dir),
            "--question",
            question,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}")
    lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise RuntimeError(completed.stdout.strip() or "LlamaIndex returned no JSON")
    return json.loads(lines[-1])


def run_llama_index(fixture: dict) -> dict:
    temp, data_dir = build_fixture_store(fixture)
    try:
        cases = []
        for case in fixture["cases"]:
            payload = run_llama_index_case(data_dir, case["question"])
            cases.append(score_case(case, payload.get("sources", []), payload.get("answer", "")))
        average = sum(item["score"] for item in cases) / len(cases)
        return {"name": "llama_index_bge_experiment", "average_score": round(average, 3), "cases": cases}
    finally:
        temp.cleanup()


def main() -> int:
    fixture = load_fixture(ROOT / "tests" / "fixtures" / "rag_eval_cases.json")
    report = {
        "custom": run_custom(fixture),
        "llama_index_bge": run_llama_index(fixture),
    }
    output = ROOT / "reports" / "rag_dual_eval_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
