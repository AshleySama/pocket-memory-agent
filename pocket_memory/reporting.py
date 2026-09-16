from __future__ import annotations

import re
from dataclasses import dataclass

from pocket_memory.chunking import split_note_text


REPORT_TEMPLATES = {
    "adaptive": {
        "label": "智能归纳",
        "description": "不预设章节，按资料内容自动组织为一篇可核验专题。",
        "writing_focus": "先识别资料真正讨论的主题和关系，再安排 2-4 个自然章节；适合来源类型混合、主题尚不明确的资料。",
    },
    "project": {
        "label": "项目复盘",
        "description": "梳理背景、进展、决策与下一步。",
        "writing_focus": "优先呈现背景与目标、已完成的进展、关键取舍或风险，以及资料中明确的下一步；不必凑齐这些栏目。",
    },
    "proposal": {
        "label": "方案介绍",
        "description": "将零散资料组织为可分享的方案说明。",
        "writing_focus": "优先解释要解决的问题、方法和实施条件；只有资料明确体现能力或差异时才写亮点，测试要求不能被包装成产品亮点。",
    },
    "meeting": {
        "label": "会议纪要",
        "description": "聚焦结论、行动项、责任与待确认事项。",
        "writing_focus": "优先区分已确认事实、行动安排与待确认事项；只有资料明确给出责任人或时间时才写入。",
    },
}

MAX_SOURCES = 8
MAX_CANDIDATES_PER_SOURCE = 4
MAX_EVIDENCE = 10
MAX_EVIDENCE_CHARS = 1800
MAX_BLOCK_CHARS = 420
MIN_PASSAGE_CHARS = 10


@dataclass(frozen=True)
class ReportEvidence:
    key: str
    source_kind: str
    source_id: int
    source_label: str
    location: str
    text: str
    target_note_id: int | None = None
    chunk_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "location": self.location,
            "text": self.text,
            "target_note_id": self.target_note_id,
            "chunk_id": self.chunk_id,
        }


class ReportService:
    """Create a source-bound, claim-level report draft.

    The local model may paraphrase only when it also returns a verbatim quote
    from supplied evidence. The service validates that quote before a claim
    reaches the UI, so a pretty report is not allowed to hide an unsupported
    assertion.
    """

    def __init__(self, store, engine) -> None:
        self.store = store
        self.engine = engine

    @staticmethod
    def templates() -> list[dict]:
        return [{"id": key, **value} for key, value in REPORT_TEMPLATES.items()]

    def create_draft(self, title: str, template: str, source_refs: list[dict]) -> dict:
        clean_title = str(title or "").strip()[:100]
        if not clean_title:
            raise ValueError("请输入专题名称")
        if template not in REPORT_TEMPLATES:
            raise ValueError("不支持的报告模板")
        sources, evidence = self._collect_sources(source_refs, clean_title)
        if not sources:
            raise ValueError("请至少选择 1 项资料")
        if len(evidence) < 2:
            raise ValueError("所选资料缺少可用于编排的正文内容")
        if not getattr(self.engine, "available", False):
            raise RuntimeError(getattr(self.engine, "unavailable_reason", "智能模型尚未就绪"))

        generated = self.engine.generate_report_claims(
            clean_title, REPORT_TEMPLATES[template], [item.as_dict() for item in evidence]
        )
        claims = self._validate_claims(generated, evidence)
        if len(claims) < 2:
            claims = self._supplement_with_source_facts(claims, evidence)
        claim_count = len(claims)
        if claim_count < 2:
            raise RuntimeError(
                "所选资料未形成足够的可核验专题内容。请补充更完整的资料，或更换报告模板后重试。"
            )
        layout = self._build_layout(clean_title, REPORT_TEMPLATES[template], claims)
        sections = self._materialize_sections(layout["sections"], claims)
        cited_sources = {claim["source_id"] for section in sections for claim in section["claims"]}
        return {
            "title": clean_title,
            "template": template,
            "template_label": REPORT_TEMPLATES[template]["label"],
            "sources": sources,
            "summary": layout["summary"],
            "sections": sections,
            "quality": {
                "claim_count": claim_count,
                "source_coverage": len(cited_sources),
                "selected_source_count": len(sources),
                "notice": "摘要和章节仅基于已核验事实编排；每条事实均保留可定位原文引句。",
            },
        }

    def _collect_sources(self, source_refs: list[dict], title: str) -> tuple[list[dict], list[ReportEvidence]]:
        if not isinstance(source_refs, list):
            raise ValueError("资料选择格式无效")
        seen: set[tuple[str, int]] = set()
        sources: list[dict] = []
        candidates_by_source: list[list[ReportEvidence]] = []
        for ref in source_refs:
            kind = str(ref.get("kind") or "")
            try:
                source_id = int(ref.get("id"))
            except (TypeError, ValueError):
                continue
            identity = (kind, source_id)
            if identity in seen or len(sources) >= MAX_SOURCES:
                continue
            seen.add(identity)
            if kind == "note":
                note = self.store.get_note(source_id)
                if note.source_type == "welcome":
                    continue
                source = {"kind": kind, "id": note.id, "label": self._clean_source_label(note.title), "updated_at": note.updated_at}
                source_evidence = self._note_evidence(note)
            elif kind == "document":
                document = self.store.get_document(source_id)
                source = {"kind": kind, "id": document.id, "label": self._clean_source_label(document.original_name), "updated_at": document.updated_at}
                source_evidence = self._document_evidence(document)
            else:
                continue
            sources.append(source)
            candidates_by_source.append(self._select_source_evidence(source_evidence, title))

        # Round-robin keeps a large document from crowding out other selected
        # materials and makes coverage of the selected sources predictable.
        evidence: list[ReportEvidence] = []
        evidence_chars = 0
        cursor = 0
        while len(evidence) < MAX_EVIDENCE and any(cursor < len(items) for items in candidates_by_source):
            for items in candidates_by_source:
                if cursor >= len(items) or len(evidence) >= MAX_EVIDENCE:
                    continue
                item = items[cursor]
                # Keep enough room for the prompt contract and 4B's structured
                # output. The first short passage of a selected source is still
                # admitted, so the budget does not silently erase a source.
                estimated = len(item.text) + 50
                if evidence and evidence_chars + estimated > MAX_EVIDENCE_CHARS:
                    continue
                evidence.append(item)
                evidence_chars += estimated
            cursor += 1
        return sources, evidence

    def _note_evidence(self, note) -> list[ReportEvidence]:
        result: list[ReportEvidence] = []
        for tab in self.store.get_note_tabs(note.id):
            tab_note = self.store.get_note(tab["id"])
            chunks = self.store.list_note_chunks(tab_note.id)
            if not chunks:
                chunks = [type("TextChunk", (), {"id": None, "location": "正文", "text": text}) for text in split_note_text(tab_note.content)]
            for chunk in chunks:
                label = self._clean_source_label(note.title)
                if tab_note.id != note.id:
                    label = f"{label} · {self._clean_source_label(tab_note.tab_name)}"
                for order, text in enumerate(self._split_passages(chunk.text), 1):
                    suffix = f" · 要点 {order}" if order > 1 else ""
                    result.append(ReportEvidence(
                        f"N{tab_note.id}C{chunk.id or 0}P{order}", "note", note.id, label,
                        f"{chunk.location}{suffix}", text, target_note_id=tab_note.id, chunk_id=chunk.id,
                    ))
        return result

    def _document_evidence(self, document) -> list[ReportEvidence]:
        result: list[ReportEvidence] = []
        for chunk in self.store.list_document_chunks(document.id):
            for order, text in enumerate(self._split_passages(chunk.text), 1):
                suffix = f" · 要点 {order}" if order > 1 else ""
                result.append(ReportEvidence(
                    f"D{document.id}C{chunk.id}P{order}", "document", document.id, self._clean_source_label(document.original_name),
                    f"{chunk.location}{suffix}", text, chunk_id=chunk.id,
                ))
        return result

    @staticmethod
    def _clean_evidence(value: object) -> str:
        """Normalize formatting without turning a table into one giant line."""
        lines: list[str] = []
        for raw_line in str(value or "").replace("\r\n", "\n").split("\n"):
            line = raw_line.strip()
            if not line or re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", line):
                continue
            if "|" in line:
                cells = [cell.strip() for cell in line.strip("|").split("|") if cell.strip()]
                line = "；".join(cells)
            line = re.sub(r"^(?:[-*+]\s+|>\s*|#{1,6}\s*)", "", line)
            line = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", line)
            line = re.sub(r"`([^`]+)`", r"\1", line)
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)[:MAX_BLOCK_CHARS].strip()

    @staticmethod
    def _clean_source_label(value: object) -> str:
        label = str(value or "未命名资料").replace("|", " ")
        label = re.sub(r"^\s*#{1,6}\s*", "", label)
        label = re.sub(r"\s+", " ", label).strip(" -·")
        return label[:140] or "未命名资料"

    @classmethod
    def _split_passages(cls, value: object) -> list[str]:
        cleaned = cls._clean_evidence(value)
        if not cleaned:
            return []
        parts: list[str] = []
        for line in cleaned.split("\n"):
            # Structured spreadsheet rows retain all fields together so a
            # threshold is not separated from its condition.
            units = [line] if "编号" in line or "KRI" in line else re.split(r"(?<=[。！？!?])\s*", line)
            for unit in units:
                unit = unit.strip(" -·；;")
                if not unit:
                    continue
                if len(unit) > MAX_BLOCK_CHARS:
                    unit = unit[:MAX_BLOCK_CHARS].rsplit("。", 1)[0].strip() or unit[:MAX_BLOCK_CHARS]
                if cls._is_meaningful_passage(unit):
                    parts.append(unit)
        if not parts and cls._is_meaningful_passage(cleaned):
            parts.append(cleaned)
        return parts

    @staticmethod
    def _is_meaningful_passage(value: str) -> bool:
        text = str(value or "").strip()
        if len(text) < MIN_PASSAGE_CHARS or len(text) > MAX_BLOCK_CHARS:
            return False
        if re.fullmatch(r"[\W_\d]+", text):
            return False
        if len(text) <= 24 and not re.search(r"[。！？!?；;]", text) and not re.search(
            r"(?:完成|建议|风险|目标|条件|需要|支持|实施|确认|应当|可以|负责)", text
        ):
            return False
        return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", text))

    def _select_source_evidence(self, evidence: list[ReportEvidence], title: str) -> list[ReportEvidence]:
        """Select varied substantive passages instead of arbitrary first chunks."""
        title_terms = {term.lower() for term in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", title)}
        ranked: list[tuple[float, ReportEvidence]] = []
        seen_text: set[str] = set()
        for item in evidence:
            normal = self._normalise(item.text)
            if len(normal) < MIN_PASSAGE_CHARS or normal in seen_text:
                continue
            seen_text.add(normal)
            length_score = 1.0 - min(abs(len(item.text) - 180), 180) / 300
            structure_score = 0.35 if re.search(r"(?:完成|建议|风险|目标|条件|应当|需要|支持|步骤|结论|金额|时间|负责)", item.text) else 0.0
            term_score = sum(0.5 for term in title_terms if term and term in item.text.lower())
            penalty = 0.6 if re.match(r"^(?:正文|第?[一二三四五六七八九十\d]+[、.])", item.text) else 0.0
            ranked.append((length_score + structure_score + term_score - penalty, item))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [item for _, item in ranked[:MAX_CANDIDATES_PER_SOURCE]]

    @staticmethod
    def _normalise(value: str) -> str:
        return re.sub(r"[\s，。；、：:（）()【】\[\]\-]", "", str(value or ""))

    def _validate_claims(self, generated: object, evidence: list[ReportEvidence]) -> list[dict]:
        by_key = {item.key: item for item in evidence}
        seen_claims: set[str] = set()
        used_evidence: set[str] = set()
        seen_quotes: list[str] = []
        claims: list[dict] = []
        if not isinstance(generated, list):
            return []
        for row in generated:
            if not isinstance(row, dict):
                continue
            topic = self._clean_topic(row.get("topic"))
            claim = self._clean_claim(row.get("claim"))
            quote = str(row.get("quote") or "").strip()
            keys = [str(key).strip("[] ") for key in list(row.get("evidence") or [])[:3]]
            if not topic or not claim or not quote or not keys:
                continue
            if claim in seen_claims or len(claims) >= 8:
                continue
            cited = [by_key[key] for key in keys if key in by_key]
            primary = next((item for item in cited if self._quote_is_supported(quote, item.text)), None)
            if primary is None or primary.key in used_evidence:
                continue
            normal_quote = self._normalise(quote)
            if any(normal_quote in prior or prior in normal_quote for prior in seen_quotes):
                continue
            seen_claims.add(claim)
            used_evidence.add(primary.key)
            seen_quotes.append(normal_quote)
            claims.append({
                "id": f"C{len(claims) + 1}",
                "topic": topic,
                "text": claim,
                "quote": quote,
                "source_key": primary.key,
                "source_kind": primary.source_kind,
                "source_id": primary.source_id,
                "source_label": primary.source_label,
                "location": primary.location,
                "target_note_id": primary.target_note_id,
                "chunk_id": primary.chunk_id,
            })
        return claims

    def _supplement_with_source_facts(self, claims: list[dict], evidence: list[ReportEvidence]) -> list[dict]:
        """Keep a single rich source usable when the local model under-produces.

        This is not a fabricated summary: the fallback conclusion is a short,
        contiguous original passage and carries the same source location. It is
        deliberately used only to reach the minimum evidence floor needed for a
        coherent report.
        """
        result = list(claims)
        used_keys = {claim["source_key"] for claim in result}
        used_quotes = [self._normalise(claim["quote"]) for claim in result]
        for item in evidence:
            if len(result) >= 2 or item.key in used_keys:
                continue
            quote = self._short_source_fact(item.text)
            normal_quote = self._normalise(quote)
            if not quote or any(normal_quote in prior or prior in normal_quote for prior in used_quotes):
                continue
            result.append({
                "id": f"C{len(result) + 1}",
                "topic": "资料要点",
                "text": quote,
                "quote": quote,
                "source_key": item.key,
                "source_kind": item.source_kind,
                "source_id": item.source_id,
                "source_label": item.source_label,
                "location": item.location,
                "target_note_id": item.target_note_id,
                "chunk_id": item.chunk_id,
            })
            used_keys.add(item.key)
            used_quotes.append(normal_quote)
        return result

    @staticmethod
    def _short_source_fact(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) > 150:
            ending = max(text.rfind(mark, 24, 150) for mark in ("。", "！", "？", "；", ";"))
            text = text[:ending + 1] if ending >= 24 else text[:150].rstrip()
        return text if 14 <= len(text) <= 150 else ""

    @staticmethod
    def _clean_topic(value: object) -> str:
        topic = re.sub(r"\s+", " ", str(value or "")).strip(" -•；;：:")
        if len(topic) < 2 or len(topic) > 18 or topic in {"资料", "其他", "总结", "要点"}:
            return ""
        return topic

    @staticmethod
    def _clean_claim(value: object) -> str:
        claim = re.sub(r"\s+", " ", str(value or "")).strip(" -•；;")
        if len(claim) < 14 or len(claim) > 150:
            return ""
        if re.fullmatch(r"(?:资料|内容|标题|要点|未覆盖|暂无)(?:不足|如下|说明)?", claim):
            return ""
        return claim

    def _quote_is_supported(self, quote: str, source_text: str) -> bool:
        cleaned_quote = self._normalise(quote)
        cleaned_source = self._normalise(source_text)
        return len(cleaned_quote) >= 8 and cleaned_quote in cleaned_source

    def _build_layout(self, title: str, template: dict, claims: list[dict]) -> dict:
        """Ask the model to arrange verified facts, never to invent new ones."""
        try:
            generated = self.engine.generate_report_structure(title, template, claims)
            layout = self._validate_layout(generated, claims)
            if layout is not None:
                return layout
        except Exception:
            # A valid fact ledger is still more useful than failing a report
            # solely because the optional narrative arrangement timed out.
            pass
        return self._fallback_layout(claims)

    @staticmethod
    def _claim_index(claims: list[dict]) -> dict[str, dict]:
        return {claim["id"]: claim for claim in claims}

    def _validate_layout(self, generated: object, claims: list[dict]) -> dict | None:
        if not isinstance(generated, dict):
            return None
        claim_index = self._claim_index(claims)
        summary = generated.get("summary") or {}
        summary_text = self._clean_narrative(summary.get("text"), minimum=24, maximum=210)
        summary_ids = self._clean_claim_ids(summary.get("claim_ids"), claim_index)
        if not summary_text or len(summary_ids) < 2:
            return None
        sections: list[dict] = []
        used_ids: set[str] = set()
        for row in list(generated.get("sections") or [])[:4]:
            if not isinstance(row, dict):
                continue
            heading = self._clean_topic(row.get("title"))
            intro = self._clean_narrative(row.get("intro"), minimum=18, maximum=180)
            claim_ids = self._clean_claim_ids(row.get("claim_ids"), claim_index)
            claim_ids = [item for item in claim_ids if item not in used_ids]
            if not heading or not intro or not claim_ids:
                continue
            used_ids.update(claim_ids)
            sections.append({"title": heading, "intro": intro, "claim_ids": claim_ids})
        if len(sections) < 2:
            return None
        # Do not silently discard verified facts simply because the organiser
        # omitted them. Attach remaining facts to the closest topic section.
        remaining = [claim["id"] for claim in claims if claim["id"] not in used_ids]
        for claim_id in remaining:
            target = next((item for item in sections if item["title"] == claim_index[claim_id]["topic"]), sections[-1])
            target["claim_ids"].append(claim_id)
        return {"summary": {"text": summary_text, "claim_ids": summary_ids}, "sections": sections}

    @staticmethod
    def _clean_narrative(value: object, minimum: int, maximum: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" -•；;")
        if len(text) < minimum or len(text) > maximum:
            return ""
        if re.search(r"(?:根据资料|如下|见下|以上内容)", text) and len(text) < 50:
            return ""
        return text

    @staticmethod
    def _clean_claim_ids(value: object, claim_index: dict[str, dict]) -> list[str]:
        if isinstance(value, str):
            raw_ids = re.split(r"[,，、\s]+", value)
        elif isinstance(value, list):
            raw_ids = value
        else:
            raw_ids = []
        result: list[str] = []
        for item in raw_ids:
            claim_id = str(item or "").strip().upper().strip("[]")
            if claim_id in claim_index and claim_id not in result:
                result.append(claim_id)
        return result

    def _fallback_layout(self, claims: list[dict]) -> dict:
        grouped: dict[str, list[str]] = {}
        for claim in claims:
            grouped.setdefault(claim["topic"], []).append(claim["id"])
        groups = list(grouped.items())
        if len(groups) > 4:
            retained = groups[:3]
            retained.append(("补充事实", [claim_id for _, ids in groups[3:] for claim_id in ids]))
            groups = retained
        if len(groups) == 1 and len(claims) > 2:
            midpoint = max(1, len(claims) // 2)
            groups = [("资料要点", [item["id"] for item in claims[:midpoint]]), ("补充说明", [item["id"] for item in claims[midpoint:]])]
        sections = [
            {
                "title": title,
                "intro": f"本节汇总 {len(claim_ids)} 项已核验事实，原文依据可逐条展开核对。",
                "claim_ids": claim_ids,
            }
            for title, claim_ids in groups
        ]
        return {
            "summary": {
                "text": "本专题基于所选资料中的已核验事实整理而成，重点与章节均会随资料内容自动变化。",
                "claim_ids": [item["id"] for item in claims[:2]],
            },
            "sections": sections,
        }

    def _materialize_sections(self, layout_sections: list[dict], claims: list[dict]) -> list[dict]:
        claim_index = self._claim_index(claims)
        return [
            {
                "title": section["title"],
                "intro": section["intro"],
                "claim_ids": section["claim_ids"],
                "claims": [claim_index[claim_id] for claim_id in section["claim_ids"] if claim_id in claim_index],
            }
            for section in layout_sections
        ]
