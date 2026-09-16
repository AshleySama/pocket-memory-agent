from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_memory.intelligence import LlamaCppTextEngine


def main() -> int:
    engine = LlamaCppTextEngine()
    print("Text understanding available:", engine.available)
    print("Text understanding reason:", engine.unavailable_reason)
    started = time.time()
    result = engine.classify("发现供应商存在外部风险，需要检查采购审批流程和异常交易。")
    print("Text understanding seconds:", round(time.time() - started, 2))
    print("Text understanding result:", json.dumps(result, ensure_ascii=True))
    if result["category"] not in {"工作", "技术", "学习", "生活", "灵感", "其他"}:
        raise AssertionError("Text understanding returned an invalid category")
    if len(result["tags"]) < 1:
        raise AssertionError("Text understanding returned no tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

