"""Build a transparent 100-question general-knowledge RAG benchmark.

Every fact is copied into the temporary Pocket Memory corpus together with a
source URL.  This makes the test a measurement of retrieval and grounded
answering, rather than a test of what the base model happens to remember.
"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "rag_eval_general_knowledge_100.json"


def fact(
    key: str,
    title: str,
    statement: str,
    answer: str,
    terms: list[str],
    questions: list[str],
    reference_url: str,
) -> dict:
    return {
        "key": key,
        "title": title,
        "content": f"核验事实：{statement}\n权威核验来源：{reference_url}",
        "answer": answer,
        "terms": terms,
        "questions": questions,
        "reference_url": reference_url,
    }


FACTS = [
    fact("light", "通识：真空光速", "真空中的光速 c 精确等于 299,792,458 米/秒。", "299,792,458 米/秒", ["299,792,458"], ["真空中的光速精确是多少？", "光在真空中每秒传播多少米？", "c 的国际单位制精确数值是什么？"], "https://physics.nist.gov/cgi-bin/cuu/Value?c"),
    fact("kilometre", "通识：千米与米", "1 千米等于 1,000 米。", "1,000 米", ["1,000", "米"], ["1 千米等于多少米？", "把 3.5 千米换算成米，换算关系是什么？", "km 和 m 的标准换算比例是多少？"], "https://www.nist.gov/pml/owm/si-units-length"),
    fact("day", "通识：日与小时", "1 日等于 24 小时。", "24 小时", ["24", "小时"], ["一天有多少小时？", "1 日应换算为几个小时？", "日和小时的换算关系是什么？"], "https://www.nist.gov/pml/owm/si-units-time"),
    fact("hour", "通识：小时与分钟", "1 小时等于 60 分钟。", "60 分钟", ["60", "分钟"], ["一小时有多少分钟？", "2.5 小时换算前，1 小时是多少分钟？", "小时转换成分钟的系数是多少？"], "https://www.nist.gov/pml/owm/si-units-time"),
    fact("kelvin", "通识：摄氏温度与开尔文", "摄氏温度 t 与热力学温度 T 的关系为 t = T - 273.15，0 摄氏度等于 273.15 K。", "0 摄氏度等于 273.15 K", ["273.15", "K"], ["0 摄氏度是多少开尔文？", "摄氏温度换算 K 时要加多少？", "冰点对应的热力学温度是多少？"], "https://www.nist.gov/pml/owm/si-units-temperature"),
    fact("earth", "通识：地球的太阳系位置", "地球是距离太阳第三近的行星。", "第三颗行星", ["第三", "行星"], ["地球是距离太阳第几颗行星？", "从太阳向外数，地球排第几？", "地球在太阳系行星序列中的位置是什么？"], "https://science.nasa.gov/earth/facts/"),
    fact("planets", "通识：太阳系行星数", "太阳系有八颗行星。", "八颗行星", ["八", "行星"], ["太阳系有多少颗行星？", "冥王星被归类为矮行星后，行星总数是多少？", "太阳系正式行星数量是几颗？"], "https://science.nasa.gov/solar-system/planets/"),
    fact("mars", "通识：火星的卫星", "火星有两颗卫星，名称是火卫一（Phobos）和火卫二（Deimos）。", "两颗：火卫一和火卫二", ["两", "火卫一", "火卫二"], ["火星有几颗卫星，分别叫什么？", "Phobos 和 Deimos 属于哪颗行星的卫星？", "火星的两颗天然卫星名称是什么？"], "https://science.nasa.gov/mars/moons/"),
    fact("jupiter", "通识：木星的大小", "木星是太阳系中最大的行星。", "最大的行星", ["最大", "行星"], ["太阳系最大的行星是哪一颗？", "哪颗行星的体积最大？", "木星在太阳系大小排名第几？"], "https://science.nasa.gov/jupiter/jupiter-facts/"),
    fact("ganymede", "通识：木卫三", "木卫三（Ganymede）是太阳系中最大的卫星，并且是木星的卫星。", "太阳系最大的卫星，是木星的卫星", ["最大", "卫星", "木星"], ["太阳系最大的卫星是哪一个？", "Ganymede 属于哪颗行星？", "木卫三有什么大小纪录？"], "https://science.nasa.gov/jupiter/moons/ganymede/"),
    fact("water", "通识：水的化学式", "水的化学式是 H2O，一个水分子由两个氢原子和一个氧原子组成。", "H2O（两个氢原子和一个氧原子）", ["H2O", "两个氢", "一个氧"], ["水的化学式是什么？", "一个水分子由哪些原子组成？", "H2O 中 H 和 O 的原子数分别是多少？"], "https://pubchem.ncbi.nlm.nih.gov/compound/Water"),
    fact("oxygen", "通识：氧元素原子序数", "氧元素（O）的原子序数是 8。", "8", ["8"], ["氧元素的原子序数是多少？", "元素 O 在周期表中的原子序数是几？", "氧有几个质子这一题应查哪个数字？"], "https://pubchem.ncbi.nlm.nih.gov/element/Oxygen"),
    fact("carbon", "通识：碳元素原子序数", "碳元素（C）的原子序数是 6。", "6", ["6"], ["碳元素的原子序数是多少？", "元素 C 在周期表中对应几号？", "碳原子的质子数是多少？"], "https://pubchem.ncbi.nlm.nih.gov/element/Carbon"),
    fact("triangle", "通识：三角形内角和", "在欧氏平面几何中，三角形三个内角的和是 180 度。", "180 度", ["180", "度"], ["平面三角形的内角和是多少度？", "一个三角形已知两角，第三角要用哪个固定总角度求？", "欧氏几何中三角形三内角相加为多少？"], "https://www.britannica.com/science/triangle-geometry"),
    fact("pythagoras", "通识：勾股定理", "直角三角形中，斜边平方等于两条直角边平方之和，即 c² = a² + b²。", "c² = a² + b²", ["c²", "a²", "b²"], ["勾股定理的公式是什么？", "直角三角形的斜边平方如何计算？", "a、b 是直角边时 c 应满足什么关系？"], "https://www.britannica.com/science/Pythagorean-theorem"),
    fact("circle", "通识：圆周长", "圆的周长等于 π 乘以直径，即 C = πd。", "C = πd", ["π", "d"], ["圆周长和直径的公式是什么？", "已知直径时如何计算圆的周长？", "C 与 d 的标准关系式是什么？"], "https://www.britannica.com/science/circumference"),
    fact("binary", "通识：二进制 1010", "二进制数 1010 等于十进制数 10。", "10", ["10"], ["二进制 1010 等于十进制多少？", "1010₂ 转换成十进制结果是几？", "二进制中的 8+2 写成什么十进制数字？"], "https://en.wikipedia.org/wiki/Binary_number"),
    fact("power", "通识：2 的十次方", "2 的 10 次方等于 1024。", "1024", ["1024"], ["2 的 10 次方是多少？", "1024 是 2 的几次方？", "计算机中常见的 2^10 数值是多少？"], "https://en.wikipedia.org/wiki/Power_of_two"),
    fact("beijing", "通识：中国首都", "中华人民共和国的首都是北京。", "北京", ["北京"], ["中国的首都是什么？", "中华人民共和国首都位于哪座城市？", "北京在国家行政首都这一事实中对应哪个国家？"], "https://www.gov.cn/guoqing/2019-09/27/content_5434190.htm"),
    fact("france", "通识：法国首都", "法国的首都是巴黎。", "巴黎", ["巴黎"], ["法国的首都是什么？", "Paris 是哪个国家的首都？", "法国首都的中文名称是什么？"], "https://www.britannica.com/place/Paris"),
]


def make_case(identifier: str, scenario: str, question: str, facts: list[dict], **extra: object) -> dict:
    return {
        "id": identifier,
        "scenario": scenario,
        "question": question,
        "kind": "qa",
        "expected_titles": [item["title"] for item in facts],
        "expected_answer_terms": [term for item in facts for term in item["terms"]],
        "gold_answer": "；".join(item["answer"] for item in facts),
        "reference_urls": [item["reference_url"] for item in facts],
        **extra,
    }


def main() -> int:
    documents = [{"title": item["title"], "content": item["content"], "category": "通识", "subcategory": "可核验事实", "reference_url": item["reference_url"]} for item in FACTS]
    cases: list[dict] = []
    for index, item in enumerate(FACTS, 1):
        cases.append(make_case(f"exact-{index:03}", "exact_fact", item["questions"][0], [item]))
    for index, item in enumerate(FACTS, 1):
        cases.append(make_case(f"paraphrase-{index:03}", "paraphrase", item["questions"][1], [item]))

    noisy_indexes = [0, 1, 4, 5, 6, 7, 10, 11, 13, 14, 15, 16, 17, 18, 19]
    for number, index in enumerate(noisy_indexes, 1):
        item = FACTS[index]
        noisy = item["questions"][2].replace("的", "d").replace("多少", "多 少")
        cases.append(make_case(f"ocr-{number:03}", "ocr_noise", noisy, [item]))

    pair_indexes = [(0, 1), (2, 3), (5, 7), (6, 8), (10, 11), (11, 12), (13, 14), (15, 16), (17, 18), (18, 19)]
    for number, (left, right) in enumerate(pair_indexes, 1):
        first, second = FACTS[left], FACTS[right]
        question = f"请分别说明：{first['questions'][0]}；{second['questions'][0]}"
        cases.append(make_case(f"compare-{number:03}", "multi_note_comparison", question, [first, second]))

    absent = [
        "这份通识笔记里，人体有多少块骨头？", "这份通识笔记里，珠穆朗玛峰多高？",
        "这份通识笔记里，莎士比亚出生在哪一年？", "这份通识笔记里，澳大利亚首都是什么？",
        "这份通识笔记里，DNA 的全称是什么？", "这份通识笔记里，元素铁的原子序数是多少？",
        "这份通识笔记里，海王星有多少颗卫星？", "这份通识笔记里，法国人口是多少？",
        "这份通识笔记里，圆的面积公式是什么？", "这份通识笔记里，水的沸点是多少摄氏度？",
    ]
    for number, question in enumerate(absent, 1):
        cases.append({"id": f"refusal-{number:03}", "scenario": "no_answer", "question": question, "kind": "qa", "expected_titles": [], "expected_answer_terms": ["没有足够信息"], "gold_answer": "应明确说明：现有笔记没有足够信息，不能作答。", "reference_urls": [], "allow_insufficient": True})

    followup_specs = [
        (5, "那它是距离太阳第几颗行星？"),
        (10, "那它的化学式是什么？"),
        (14, "那条公式怎么写？"),
        (17, "那它的数值是多少？"),
        (18, "那首都叫什么？"),
    ]
    for number, (index, question) in enumerate(followup_specs * 2, 1):
        item = FACTS[index]
        cases.append(make_case(f"followup-{number:03}", "follow_up", question, [item], history=[{"role": "user", "content": item["questions"][0]}]))

    ambiguous = [
        ("有哪些天文事实？", [FACTS[5], FACTS[6], FACTS[8]], ["第三", "八", "最大"]),
        ("有哪些单位换算？", [FACTS[1], FACTS[2], FACTS[3]], ["1,000", "24", "60"]),
        ("有哪些数学公式？", [FACTS[13], FACTS[14], FACTS[15]], ["180", "c²", "π"]),
        ("有哪些化学信息？", [FACTS[10], FACTS[11], FACTS[12]], ["H2O", "8", "6"]),
        ("有哪些国家首都？", [FACTS[18], FACTS[19]], ["北京", "巴黎"]),
    ]
    for number, (question, relevant, terms) in enumerate(ambiguous, 1):
        cases.append(make_case(f"ambiguous-{number:03}", "ambiguous_question", question, relevant, expected_answer_terms=terms, gold_answer="应明确限定为笔记中覆盖的范围，列出相关事实且不编造。"))

    distractors = [
        ("氧元素的原子序数是 6 吗？", 11), ("火星是最大的行星吗？", 8),
        ("法国的首都是北京吗？", 19), ("水的化学式是 CO2 吗？", 10),
        ("地球距离太阳第二近吗？", 5), ("二进制 1010 等于 8 吗？", 16),
        ("一个水分子有两个氧原子吗？", 10), ("木卫三是火星的卫星吗？", 9),
        ("三角形内角和是 360 度吗？", 13), ("一日有 60 小时吗？", 2),
    ]
    for number, (question, index) in enumerate(distractors, 1):
        item = FACTS[index]
        cases.append(make_case(f"distractor-{number:03}", "false_premise", question, [item]))

    assert len(cases) == 100, len(cases)
    payload = {
        "dataset": {
            "type": "transparent-general-knowledge-rag-benchmark",
            "version": 1,
            "description": "100 条可核验通识题；测 RAG 是否基于写入的笔记回答，不测模型自身记忆。",
            "limitations": "这是公开通识回归集，不能替代至少 100 条脱敏真实笔记的上线验收集。",
        },
        "documents": documents,
        "cases": cases,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUTPUT} with {len(documents)} documents and {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
