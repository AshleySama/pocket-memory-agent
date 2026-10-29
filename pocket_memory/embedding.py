# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np

from pocket_memory.chunking import split_note_text
from pocket_memory.storage import Note, NoteStore

logger = logging.getLogger(__name__)


def resource_path(relative_path: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        external = Path(sys.executable).resolve().parent / relative_path
        # Optional external LLM downloads must not hide bundled BGE assets.
        if external.exists():
            return external
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parent.parent / relative_path


class BgeEmbeddingEngine:
    dimension = 512

    def __init__(self, model_dir: Path | None = None) -> None:
        self.model_dir = (model_dir or resource_path("models/bge-small-zh-v1.5")).resolve()
        self._session = None
        self._tokenizer = None
        self._error: str | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        self._load()
        return self._session is not None and self._tokenizer is not None

    @property
    def unavailable_reason(self) -> str:
        self._load()
        return self._error or ""

    def embed(self, text: str) -> np.ndarray:
        self._load()
        if self._session is None or self._tokenizer is None:
            raise RuntimeError(self._error or "BGE 向量模型未安装")
        encoding = self._tokenizer.encode(text[:4000])
        input_ids = encoding.ids[:512]
        attention_mask = encoding.attention_mask[:512]
        type_ids = encoding.type_ids[:512]
        feeds = {
            "input_ids": np.asarray([input_ids], dtype=np.int64),
            "attention_mask": np.asarray([attention_mask], dtype=np.int64),
        }
        input_names = {item.name for item in self._session.get_inputs()}
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = np.asarray([type_ids], dtype=np.int64)
        output = self._session.run(None, feeds)[0]
        vector = np.asarray(output[0][0], dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector

    def _load(self) -> None:
        if self._session is not None or self._error is not None:
            return
        with self._lock:
            if self._session is not None or self._error is not None:
                return
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer

                model_path = self.model_dir / "onnx" / "model_quantized.onnx"
                tokenizer_path = self.model_dir / "tokenizer.json"
                missing = [str(path) for path in (model_path, tokenizer_path) if not path.is_file()]
                if missing:
                    raise FileNotFoundError("BGE 模型文件缺失: " + ", ".join(missing))
                self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
                self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                logger.info("BGE embedding unavailable: %s", self._error)


class EmbeddingService:
    hybrid_score_threshold = 0.50
    semantic_mode_threshold = 0.50

    def __init__(self, store: NoteStore, engine: BgeEmbeddingEngine | None = None) -> None:
        self.store = store
        self.engine = engine or BgeEmbeddingEngine()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pocket-memory-embedding")
        self._pending_condition = threading.Condition()
        self._pending: set[Future] = set()
        self._closed = False
        self._index_errors_lock = threading.Lock()
        self._index_errors_epoch: int | None = None
        self._index_errors: dict[tuple[str, int], str] = {}

    def status(self) -> dict:
        return {
            "available": self.engine.available,
            "reason": self.engine.unavailable_reason,
            "indexed": self.store.embedding_count(),
            "total": self.store.note_count(),
            "note_chunks_indexed": self.store.note_chunk_embedding_count(),
            "document_chunks_indexed": self.store.document_chunk_embedding_count(),
        }

    def index_errors(self) -> list[dict]:
        """Return current-library failures until successful indexing or deletion.

        A drained queue is not enough to serve a cached index: callers must also
        check this result before using previously persisted chunks.
        """
        with self.store._lock:
            epoch = self.store.source_revision()[0]
            with self._index_errors_lock:
                if self._index_errors_epoch != epoch:
                    self._index_errors.clear()
                    self._index_errors_epoch = epoch
                for kind, source_id in list(self._index_errors):
                    try:
                        if kind == "note":
                            self.store.get_note(source_id)
                        else:
                            self.store.get_document(source_id)
                    except KeyError:
                        self._index_errors.pop((kind, source_id), None)
                return [
                    {"source_kind": kind, "source_id": source_id, "error": error}
                    for (kind, source_id), error in sorted(self._index_errors.items())
                ]

    def index_ready(self) -> bool:
        """Whether queued embedding work is complete without known source failures."""
        if self.index_errors():
            return False
        with self._pending_condition:
            return not any(not future.done() for future in self._pending)

    def _record_index_error(self, kind: str, source_id: int, epoch: int | None, error: str) -> None:
        with self.store._lock:
            try:
                if self.store.source_revision()[0] != epoch:
                    return
                if kind == "note":
                    self.store.get_note(source_id)
                else:
                    self.store.get_document(source_id)
            except (KeyError, RuntimeError):
                self._clear_index_error(kind, source_id, epoch)
                return
            with self._index_errors_lock:
                if self._index_errors_epoch != epoch:
                    self._index_errors.clear()
                    self._index_errors_epoch = epoch
                self._index_errors[(kind, source_id)] = error[:800]

    def _clear_index_error(self, kind: str, source_id: int, epoch: int | None) -> None:
        with self._index_errors_lock:
            if self._index_errors_epoch == epoch:
                self._index_errors.pop((kind, source_id), None)

    def enqueue(self, note_id: int) -> None:
        epoch = self.store.source_revision()[0]
        if self.engine.available:
            self._submit(self._process, note_id, epoch)

    def enqueue_document(self, document_id: int) -> None:
        """Embed each extracted file chunk separately for long-document recall."""
        epoch = self.store.source_revision()[0]
        if self.engine.available:
            self._submit(self._process_document, document_id, epoch)
        else:
            # Keyword FTS remains usable when the optional embedding model is absent.
            with self.store._lock:
                if self.store.source_revision()[0] == epoch:
                    try:
                        self.store.set_document_status(document_id, "ready")
                    except KeyError:
                        pass

    def _submit(self, method, source_id: int, epoch: int) -> None:
        with self._pending_condition:
            if self._closed:
                return
            future = self.executor.submit(method, source_id, epoch)
            self._pending.add(future)
            future.add_done_callback(self._task_done)

    def _task_done(self, future: Future) -> None:
        with self._pending_condition:
            self._pending.discard(future)
            self._pending_condition.notify_all()

    def drain(self, timeout: float | None = 30.0) -> bool:
        """Wait for work already queued at entry; completion is not success.

        New work submitted after this call starts is left for a subsequent drain.
        A timeout leaves running and queued tasks intact.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._pending_condition:
            pending = tuple(self._pending)
            while any(not future.done() for future in pending):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._pending_condition.wait(remaining)
            return True

    def rebuild(self) -> None:
        if not self.engine.available:
            logger.warning("Embedding rebuild skipped: engine unavailable (%s)", self.engine.unavailable_reason)
            return
        # 先清理孤儿向量：历史删除笔记时若未同步删 embedding，此处兜底清理，
        # 否则 embedding_count 会大于实际笔记数，语义检索也会因 get_note 失败而报错。
        try:
            removed = self.store.delete_orphan_embeddings()
            if removed:
                logger.info("清理孤儿向量 %d 条", removed)
        except Exception:
            logger.exception("清理孤儿向量失败")
        for note in self.store.list_notes(limit=100000):
            self.enqueue(note.id)
            # 同时为子页签创建向量
            for tab in self.store.get_note_tabs(note.id):
                if tab["id"] != note.id:
                    self.enqueue(tab["id"])
        for document in self.store.list_documents(limit=100000):
            self.enqueue_document(document.id)

    def query_embedding(self, query: str) -> np.ndarray | None:
        if not query.strip() or not self.engine.available:
            return None
        return self.engine.embed(query)

    @staticmethod
    def _top_vector_ids(rows: list[tuple[int, np.ndarray]], query_vector: np.ndarray | None,
                        limit: int, min_score: float | None) -> list[tuple[int, float]]:
        if query_vector is None or not rows:
            return []
        ids = [row[0] for row in rows]
        matrix = np.vstack([row[1] for row in rows])
        scores = matrix @ query_vector
        # Stable descending ordering keeps equal-score results deterministic.
        order = np.argsort(-scores, kind="stable")
        results = []
        for index in order:
            score = float(scores[index])
            if min_score is not None and score < min_score:
                break
            results.append((ids[int(index)], score))
            if len(results) >= limit:
                break
        return results

    def semantic_search(self, query: str, limit: int = 20, min_score: float | None = None,
                        query_vector: np.ndarray | None = None) -> list[tuple[Note, float]]:
        if not query.strip() or not self.engine.available:
            return []
        query_vector = query_vector if query_vector is not None else self.query_embedding(query)
        results = []
        for note_id, score in self._top_vector_ids(self.store.list_embeddings(), query_vector, limit, min_score):
            try:
                note = self.store.get_note(note_id)
            except KeyError:
                # 兜底：跳过孤儿向量（note 已被删除但 embedding 未清理）
                continue
            if note.source_type == "welcome":
                continue
            results.append((note, score))
        return results

    def semantic_document_search(self, query: str, limit: int = 20, min_score: float | None = None,
                                 query_vector: np.ndarray | None = None):
        if not query.strip() or not self.engine.available:
            return []
        query_vector = query_vector if query_vector is not None else self.query_embedding(query)
        results = []
        for chunk_id, score in self._top_vector_ids(
            self.store.list_document_chunk_embeddings(), query_vector, limit, min_score
        ):
            try:
                chunk = self.store.get_document_chunk(chunk_id)
            except KeyError:
                continue
            results.append((chunk, score))
        return results

    def semantic_note_chunk_search(self, query: str, limit: int = 20, min_score: float | None = None,
                                  query_vector: np.ndarray | None = None):
        if not query.strip() or not self.engine.available:
            return []
        query_vector = query_vector if query_vector is not None else self.query_embedding(query)
        results = []
        for chunk_id, score in self._top_vector_ids(
            self.store.list_note_chunk_embeddings(), query_vector, limit, min_score
        ):
            try:
                chunk = self.store.get_note_chunk(chunk_id)
            except KeyError:
                continue
            results.append((chunk, score))
        return results

    def hybrid_search(self, query: str, limit: int = 100, **filters: str) -> list[Note]:
        # 展开 folder_id 为 folder_ids（含所有子孙 folder），与 list_notes 行为一致：
        # 点击大分类时，语义检索也应覆盖所有小分类下的笔记。
        if filters.get("folder_id") is not None and "folder_ids" not in filters:
            filters["folder_ids"] = set(self.store.get_descendant_folder_ids(filters.pop("folder_id")))
        keyword_notes = self.store.search_notes(query, limit=limit, **filters)
        # 语义检索：按父笔记聚合，子页签命中归到父笔记，确保只返回主笔记
        semantic_by_parent: dict[int, tuple[Note, float]] = {}
        for note, score in self.semantic_search(query, limit=limit, min_score=self.hybrid_score_threshold):
            if not self._matches_filters(note, filters):
                continue
            group_id = note.parent_note_id or note.id
            if group_id not in semantic_by_parent or score > semantic_by_parent[group_id][1]:
                parent_note = note if note.parent_note_id is None else self.store.get_note(group_id)
                semantic_by_parent[group_id] = (parent_note, score)
        semantic_notes = [note for note, _ in semantic_by_parent.values()]
        scores: dict[int, float] = {}
        notes: dict[int, Note] = {}
        for rank, note in enumerate(keyword_notes):
            notes[note.id] = note
            scores[note.id] = scores.get(note.id, 0) + 1 / (60 + rank)
        for rank, note in enumerate(semantic_notes):
            notes[note.id] = note
            scores[note.id] = scores.get(note.id, 0) + 1 / (60 + rank)
        return sorted(notes.values(), key=lambda note: scores[note.id], reverse=True)[:limit]

    def shutdown(self, wait: bool = False) -> None:
        with self._pending_condition:
            self._closed = True
        self.executor.shutdown(wait=wait, cancel_futures=True)

    @staticmethod
    def _note_fingerprint(note: Note) -> tuple:
        """Compare source content and metadata rather than second-level timestamps alone."""
        return (
            note.title, note.tab_name, note.parent_note_id, note.tab_order,
            note.category, note.subcategory, note.folder_id, tuple(note.tags or []),
            note.content, note.ocr_text, note.source_type, note.attachment_path,
            note.created_at, note.updated_at,
        )

    def _process(self, note_id: int, epoch: int | None = None) -> None:
        try:
            with self.store._lock:
                current_epoch = self.store.source_revision()[0]
                if epoch is not None and epoch != current_epoch:
                    return
                epoch = current_epoch
                note = self.store.get_note(note_id)
            if note.source_type == "welcome":
                self._clear_index_error("note", note_id, epoch)
                return
            fingerprint = self._note_fingerprint(note)
            text = "\n".join(
                part
                for part in (
                    note.title,
                    note.tab_name,
                    note.category,
                    note.subcategory,
                    " ".join(note.tags or []),
                    note.content,
                    note.ocr_text,
                )
                if part.strip()
            )
            vector = self.engine.embed(text)
            # Match storage's existing field normalization before embedding.
            # This does not change the splitter or its boundaries.
            chunks = [
                {
                    "title": str(chunk.get("title") or "未命名笔记")[:180],
                    "text": str(chunk["text"]).strip(),
                    "location": str(chunk.get("location") or "正文")[:240],
                    "kind": str(chunk.get("kind") or "content")[:32],
                }
                for chunk in self._note_chunks(note)
                if str(chunk.get("text", "")).strip()
            ]
            chunk_vectors = []
            for chunk in chunks:
                chunk_text = "\n".join(part for part in (chunk["title"], chunk["location"], chunk["text"]) if part.strip())
                chunk_vectors.append(self.engine.embed(chunk_text))
            # All model work is outside the store lock. Validate and publish as
            # one critical section so an edit, deletion or switch cannot race it.
            with self.store._lock:
                if self.store.source_revision()[0] != epoch:
                    return
                if self._note_fingerprint(self.store.get_note(note_id)) != fingerprint:
                    self._record_index_error("note", note_id, epoch, "Source changed during indexing; reindex required")
                    return
                self.store.upsert_embedding(note_id, vector)
                stored_chunks = self.store.list_note_chunks(note_id)
                existing_fields = [
                    (chunk.title, chunk.text, chunk.location, chunk.kind, chunk.chunk_order)
                    for chunk in stored_chunks
                ]
                computed_fields = [
                    (chunk["title"], chunk["text"], chunk["location"], chunk["kind"], order)
                    for order, chunk in enumerate(chunks)
                ]
                if existing_fields != computed_fields:
                    stored_chunks = self.store.replace_note_chunks(note_id, chunks)
                for chunk, chunk_vector in zip(stored_chunks, chunk_vectors):
                    self.store.upsert_note_chunk_embedding(chunk.id, chunk_vector)
                self._clear_index_error("note", note_id, epoch)
        except KeyError as exc:
            self._record_index_error("note", note_id, epoch, f"KeyError: {exc}")
            return
        except Exception as exc:
            logger.exception("Embedding failed for note %s", note_id)
            self._record_index_error("note", note_id, epoch, f"{type(exc).__name__}: {exc}")

    def _process_document(self, document_id: int, epoch: int | None = None) -> None:
        document = None
        chunks = []
        try:
            with self.store._lock:
                current_epoch = self.store.source_revision()[0]
                if epoch is not None and epoch != current_epoch:
                    return
                epoch = current_epoch
                document = self.store.get_document(document_id)
                chunks = self.store.list_document_chunks(document_id)
            chunk_vectors = []
            for chunk in chunks:
                text = "\n".join(part for part in (chunk.title, chunk.location, chunk.text) if part.strip())
                chunk_vectors.append(self.engine.embed(text))
            with self.store._lock:
                if self.store.source_revision()[0] != epoch:
                    return
                if self.store.get_document(document_id) != document or self.store.list_document_chunks(document_id) != chunks:
                    self._record_index_error("document", document_id, epoch, "Source changed during indexing; reindex required")
                    return
                for chunk, vector in zip(chunks, chunk_vectors):
                    self.store.upsert_document_chunk_embedding(chunk.id, vector)
                self.store.set_document_status(document_id, "ready")
                self._clear_index_error("document", document_id, epoch)
        except KeyError as exc:
            self._record_index_error("document", document_id, epoch, f"KeyError: {exc}")
            return
        except Exception as exc:
            logger.exception("Embedding failed for document %s", document_id)
            self._record_index_error("document", document_id, epoch, f"{type(exc).__name__}: {exc}")
            with self.store._lock:
                try:
                    if (
                        self.store.source_revision()[0] == epoch
                        and self.store.get_document(document_id) == document
                        and self.store.list_document_chunks(document_id) == chunks
                    ):
                        self.store.set_document_status(document_id, "failed", f"{type(exc).__name__}: {exc}")
                except (KeyError, RuntimeError):
                    pass

    @staticmethod
    def _note_chunks(note: Note) -> list[dict]:
        chunks = []
        for kind, label, text in (("content", "正文", note.content), ("ocr", "图片识别", note.ocr_text)):
            for index, segment in enumerate(split_note_text(text), 1):
                chunks.append({
                    "title": EmbeddingService._chunk_title(note.title, segment),
                    "text": segment,
                    "location": f"{label} {index}",
                    "kind": kind,
                })
        if not chunks and note.title.strip():
            chunks.append({"title": note.title, "text": note.title, "location": "标题", "kind": "title"})
        return chunks

    @staticmethod
    def _chunk_title(default_title: str, text: str) -> str:
        """Prefer a section heading when a persisted chunk starts with one."""
        for line in text.splitlines()[:3]:
            candidate = line.strip().lstrip("#").strip()
            if candidate.startswith("《") and "》" in candidate:
                return candidate[:180]
            if line.lstrip().startswith("#") and candidate:
                return candidate[:180]
        return default_title

    @staticmethod
    def _split_text(text: str, target_size: int = 420, overlap: int = 80) -> list[str]:
        return split_note_text(text, target_size=target_size, overlap=overlap)

    @staticmethod
    def _matches_filters(note: Note, filters: dict[str, str]) -> bool:
        folder_ids = filters.get("folder_ids")
        folder_id = filters.get("folder_id")
        folder_ok = (
            (not folder_ids and folder_id is None)
            or (folder_ids and note.folder_id in folder_ids)
            or (folder_id is not None and note.folder_id == folder_id)
        )
        return (
            (not filters.get("category") or note.category == filters["category"])
            and (not filters.get("subcategory") or note.subcategory == filters["subcategory"])
            and (not filters.get("tag") or filters["tag"] in (note.tags or []))
            and (not filters.get("since") or note.created_at >= filters["since"])
            and (not filters.get("favorite") or bool(note.favorite))
            and folder_ok
        )
