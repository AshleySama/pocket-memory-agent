from __future__ import annotations

import re


# Notes need small, precise evidence units.  Imported documents use their own
# larger extractor because headings, pages and table locations must be retained.
NOTE_CHUNK_TARGET_SIZE = 420
NOTE_CHUNK_OVERLAP = 80


def split_note_text(
    text: str,
    target_size: int = NOTE_CHUNK_TARGET_SIZE,
    overlap: int = NOTE_CHUNK_OVERLAP,
) -> list[str]:
    """Return the canonical chunks used both for note indexing and retrieval.

    Structured rows are kept as complete records whenever possible, so a name,
    its function and its scenario do not end up in different vectors.
    """
    normalized = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if not normalized:
        return []
    parts = [
        part.strip()
        for part in re.split(r"\n\s*\n|(?=^#{1,6}\s)|(?=《[^》]+》)", normalized, flags=re.MULTILINE)
        if part.strip()
    ]
    chunks: list[str] = []
    for part in parts:
        if len(part) <= target_size:
            chunks.append(part)
            continue
        lines = [line.strip() for line in part.splitlines() if line.strip()]
        structured_rows = [
            line for line in lines
            if "；" in line and re.search(r"(?:编号|名称|Agent名称|核心功能|适用场景|合同编号)：", line)
        ]
        if len(structured_rows) >= 2:
            current: list[str] = []
            current_size = 0
            for line in lines:
                units = _split_long_unit(line, target_size, overlap=0)
                for unit in units:
                    unit_size = len(unit) + (1 if current else 0)
                    if current and current_size + unit_size > target_size:
                        chunks.append("\n".join(current))
                        current = []
                        current_size = 0
                    current.append(unit)
                    current_size += len(unit) + (1 if len(current) > 1 else 0)
            if current:
                chunks.append("\n".join(current))
            continue

        sentences = [item.strip() for item in re.split(r"(?<=[。！？；;.!?])\s*|\n+", part) if item.strip()]
        current = ""
        for sentence in sentences:
            for unit in _split_long_unit(sentence, target_size, overlap):
                if current and len(current) + len(unit) > target_size:
                    chunks.append(current.strip())
                    current = current[-overlap:] if overlap else ""
                # A very long sentence may already fill the target. Keep the
                # overlap only when it still fits instead of violating the cap.
                if current and len(current) + len(unit) > target_size:
                    current = unit
                    continue
                current = (current + "\n" + unit).strip() if current else unit
        if current:
            chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk.strip()]


def _split_long_unit(text: str, limit: int, overlap: int) -> list[str]:
    """Hard-bound a sentence or a structured row without producing empty chunks."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return [value] if value else []
    pieces: list[str] = []
    while len(value) > limit:
        cut = max(value.rfind(mark, 0, limit) for mark in ("；", ";", "，", ",", "。", " "))
        if cut < max(1, limit // 2):
            cut = limit
        else:
            cut += 1
        pieces.append(value[:cut].strip())
        start = max(0, cut - overlap) if overlap else cut
        value = value[start:].strip()
    if value:
        pieces.append(value)
    return pieces
