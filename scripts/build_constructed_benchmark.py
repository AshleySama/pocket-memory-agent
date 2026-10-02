# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Create a deterministic 100-question Chinese regression benchmark.

It reuses a de-identified fixture corpus and deliberately labels itself as
constructed. It is useful for repeatable engineering regressions, never as a
substitute for the real-note release gate.
"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "fixtures" / "rag_eval_business.json"
OUTPUT = ROOT / "tests" / "fixtures" / "rag_eval_constructed_100.json"


def case(identifier: str, scenario: str, question: str, titles: list[str], terms: list[str], **extra) -> dict:
    return {
        "id": identifier, "scenario": scenario, "question": question,
        "kind": "qa", "expected_titles": titles, "expected_answer_terms": terms, **extra,
    }


def main() -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    t = {
        "kri": "供应商A采购风控KRI阈值方案", "ocr": "FastAPI 后端 OCR 异常排查记录",
        "mysql": "MySQL 慢查询优化：笔记全文检索", "qwen": "Qwen3-0.6B 本地部署笔记",
        "bge": "BGE-small-zh 向量检索方案对比", "approval": "采购审批流重构方案",
        "async": "Python 异步任务最佳实践", "pack": "Vue3 + PyWebView 桌面打包踩坑",
        "learning": "2026 Q3 个人学习计划",
    }
    cases: list[dict] = []
    # 20 exact, auditable facts.
    exact = [
        ("供应商A红色预警阈值是多少？", [t["kri"]], ["> 10%", "暂停新订单"]),
        ("供应商A黄色预警的交货延迟率是多少？", [t["kri"]], ["> 5%"]),
        ("一次合格率连续几个月低于多少会启动整改？", [t["kri"]], ["< 95%", "2 个月"]),
        ("涉及金额超过多少需要提级到采购总监审批？", [t["kri"]], ["> 50 万"]),
        ("OCR 图片最长边要缩放到多少？", [t["ocr"]], ["2048"]),
        ("修复后单图 OCR 延迟范围是多少？", [t["ocr"]], ["1.5-2.5 秒"]),
        ("FTS5 在多少条笔记时性能异常？", [t["mysql"]], ["5000", "800ms"]),
        ("两字及以上关键词走什么检索方式？", [t["mysql"]], ["MATCH", "bm25"]),
        ("BGE-small-zh 的模型体积是多少？", [t["bge"]], ["95MB"]),
        ("BGE 线性扫描建议控制在多少条以内？", [t["bge"]], ["500", "50ms"]),
        ("RRF 的融合阈值是多少？", [t["bge"]], ["0.50"]),
        ("Qwen3 常驻内存大约是多少？", [t["qwen"]], ["900MB"]),
        ("Qwen3 分类准确率约多少？", [t["qwen"]], ["82%"]),
        ("采购审批当前平均耗时多少天？", [t["approval"]], ["5.2 天"]),
        ("采购审批重构后的目标耗时多少天？", [t["approval"]], ["2.5 天"]),
        ("自动风控评估的触发分数是多少？", [t["approval"]], ["<70 分"]),
        ("异步任务最多重试几次？", [t["async"]], ["3 次"]),
        ("异步任务重启时用哪个方法续跑？", [t["async"]], ["recover"]),
        ("PyInstaller 最终目录里有哪些核心项？", [t["pack"]], ["PocketMemory.exe", "_internal", "models"]),
        ("本周 RAG 学习安排在什么时间？", [t["learning"]], ["周一", "19:00-21:00"]),
    ]
    cases += [case(f"exact-{i:03}", "precise_parameter", q, titles, terms) for i, (q, titles, terms) in enumerate(exact, 1)]
    # 15 OCR noisy / paraphrased variants.
    ocr_questions = [
        "OCR大图为什么会500？", "图片太大时 OCR 为何挂掉？", "RapidOCR 遇到4K截图怎么处理？",
        "ocr 内存爆了要先做什么？", "Pillow 压图的目的是什么？", "检测模型为什么会分配2GB内存？",
        "识别图片异常，最长边应该限制到哪里？", "OCR 线程数为何设为2？", "识别失败时状态应该是什么？",
        "修复 OCR 后线上复现了吗？", "ocR 处理 10MB+ 图片失败的根因？", "图片识别卡死时如何避免请求挂住？",
        "Pillow缩放2048是在什么问题里用的？", "OCR 延迟从多少降到多少？", "RapidOcrEngine 默认有什么缺陷？",
    ]
    cases += [case(f"ocr-{i:03}", "ocr_noise", q, [t["ocr"]], ["OCR"]) for i, q in enumerate(ocr_questions, 1)]
    # 15 similar-concept distractors and wrong premises.
    confusion = [
        ("供应商KRI方案里的 FTS5 怎么优化？", [t["mysql"]], ["FTS5", "MATCH"], ["供应商A"]),
        ("Qwen3 向量检索方案为什么选 sqlite-vec？", [t["bge"]], ["BGE", "sqlite-vec"], ["Qwen3"]),
        ("菜谱里的 BGE 模型内存占用多少？", [t["bge"]], ["95MB"], ["番茄炒蛋"]),
        ("OCR 记录中 RRF 阈值是多少？", [t["bge"]], ["0.50"], ["OCR"]),
        ("采购审批如何用 bm25 排序？", [t["mysql"]], ["bm25"], ["采购审批"]),
    ] * 3
    cases += [case(f"confusion-{i:03}", "similar_concept_distraction", q, titles, terms, forbidden_terms=forbidden) for i, (q, titles, terms, forbidden) in enumerate(confusion, 1)]
    # 15 explicit no-answer questions.
    absent = [
        "Claude 和 GPT-4 在当前笔记里的对比如何？", "供应商B红色预警阈值是多少？",
        "MacBook 上的 OCR 速度是多少？", "BGE 是否使用 HNSW？", "Qwen3-4B 显存占用是多少？",
    ] * 3
    cases += [case(f"refusal-{i:03}", "no_answer", q, [], ["没有足够信息"], allow_insufficient=True) for i, q in enumerate(absent, 1)]
    # 10 multi-note comparisons, intentionally difficult.
    comparisons = [
        ("OCR 优化和 MySQL 优化分别采取了什么手段？", [t["ocr"], t["mysql"]], ["Pillow", "FTS5"]),
        ("Qwen3 与 BGE 选型时各自关注什么？", [t["qwen"], t["bge"]], ["Qwen3", "BGE"]),
        ("异步任务和审批流的自动化分别怎么做？", [t["async"], t["approval"]], ["自动", "状态"]),
        ("OCR 和向量检索的性能目标分别是多少？", [t["ocr"], t["bge"]], ["OCR", "50ms"]),
        ("打包和模型部署的内存或体积限制分别是什么？", [t["pack"], t["qwen"]], ["3GB", "900MB"]),
    ] * 2
    cases += [case(f"compare-{i:03}", "multi_note_comparison", q, titles, terms) for i, (q, titles, terms) in enumerate(comparisons, 1)]
    # 10 ambiguous, 10 follow-up, 5 time-oriented questions. The latter expose routing errors.
    ambiguous = ["最近模型方面有什么记录？", "有什么性能优化？", "我最近在学习什么？", "有哪些自动化方案？", "什么内容和检索有关？"] * 2
    cases += [case(f"ambiguous-{i:03}", "ambiguous_question", q, [], [], allow_insufficient=True) for i, q in enumerate(ambiguous, 1)]
    followups = [
        ("那它的阈值呢？", [{"role": "user", "content": "供应商A的交货延迟率红色预警是什么？"}], [t["kri"]], ["10%"]),
        ("那模型多大？", [{"role": "user", "content": "BGE-small-zh 向量检索方案有什么特点？"}], [t["bge"]], ["95MB"]),
    ] * 5
    cases += [dict(case(f"followup-{i:03}", "follow_up", q, titles, terms), history=history) for i, (q, history, titles, terms) in enumerate(followups, 1)]
    time_questions = ["2026 Q3 的采购审批计划是什么？", "2026 Q3 学习计划里有什么？", "Q2 供应商阈值是什么？", "最近的 OCR 修复记录是什么？", "本周 RAG 学习安排是什么？"]
    cases += [case(f"time-{i:03}", "time_query", q, [], [], allow_insufficient=True) for i, q in enumerate(time_questions, 1)]
    assert len(cases) == 100, len(cases)
    OUTPUT.write_text(json.dumps({"dataset": {"type": "constructed-regression", "version": 1}, "documents": source["documents"], "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUTPUT} with {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
