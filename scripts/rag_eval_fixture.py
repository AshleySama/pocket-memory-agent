# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
from typing import Any


_MIME_TYPES = {
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}


def populate_fixture(store: Any, embedding: Any, fixture: dict) -> None:
    """Load notes and synthetic imported documents through production storage APIs."""

    for source in fixture["documents"]:
        if str(source.get("source_type", "note")).strip().lower() == "document":
            _create_document(store, embedding, source)
        else:
            note = store.create_note(
                source["title"],
                source["content"],
                category=source.get("category", "Other"),
                subcategory=source.get("subcategory", ""),
            )
            embedding._process(note.id)
            store.update_ai_status(note.id, "done", "")


def _create_document(store: Any, embedding: Any, source: dict) -> None:
    original_name = str(source.get("original_name") or "").strip()
    suffix = "." + original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if suffix not in _MIME_TYPES:
        raise ValueError(f"unsupported evaluation document type: {original_name}")

    # The original bytes are deliberately small because fixture chunks are
    # injected below. Storage, indexing and retrieval still use production APIs.
    payload = (
        "Pocket Memory synthetic RAG evaluation fixture\n"
        f"title={source['title']}\n"
    ).encode("utf-8")
    data_url = (
        f"data:{_MIME_TYPES[suffix]};base64,"
        + base64.b64encode(payload).decode("ascii")
    )
    document = store.create_document_from_data_url(original_name, data_url)
    chunks = list(source.get("chunks") or [])
    if not chunks:
        chunks = [{"title": source["title"], "text": source["content"], "location": "正文"}]
    normalised_chunks = []
    for chunk in chunks:
        item = dict(chunk)
        item.setdefault("title", source["title"])
        normalised_chunks.append(item)
    replaced = store.replace_document_chunks(
        document.id,
        normalised_chunks,
        expected_sha256=document.sha256,
    )
    if replaced is None:
        raise RuntimeError(f"evaluation document changed while indexing: {source['title']}")
    embedding._process_document(document.id)
