# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Conservative references for clarifying an unnamed subject.

These references are separate from factual citations. Only verified original
table records in the exact model input can establish the possible objects; a
retrieval hit, model example, or source title alone cannot do so.
"""
from __future__ import annotations

import copy
import re


_NAME_HEADERS = {"名称", "名字", "对象", "项目", "产品", "工具", "系统", "方案", "合同", "agent"}
_MULTIPLE = re.compile(r"多个|多种|不同|不止一个|不唯一|无法唯一")
_CLARIFY = re.compile(
    r"(?:未明确|未指定|未说明|没有明确|没有指定|缺少|不清楚|尚不明确).{0,24}(?:对象|主体|具体|名称)"
    r"|请.{0,10}(?:明确|说明|提供|指定|确认).{0,24}(?:对象|名称|哪[一]?个|具体)"
    r"|(?:需|需要).{0,10}(?:确认|明确).{0,20}(?:哪[一]?个|对象|名称)"
)


def _compact(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _cells(line):
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        return []
    return [cell.strip().strip("*`") for cell in line[1:-1].split("|")]


def _records(source):
    """Read explicit name/attribute columns, never infer names from prose."""
    headers = None
    name_index = None
    records = []
    for line in str(source.get("evidence", "")).splitlines():
        cells = _cells(line)
        if len(cells) < 2 or all(re.fullmatch(r"[:\-\s]+", cell) for cell in cells):
            continue
        names = [i for i, cell in enumerate(cells)
                 if _compact(cell) in _NAME_HEADERS or _compact(cell).endswith("名称")]
        if len(names) == 1:
            headers, name_index = cells, names[0]
            continue
        if headers is not None and len(cells) == len(headers):
            name = cells[name_index]
            if name and name not in {"—", "-", "无", "未命名"}:
                records.append((headers[name_index], name, dict(zip(headers, cells))))
    return records


def _missing_subject_fields(question, name_header, values):
    """Accept only a bare attribute question, including an unresolved pronoun.

    A subject recovered by the existing conversation resolver remains in the
    question, so it cannot pass this grammar. Unknown residual words fail closed.
    """
    compact = _compact(question)
    if re.search(r"哪些|分别|各自|全部|所有|每个|列出|清单|这些|那些", compact):
        return []
    fields = [field for field in values if field != name_header
              and len(_compact(field)) >= 2 and _compact(field) in compact]
    if not fields:
        return []
    rest = compact
    for field in sorted(fields, key=len, reverse=True):
        rest = rest.replace(_compact(field), "")
    # Generic type labels may be present, but a concrete object never is.
    category = _compact(name_header).removesuffix("名称")
    if category and category not in {"名", "对"}:
        rest = rest.replace(category, "")
    rest = re.sub(r"请问|请介绍|请说明|介绍一下|说明一下|告诉我|它|这个|那个|其|的|以及|和|与|及|、|是什么|有哪些|如何|怎样|什么|呢|吗|[?？。！!，,：:；;（）()“”\"']", "", rest)
    return fields if not rest else []


def _supplied_context(response):
    trace = response.get("trace") or {}
    registry = trace.get("evidence_registry") or {}
    if registry.get("prompt_checked") is not True or registry.get("deterministic_or_structured_path"):
        return "", {}
    messages = trace.get("messages") or []
    users = [message.get("content") for message in messages if message.get("role") == "user"]
    if len(users) != 1 or not isinstance(users[0], str):
        return "", {}
    if not users[0].startswith("参考资料：\n") or "\n\n问题：" not in users[0]:
        return "", {}
    return users[0][len("参考资料：\n"):].split("\n\n问题：", 1)[0], registry


def _verified_block(source, context, registry):
    index = source.get("index")
    identity = source.get("citation_source_id")
    if not identity or registry.get("supplied_to_identity", {}).get(str(index)) != identity:
        return ""
    if source.get("source_type") not in {"note", "document"} or source.get("source_id") is None:
        return ""
    members = source.get("citation_members") or []
    if not members or source.get("citation_provenance", {}).get("issues"):
        return ""
    for member in members:
        if (member.get("verification") != "exact_saved_original"
                or member.get("source_type") != source["source_type"]
                or member.get("source_id") != source["source_id"] or not member.get("spans")):
            return ""
    markers = list(re.finditer(r"(?m)^\[(\d+)\] ([^\n]* · [^\n]*)\n", context))
    matches = [i for i, marker in enumerate(markers) if marker.group(1) == str(index)]
    if len(matches) != 1:
        return ""
    i = matches[0]
    end = markers[i + 1].start() if i + 1 < len(markers) else len(context)
    block = context[markers[i].start():end]
    prompt = source.get("prompt_block")
    if prompt:
        return block if block.startswith(prompt) else ""
    header = f'[{index}] {source.get("title", "")} · '
    evidence = source.get("evidence")
    return block if evidence and block.startswith(header) and evidence in block else ""


def _examples_supported(answer, names):
    for match in re.finditer(r"(?:例如|比如|如[:：])\s*([^）)。；;\n]+)", answer):
        rest = _compact(match.group(1))
        for name in sorted(names, key=len, reverse=True):
            rest = rest.replace(name, "")
        if re.sub(r"以及|等|和|与|[、,，:：/（）()\s]", "", rest):
            return False
    return True


def clarification_sources(question, response):
    """Return source-bound alternatives without changing answer or citations."""
    answer = str(response.get("answer") or "")
    if response.get("sources") or not _CLARIFY.search(answer) or re.search(r"\[\d+\]", answer):
        return []
    context, registry = _supplied_context(response)
    if not context:
        return []
    groups = {}
    for source in response.get("provided_sources") or []:
        block = _verified_block(source, context, registry)
        if not block:
            continue
        for name_header, name, values in _records(source):
            fields = _missing_subject_fields(question, name_header, values)
            if not fields:
                continue
            # The serialized model block, not merely the original table, must
            # contain this exact object and the requested original field values.
            if name not in block or any(not values[field] or values[field] not in block for field in fields):
                continue
            key = (_compact(name_header), tuple(sorted(_compact(field) for field in fields)))
            groups.setdefault(key, []).append((source, name_header, name, fields))

    accepted = []
    compact_answer = _compact(answer)
    for records in groups.values():
        names = {_compact(record[2]) for record in records}
        if len(names) < 2 or not _examples_supported(answer, names):
            continue
        mentioned = {name for name in names if name in compact_answer}
        category = _compact(records[0][1]).removesuffix("名称")
        # Concrete examples, or the explicit common type plus plurality, tie
        # this set to the model's clarification. Other tables stay unrelated.
        if len(mentioned) < 2 and not (category and category in compact_answer and _MULTIPLE.search(answer)):
            continue
        accepted.extend(records)

    selected = {}
    for source, name_header, name, fields in accepted:
        identity = source["citation_source_id"]
        if identity not in selected:
            out = copy.deepcopy(source)
            out.update(reference_role="clarification", clarification_entities=[], clarification_fields=fields)
            selected[identity] = out
        if name not in selected[identity]["clarification_entities"]:
            selected[identity]["clarification_entities"].append(name)
    return list(selected.values())
