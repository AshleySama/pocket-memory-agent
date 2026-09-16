from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_memory.embedding import BgeEmbeddingEngine
from pocket_memory.ocr import RapidOcrEngine


def main() -> int:
    ocr = RapidOcrEngine()
    print("OCR available:", ocr.available)
    print("OCR reason:", ocr.unavailable_reason)
    if not ocr.available:
        return 1

    embedding = BgeEmbeddingEngine()
    print("Embedding available:", embedding.available)
    print("Embedding reason:", embedding.unavailable_reason)
    if not embedding.available:
        return 1

    query = embedding.embed("供应商异常风险")
    related = embedding.embed("采购过程中需要识别外部合作方风险")
    unrelated = embedding.embed("FastAPI 接口开发备忘")
    related_score = float(np.dot(query, related))
    unrelated_score = float(np.dot(query, unrelated))
    print("Related score:", related_score)
    print("Unrelated score:", unrelated_score)
    if related_score <= unrelated_score:
        raise AssertionError("Semantic model did not rank the related note first")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
