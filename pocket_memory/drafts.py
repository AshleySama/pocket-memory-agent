# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Local, library-scoped editor recovery snapshots; never indexed as notes.

Each editing session owns a UUID. Closing it leaves a small durable tombstone,
so a delayed browser request cannot resurrect a discarded/committed draft.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import UUID

MAX_RECORD_BYTES = 15 * 1024 * 1024
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_FIELDS = (
    "draft_id", "revision", "kind", "edit_field", "note_id", "parent_note_id", "title",
    "content", "base_content", "base_updated_at", "base_created_at",
    "folder_id", "image_data_url", "image_name",
)


class DraftConflict(Exception):
    def __init__(self, code: str, message: str, current: dict | None = None):
        super().__init__(message)
        self.code = code
        self.current = current


class DraftStorageError(Exception):
    """An existing snapshot is unreadable; do not overwrite it as an empty store."""


def library_identity(data_dir: Path) -> str:
    canonical = os.path.normcase(str(Path(data_dir).resolve()))
    return hashlib.sha256(("pocket-memory-editor-v1\0" + canonical).encode("utf-8")).hexdigest()


def _draft_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("草稿编号无效")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("草稿编号无效") from exc
    if str(parsed) != value.lower():
        raise ValueError("草稿编号必须为标准 UUID")
    return str(parsed)


def _optional_id(value, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"草稿 {field} 无效")
    return value


def _text(payload, field: str, maximum: int, default="") -> str:
    value = payload.get(field, default)
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"草稿 {field} 不是有效文本或过长")
    return value


def _normalize(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("草稿必须为对象")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 9007199254740991:
        raise ValueError("草稿修订号必须为正整数")
    kind = payload.get("kind")
    if kind not in ("new", "note"):
        raise ValueError("草稿类型无效")
    edit_field = payload.get("edit_field", "content")
    if edit_field not in ("content", "title", "tab_name") or (kind == "new" and edit_field != "content"):
        raise ValueError("草稿编辑字段无效")
    note_id = _optional_id(payload.get("note_id"), "note_id")
    if (kind == "note") != (note_id is not None):
        raise ValueError("草稿类型与笔记编号不一致")
    image = payload.get("image_data_url")
    if image is not None:
        if not isinstance(image, str) or not re.fullmatch(r"data:image/(?:png|jpeg|jpg|webp|gif|bmp);base64,[A-Za-z0-9+/=\r\n]*", image):
            raise ValueError("草稿图片必须为本机图片数据")
    image_name = payload.get("image_name")
    if image_name is not None and (not isinstance(image_name, str) or len(image_name) > 4096):
        raise ValueError("草稿图片名称无效")
    record = {
        "draft_id": _draft_id(payload.get("draft_id")), "revision": revision,
        "kind": kind, "edit_field": edit_field, "note_id": note_id,
        "parent_note_id": _optional_id(payload.get("parent_note_id"), "parent_note_id"),
        "title": _text(payload, "title", 4096),
        "content": _text(payload, "content", 2_000_000),
        "base_content": _text(payload, "base_content", 2_000_000),
        "base_updated_at": _text(payload, "base_updated_at", 128),
        "base_created_at": _text(payload, "base_created_at", 128),
        "folder_id": _optional_id(payload.get("folder_id"), "folder_id"),
        "image_data_url": image, "image_name": image_name,
    }
    if len(json.dumps(record, ensure_ascii=False).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("草稿内容过大，无法保存恢复副本")
    return record


class DraftStore:
    def __init__(self, data_dir: Path):
        self.library_id = library_identity(data_dir)
        self.directory = Path(data_dir).resolve() / ".editor-drafts"
        key = os.path.normcase(str(self.directory))
        with _LOCKS_GUARD:
            self._thread_lock = _LOCKS.setdefault(key, threading.RLock())

    def _require_library(self, value) -> None:
        if value != self.library_id:
            raise DraftConflict("library_mismatch", "资料库已经切换，草稿没有写入其他资料库")

    @contextmanager
    def _locked(self):
        # A process-local lock alone is insufficient when two local app
        # instances happen to share a library. Lock one stable sidecar byte.
        with self._thread_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / ".lock").open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0"); handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError as exc:
                            if time.monotonic() >= deadline:
                                raise DraftStorageError("草稿正在被另一窗口保存，请稍后重试") from exc
                            time.sleep(0.025)
                    try:
                        yield
                    finally:
                        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self, draft_id: str) -> dict | None:
        path = self.directory / f"{draft_id}.json"
        try:
            if not path.exists():
                return None
            if path.stat().st_size > MAX_RECORD_BYTES + 4096:
                raise ValueError("snapshot size")
            wrapper = json.loads(path.read_text("utf-8"))
            if not isinstance(wrapper, dict) or wrapper.get("version") != 1 or wrapper.get("draft_id") != draft_id:
                raise ValueError("snapshot identity")
            if wrapper.get("closed") is True:
                return wrapper
            record = wrapper.get("draft")
            normalized = _normalize(record)
            if normalized["draft_id"] != draft_id or any(record.get(k) != v for k, v in normalized.items()):
                raise ValueError("snapshot record")
            if not all(isinstance(record.get(k), str) and record[k] for k in ("created_at", "updated_at")):
                raise ValueError("snapshot timestamp")
            return wrapper
        except (OSError, ValueError, TypeError) as exc:
            raise DraftStorageError(f"草稿 {draft_id} 无法读取，原文件已保留，请勿覆盖") from exc

    def _write(self, draft_id: str, wrapper: dict) -> None:
        target = self.directory / f"{draft_id}.json"
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".pending-", suffix=".tmp", dir=self.directory, delete=False) as handle:
                temp_name = handle.name
                json.dump(wrapper, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            temp_name = None
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
        except OSError as exc:
            raise DraftStorageError("草稿未能写入本机，请检查磁盘空间后重试") from exc
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)

    def list(self) -> dict:
        with self._locked():
            records, closed_ids = [], []
            for path in self.directory.glob("*.json"):
                try:
                    identifier = _draft_id(path.stem)
                except ValueError as exc:
                    raise DraftStorageError("草稿目录存在无法识别的记录，已保留原文件") from exc
                wrapper = self._read(identifier)
                if wrapper and not wrapper.get("closed"):
                    records.append(wrapper["draft"])
                elif wrapper:
                    closed_ids.append(identifier)
            records.sort(key=lambda value: (value["updated_at"], value["draft_id"]), reverse=True)
            return {"library_id": self.library_id, "drafts": records, "closed_ids": closed_ids}

    def save(self, payload: dict) -> dict:
        self._require_library(payload.get("library_id"))
        record = _normalize(payload)
        with self._locked():
            previous = self._read(record["draft_id"])
            if previous and previous.get("closed"):
                raise DraftConflict("draft_closed", "这份草稿已保存或丢弃，迟到请求未重新创建它")
            old = previous["draft"] if previous else None
            if old:
                identity = ("kind", "edit_field", "note_id", "parent_note_id", "base_created_at")
                if any(old[key] != record[key] for key in identity):
                    raise DraftConflict("identity_conflict", "草稿所属笔记或页签不一致", old)
                if record["revision"] < old["revision"]:
                    raise DraftConflict("stale_revision", "已有更新的草稿，未覆盖", old)
                if record["revision"] == old["revision"]:
                    if any(old[key] != record[key] for key in _FIELDS):
                        raise DraftConflict("revision_conflict", "同一草稿修订出现不同内容，未覆盖", old)
                    return {"library_id": self.library_id, "draft": old}
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            record.update(created_at=old["created_at"] if old else now, updated_at=now)
            self._write(record["draft_id"], {"version": 1, "draft_id": record["draft_id"], "draft": record})
            return {"library_id": self.library_id, "draft": record}

    def close(self, draft_id: str, library_id: str) -> dict:
        self._require_library(library_id)
        draft_id = _draft_id(draft_id)
        with self._locked():
            previous = self._read(draft_id)
            if not previous or not previous.get("closed"):
                self._write(draft_id, {"version": 1, "draft_id": draft_id, "closed": True,
                                       "closed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")})
            return {"library_id": self.library_id, "draft_id": draft_id, "closed": True}
