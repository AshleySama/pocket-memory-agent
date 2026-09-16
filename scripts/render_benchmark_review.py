"""Render a human-auditable report for an API RAG benchmark result."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成逐题可核验的 RAG 测试报告")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    result = json.loads(args.result.read_text(encoding="utf-8"))
    definitions = {item["id"]: item for item in fixture["cases"]}
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in result["cases"]:
        groups[definitions[item["id"]]["scenario"]].append(item)

    lines = [
        "# 通识知识 RAG 测试报告", "",
        "## 口径", "",
        "- 本报告逐题展示金标准、模型最终回答、返回笔记和来源网址；可直接核验，不把综合分数当成黑盒结论。",
        "- 题库只测系统能否从预置笔记检索并忠实回答，不测模型是否依赖预训练知识答对。",
        "- 公开通识集是工程回归测试，不能代替真实脱敏笔记的上线验收。", "",
        "## 汇总", "",
        f"- 题目数：{len(result['cases'])}",
        f"- 代理分数：{result['average_score']}",
        f"- 延迟：P50 {result['latency_seconds']['p50']} 秒，P95 {result['latency_seconds']['p95']} 秒，最大 {result['latency_seconds']['max']} 秒", "",
        "| 场景 | 题数 | 平均代理分 |", "| --- | ---: | ---: |",
    ]
    for scenario in sorted(groups):
        values = groups[scenario]
        average = sum(item["score"] for item in values) / len(values)
        lines.append(f"| {scenario} | {len(values)} | {average:.3f} |")

    lines += ["", "## 逐题审计", ""]
    for item in result["cases"]:
        case = definitions[item["id"]]
        links = " ".join(f"[来源 {index + 1}]({url})" for index, url in enumerate(case.get("reference_urls", []))) or "无（应拒答或澄清）"
        sources = "；".join(item.get("sources", [])) or "无"
        lines += [
            f"### {case['id']} · {case['scenario']}", "",
            f"- 问题：{case['question']}",
            f"- 金标准：{case.get('gold_answer', '未设置')}",
            f"- 核验：{links}",
            f"- 系统返回笔记：{sources}",
            f"- 模型回答：{item.get('answer', '')}",
            f"- 代理评分：{item['score']}；耗时：{item.get('elapsed_seconds', 'n/a')} 秒", "",
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
