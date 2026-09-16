from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass

from pocket_memory.chunking import split_note_text


TEMPLATES = {
    "general": {"label": "通用主题", "fields": []},
    "company": {"label": "公司", "fields": ["基本情况", "主营业务", "资质与能力", "项目与客户", "关键时间"]},
    "project": {"label": "项目", "fields": ["目标与范围", "关键节点", "协作与责任", "风险与决策"]},
    "meeting": {"label": "会议", "fields": ["会议结论", "待办事项", "责任与时间", "风险与分歧"]},
}

MAX_SOURCES = 12
MAX_EVIDENCE_BLOCKS = 10
MAX_BLOCK_CHARS = 230


@dataclass(frozen=True)
class EvidenceBlock:
    key: str
    source_kind: str
    source_id: int
    source_label: str
    location: str
    text: str
    target_note_id: int | None = None
    chunk_id: int | None = None

    def as_prompt(self) -> str:
        return f"[{self.key}] {self.source_label}｜{self.location}\n{self.text}"

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


class WikiService:
    """Build deliberately evidence-bound topic-card drafts from user-selected sources."""

    def __init__(self, store, engine) -> None:
        self.store = store
        self.engine = engine

    @staticmethod
    def templates() -> list[dict]:
        return [{"id": key, "label": value["label"], "fields": value["fields"]} for key, value in TEMPLATES.items()]

    def create_draft(self, title: str, template: str, source_refs: list[dict]) -> dict:
        clean_title = str(title or "").strip()[:100]
        if not clean_title:
            raise ValueError("请输入专题名称")
        if template not in TEMPLATES:
            raise ValueError("不支持的专题模板")
        sources, blocks = self._collect_sources(source_refs)
        if not blocks:
            raise ValueError("请至少选择一条包含正文的笔记或文档")
        if not getattr(self.engine, "available", False):
            raise RuntimeError(getattr(self.engine, "unavailable_reason", "智能模型尚未就绪"))

        generated = self.engine.generate_wiki_card(clean_title, TEMPLATES[template], [block.as_dict() for block in blocks])
        items = self._validate_items(generated, template, blocks)
        card_id = self.store.create_wiki_card(clean_title, template, sources, items)
        return self.store.get_wiki_card(card_id)

    def rebuild(self, card_id: int) -> dict:
        card = self.store.get_wiki_card(card_id)
        return self.create_draft(card["title"], card["template"], card["source_refs"])

    def _collect_sources(self, source_refs: list[dict]) -> tuple[list[dict], list[EvidenceBlock]]:
        if not isinstance(source_refs, list):
            raise ValueError("资料选择格式无效")
        seen: set[tuple[str, int]] = set()
        sources: list[dict] = []
        blocks: list[EvidenceBlock] = []
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
                sources.append({"kind": kind, "id": note.id, "label": note.title, "updated_at": note.updated_at, "sha256": self.note_fingerprint(note)})
                note_blocks = self._note_blocks(note)
            elif kind == "document":
                document = self.store.get_document(source_id)
                sources.append({"kind": kind, "id": document.id, "label": document.original_name, "updated_at": document.updated_at, "sha256": document.sha256})
                note_blocks = self._document_blocks(document)
            else:
                continue
            for block in note_blocks:
                if len(blocks) >= MAX_EVIDENCE_BLOCKS:
                    break
                blocks.append(block)
        return sources, blocks

    @staticmethod
    def note_fingerprint(note) -> str:
        content = "\0".join((str(note.title or ""), str(note.content or ""), str(note.ocr_text or "")))
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _note_blocks(self, note) -> list[EvidenceBlock]:
        result: list[EvidenceBlock] = []
        tabs = self.store.get_note_tabs(note.id)
        for tab in tabs:
            tab_note = self.store.get_note(tab["id"])
            for chunk in self.store.list_note_chunks(tab_note.id):
                text = str(chunk.text or "").strip()[:MAX_BLOCK_CHARS]
                if text:
                    label = note.title if tab_note.id == note.id else f"{note.title} · {tab_note.tab_name}"
                    result.append(
                        EvidenceBlock(
                            f"N{tab_note.id}C{chunk.id}",
                            "note",
                            note.id,
                            label,
                            chunk.location,
                            text,
                            target_note_id=tab_note.id,
                            chunk_id=chunk.id,
                        )
                    )
        if not result:
            for index, text in enumerate(split_note_text(note.content)[:3], 1):
                result.append(
                    EvidenceBlock(
                        f"N{note.id}T{index}",
                        "note",
                        note.id,
                        note.title,
                        f"正文 {index}",
                        text[:MAX_BLOCK_CHARS],
                        target_note_id=note.id,
                    )
                )
        return result

    def _document_blocks(self, document) -> list[EvidenceBlock]:
        result = []
        for chunk in self.store.list_document_chunks(document.id)[:4]:
            text = str(chunk.text or "").strip()[:MAX_BLOCK_CHARS]
            if text:
                result.append(
                    EvidenceBlock(
                        f"D{document.id}C{chunk.id}",
                        "document",
                        document.id,
                        document.original_name,
                        chunk.location,
                        text,
                        chunk_id=chunk.id,
                    )
                )
        return result

    @staticmethod
    def _compact(value: str) -> str:
        return re.sub(r"[\s，。；、：:（）()【】\[\]\-]", "", str(value or ""))

    def _validate_items(self, generated: dict, template: str, blocks: list[EvidenceBlock]) -> list[dict]:
        if not isinstance(generated, dict):
            generated = {}
        by_key = {block.key: block for block in blocks}
        accepted: list[dict] = []
        used_fields: set[str] = set()
        seen_claims: set[tuple[str, str]] = set()
        allowed_fields = set(TEMPLATES[template]["fields"])
        for raw in list(generated.get("facts") or [])[:8]:
            field = str(raw.get("field") or "要点").strip()[:32]
            claim = str(raw.get("claim") or "").strip()[:180]
            evidence_keys = [str(key) for key in list(raw.get("evidence") or [])[:3] if str(key) in by_key]
            if not claim or not evidence_keys or (allowed_fields and field not in allowed_fields):
                continue
            compact_claim = self._compact(claim)
            # Only preserve a fact when it is a direct, inspectable quotation or substring of its cited evidence.
            if len(compact_claim) < 6 or not any(compact_claim in self._compact(by_key[key].text) for key in evidence_keys):
                continue
            identity = (field, compact_claim)
            if identity in seen_claims:
                continue
            seen_claims.add(identity)
            used_fields.add(field)
            accepted.append({"item_type": "verified", "field": field, "claim": claim, "evidence": [by_key[key].as_dict() for key in evidence_keys]})
        for field in TEMPLATES[template]["fields"]:
            if field not in used_fields:
                accepted.append({"item_type": "missing", "field": field, "claim": "未在当前资料范围找到明确来源", "evidence": []})
        # Different direct quotations assigned to the same schema field are surfaced for review, never resolved.
        for field in set(item["field"] for item in accepted if item["item_type"] == "verified"):
            same_field = [item for item in accepted if item["item_type"] == "verified" and item["field"] == field]
            if len(same_field) > 1:
                accepted.append({"item_type": "conflict", "field": field, "claim": "同一字段存在多种原始表述，请核对来源", "evidence": [e for item in same_field for e in item["evidence"]]})
        return accepted
