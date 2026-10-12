# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


class DatasetValidationError(ValueError):
    """Raised when a RAG fixture cannot produce a trustworthy evaluation."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def load_dataset(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    normalize_legacy_dataset(payload)
    validate_dataset(payload)
    return payload


def normalize_legacy_dataset(payload: object) -> None:
    """Upgrade supported legacy fixture fields in memory before validation.

    Frozen fixtures stay byte-for-byte unchanged on disk. This compatibility
    layer only translates fields whose old meaning is unambiguous.
    """
    if not isinstance(payload, dict):
        return
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return

    for case in cases:
        if not isinstance(case, dict):
            continue

        expected_titles = case.get("expected_titles", [])
        has_expected_source = isinstance(expected_titles, list) and bool(expected_titles)
        if (
            case.get("allow_insufficient") is True
            and not has_expected_source
            and "expected_refusal" not in case
            and not case.get("expected_clarification")
        ):
            scenario = str(case.get("scenario", "")).strip().lower()
            expected_terms = " ".join(
                str(term) for term in case.get("expected_answer_terms", [])
            )
            refusal_labeled = scenario == "no_answer" or any(
                phrase in expected_terms
                for phrase in ("没有足够信息", "无法回答", "无法确认", "未找到")
            )
            if refusal_labeled:
                case["expected_refusal"] = True
            else:
                case["evaluation_excluded"] = True
                case["evaluation_exclusion_reason"] = (
                    "legacy case has no frozen source or answer ground truth"
                )

        history = case.get("history")
        if isinstance(history, list) and any(isinstance(message, str) for message in history):
            case["history"] = [
                {"role": "user", "content": message}
                if isinstance(message, str)
                else message
                for message in history
            ]


def validate_dataset(payload: dict) -> None:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise DatasetValidationError("fixture root must be an object")

    documents = payload.get("documents")
    cases = payload.get("cases")
    if not isinstance(documents, list) or not documents:
        errors.append("documents must be a non-empty list")
        documents = []
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty list")
        cases = []

    titles: list[str] = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            errors.append(f"documents[{index}] must be an object")
            continue
        title = str(document.get("title", "")).strip()
        content = str(document.get("content", "")).strip()
        source_type = str(document.get("source_type", "note")).strip().lower()
        if not title:
            errors.append(f"documents[{index}] has no title")
        if not content:
            errors.append(f"documents[{index}] has no content")
        if source_type not in {"note", "document"}:
            errors.append(f"documents[{index}] source_type must be note or document")
        if source_type == "document":
            original_name = str(document.get("original_name", "")).strip().lower()
            if not original_name.endswith((".txt", ".docx", ".xlsx", ".pdf")):
                errors.append(
                    f"documents[{index}] document original_name must end with TXT, DOCX, XLSX or PDF"
                )
            chunks = document.get("chunks")
            if chunks is not None:
                if not isinstance(chunks, list) or not chunks:
                    errors.append(f"documents[{index}] chunks must be a non-empty list")
                else:
                    for chunk_index, chunk in enumerate(chunks):
                        if not isinstance(chunk, dict):
                            errors.append(f"documents[{index}].chunks[{chunk_index}] must be an object")
                            continue
                        if not str(chunk.get("text", "")).strip():
                            errors.append(f"documents[{index}].chunks[{chunk_index}] has no text")
                        if not str(chunk.get("location", "")).strip():
                            errors.append(f"documents[{index}].chunks[{chunk_index}] has no location")
        if title:
            titles.append(title)
    duplicate_titles = sorted({title for title in titles if titles.count(title) > 1})
    if duplicate_titles:
        errors.append("duplicate document titles: " + ", ".join(duplicate_titles))

    case_ids: list[str] = []
    known_titles = set(titles)
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}] must be an object")
            continue
        case_id = str(case.get("id", "")).strip()
        question = str(case.get("question", "")).strip()
        if not case_id:
            errors.append(f"cases[{index}] has no id")
        else:
            case_ids.append(case_id)
        if not question:
            errors.append(f"case {case_id or index} has no question")

        expected_titles = case.get("expected_titles", [])
        if not isinstance(expected_titles, list):
            errors.append(f"case {case_id or index} expected_titles must be a list")
            expected_titles = []
        missing_titles = [str(title) for title in expected_titles if title not in known_titles]
        if missing_titles:
            errors.append(
                f"case {case_id or index} references unknown titles: " + ", ".join(missing_titles)
            )
        if (
            not expected_titles
            and not case.get("expected_refusal")
            and not case.get("expected_clarification")
            and not case.get("evaluation_excluded")
        ):
            errors.append(
                f"case {case_id or index} has no expected source and is not refusal/clarification"
            )

        if case.get("evaluation_excluded"):
            reason = str(case.get("evaluation_exclusion_reason", "")).strip()
            if not reason:
                errors.append(
                    f"case {case_id or index} evaluation_excluded requires a reason"
                )

        expected_passages = case.get("expected_passages", [])
        if not isinstance(expected_passages, list):
            errors.append(f"case {case_id or index} expected_passages must be a list")
            expected_passages = []
        passage_ids: list[str] = []
        for passage_index, passage in enumerate(expected_passages):
            if not isinstance(passage, dict):
                errors.append(
                    f"case {case_id or index} expected_passages[{passage_index}] must be an object"
                )
                continue
            passage_id = str(passage.get("id", "")).strip()
            passage_title = str(passage.get("title", "")).strip()
            required_terms = passage.get("required_terms")
            locations = passage.get("locations")
            relevance = passage.get("relevance", 1)
            if not passage_id:
                errors.append(
                    f"case {case_id or index} expected_passages[{passage_index}] has no id"
                )
            else:
                passage_ids.append(passage_id)
            if passage_title not in known_titles:
                errors.append(
                    f"case {case_id or index} passage {passage_id or passage_index} references unknown title"
                )
            if not isinstance(required_terms, list) or not required_terms or not all(
                str(term).strip() for term in required_terms
            ):
                errors.append(
                    f"case {case_id or index} passage {passage_id or passage_index} requires non-empty terms"
                )
            if locations is not None and (
                not isinstance(locations, list)
                or not locations
                or not all(str(location).strip() for location in locations)
            ):
                errors.append(
                    f"case {case_id or index} passage {passage_id or passage_index} locations must be a non-empty list"
                )
            if not isinstance(relevance, int) or relevance not in {1, 2, 3}:
                errors.append(
                    f"case {case_id or index} passage {passage_id or passage_index} relevance must be 1, 2 or 3"
                )
        duplicate_passage_ids = sorted(
            {passage_id for passage_id in passage_ids if passage_ids.count(passage_id) > 1}
        )
        if duplicate_passage_ids:
            errors.append(
                f"case {case_id or index} has duplicate passage ids: "
                + ", ".join(duplicate_passage_ids)
            )

        for field in ("expected_answer_terms", "expected_evidence_terms", "forbidden_terms"):
            value = case.get(field, [])
            if value is not None and not isinstance(value, list):
                errors.append(f"case {case_id or index} {field} must be a list")

        history = case.get("history")
        if history is not None:
            if not isinstance(history, list):
                errors.append(f"case {case_id or index} history must be a list")
            else:
                for message in history:
                    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                        errors.append(f"case {case_id or index} has invalid history message")
                        break

    duplicate_ids = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicate_ids:
        errors.append("duplicate case ids: " + ", ".join(duplicate_ids))

    if errors:
        raise DatasetValidationError("; ".join(errors))


def select_cases(cases: Iterable[dict], case_ids: set[str] | None = None) -> list[dict]:
    selected = [case for case in cases if not case_ids or case["id"] in case_ids]
    if not selected:
        raise DatasetValidationError("no cases matched the requested ids")
    return selected
