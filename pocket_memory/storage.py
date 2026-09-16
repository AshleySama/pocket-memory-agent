from __future__ import annotations

import base64
import binascii
import hashlib
from difflib import SequenceMatcher
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np

WELCOME_SOURCE_TYPE = "welcome"
MAX_TABS_PER_NOTE = 20
MAX_NOTE_CONTENT_CHARS = 200_000
MAX_TAGS_PER_NOTE = 2
TAG_VOCABULARY_LIMIT = 40
# Broad product concepts must not be reused as AI tags across unrelated notes.
AUTO_TAG_BLOCKLIST = frozenset({
    "证据链", "智能问答", "知识库", "本地知识库", "知识管理", "本地化",
    "资料", "笔记", "项目", "工作", "技术", "学习", "生活", "其他",
})
WELCOME_TITLE = "欢迎使用 Pocket Memory"
WELCOME_CONTENT = """Pocket Memory 是一款本地优先的智能笔记工具。你的笔记、图片和本地模型都保存在这台电脑上，无需把内容发送到云端。

## 从这里开始

1. 点击右上角“新建笔记”，或按 `Ctrl+N` 记录第一条内容。
2. 可以直接粘贴截图、图片或 Excel 表格；图片会在后台自动识别文字。
3. 使用顶部搜索框查找笔记，或直接提问，让本地智能助手基于你的笔记回答。

## 你可以记录什么

- 工作中的待办、会议结论和项目线索
- 学习笔记、灵感、链接和代码片段
- 带截图的资料，或从 Excel 粘贴的表格

## 数据与备份

所有内容保存为 Markdown、SQLite 和附件文件。请定期在“设置 - 数据”中导出完整 ZIP 备份；备份包含笔记、页签、图片和数据库。

这是一篇系统欢迎笔记，不会参与你的搜索、统计、智能问答或向量索引。你可以保留它作为使用说明，也可以随时删除。"""


@dataclass
class Note:
    id: int
    title: str
    content: str
    markdown_path: str
    source_type: str
    created_at: str
    updated_at: str
    attachment_path: str | None = None
    category: str = "未分类"
    subcategory: str = ""
    tags: list[str] | None = None
    ocr_status: str = "not_applicable"
    ocr_text: str = ""
    ocr_error: str = ""
    ai_status: str = "pending"
    ai_error: str = ""
    ai_reason: str = ""
    category_source: str = "auto"
    subcategory_source: str = "auto"
    pinned: int = 0  # 0=未置顶, 1=已置顶
    favorite: int = 0  # 0=未收藏, 1=已收藏
    sort_order: int = 0
    folder_id: int | None = None
    parent_note_id: int | None = None  # 父笔记ID，None=独立笔记；非None=某笔记的一个页签
    tab_name: str = ""  # 页签名称（仅 parent_note_id 非空时有意义）
    tab_order: int = 0  # 页签顺序

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["tags"] = self.tags or []
        return payload


@dataclass
class Document:
    id: int
    title: str
    original_name: str
    stored_path: str
    file_type: str
    size_bytes: int
    sha256: str
    chunk_profile: str
    status: str
    error: str
    extracted_chars: int
    chunk_count: int
    created_at: str
    updated_at: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocumentChunk:
    id: int
    document_id: int
    title: str
    text: str
    location: str
    page: int | None
    section: str
    sheet: str
    cell_range: str
    chunk_order: int
    source_start_order: int
    source_end_order: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class NoteChunk:
    id: int
    note_id: int
    title: str
    text: str
    location: str
    kind: str
    chunk_order: int


class NoteStore:
    def __init__(self, data_dir: Path):
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self.data_dir = Path()
        self.notes_dir = Path()
        self.images_dir = Path()
        self.documents_dir = Path()
        self.db_path = Path()
        self._connect(data_dir)

    def _connect(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.notes_dir = self.data_dir / "notes"
        self.images_dir = self.data_dir / "attachments" / "images"
        self.documents_dir = self.data_dir / "attachments" / "documents"
        self.db_path = self.data_dir / "pocket_memory.db"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        self._purge_blocklisted_auto_tags()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Store is closed")
        return self._connection

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    markdown_path TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL DEFAULT 'text',
                    attachment_path TEXT,
                    category TEXT NOT NULL DEFAULT '未分类',
                    subcategory TEXT NOT NULL DEFAULT '',
                    ocr_status TEXT NOT NULL DEFAULT 'not_applicable',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    ocr_error TEXT NOT NULL DEFAULT '',
                    ai_status TEXT NOT NULL DEFAULT 'pending',
                    ai_error TEXT NOT NULL DEFAULT '',
                    ai_reason TEXT NOT NULL DEFAULT '',
                    category_source TEXT NOT NULL DEFAULT 'auto',
                    subcategory_source TEXT NOT NULL DEFAULT 'auto',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS note_tags (
                    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    source TEXT NOT NULL DEFAULT 'manual',
                    PRIMARY KEY (note_id, tag_id)
                );

                -- Tags explicitly removed by a user must not quietly return
                -- during the next automatic organisation pass.
                CREATE TABLE IF NOT EXISTS note_tag_exclusions (
                    group_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    tag_name TEXT NOT NULL,
                    PRIMARY KEY (group_note_id, tag_name)
                );

                CREATE TABLE IF NOT EXISTS embeddings (
                    note_id INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
                    vector BLOB NOT NULL,
                    dimension INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    title,
                    content,
                    tokenize='trigram',
                    content='notes',
                    content_rowid='id'
                );

                CREATE TABLE IF NOT EXISTS folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                -- 双向链接：source_id 笔记的 content 里写了 [[target_title]]
                -- target_id 可能为 NULL（目标笔记不存在 = 失效链接）
                -- target_title 始终存文本，跳转时按标题查找（容忍重名，取最近更新的一条）
                CREATE TABLE IF NOT EXISTS note_links (
                    source_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    target_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
                    target_title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, target_title)
                );

                CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                    INSERT INTO notes_fts(rowid, title, content)
                    VALUES (new.id, new.title, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, title, content)
                    VALUES ('delete', old.id, old.title, old.content);
                END;

                CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, title, content)
                    VALUES ('delete', old.id, old.title, old.content);
                    INSERT INTO notes_fts(rowid, title, content)
                    VALUES (new.id, new.title, new.content);
                END;

                -- 笔记历史版本：每次 update_note 时写入一条快照
                CREATE TABLE IF NOT EXISTS note_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    tab_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_note_versions_note_id ON note_versions(note_id, id DESC);

                CREATE TABLE IF NOT EXISTS note_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    location TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'content',
                    chunk_order INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_note_chunks_note ON note_chunks(note_id, chunk_order);
                CREATE TABLE IF NOT EXISTS note_chunk_embeddings (
                    chunk_id INTEGER PRIMARY KEY REFERENCES note_chunks(id) ON DELETE CASCADE,
                    vector BLOB NOT NULL,
                    dimension INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL UNIQUE,
                    file_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    chunk_profile TEXT NOT NULL DEFAULT 'standard',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    extracted_chars INTEGER NOT NULL DEFAULT 0,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    location TEXT NOT NULL,
                    page INTEGER,
                    section TEXT NOT NULL DEFAULT '',
                    sheet TEXT NOT NULL DEFAULT '',
                    cell_range TEXT NOT NULL DEFAULT '',
                    chunk_order INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_document_chunks_document ON document_chunks(document_id, chunk_order);

                CREATE TABLE IF NOT EXISTS document_chunk_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    document_sha256 TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    start_order INTEGER NOT NULL,
                    end_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, document_sha256, profile, start_order, end_order)
                );
                CREATE INDEX IF NOT EXISTS idx_document_chunk_adjustments_document
                    ON document_chunk_adjustments(document_id, document_sha256, profile);

                CREATE TABLE IF NOT EXISTS document_chunk_embeddings (
                    chunk_id INTEGER PRIMARY KEY REFERENCES document_chunks(id) ON DELETE CASCADE,
                    vector BLOB NOT NULL,
                    dimension INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
                    title,
                    text,
                    tokenize='trigram',
                    content='document_chunks',
                    content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS document_chunks_ai AFTER INSERT ON document_chunks BEGIN
                    INSERT INTO document_chunks_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS document_chunks_ad AFTER DELETE ON document_chunks BEGIN
                    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, title, text)
                    VALUES ('delete', old.id, old.title, old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS document_chunks_au AFTER UPDATE ON document_chunks BEGIN
                    INSERT INTO document_chunks_fts(document_chunks_fts, rowid, title, text)
                    VALUES ('delete', old.id, old.title, old.text);
                    INSERT INTO document_chunks_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
                END;
                CREATE TABLE IF NOT EXISTS wiki_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    template TEXT NOT NULL DEFAULT 'general',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_cards_updated ON wiki_cards(updated_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS wiki_card_sources (
                    card_id INTEGER NOT NULL REFERENCES wiki_cards(id) ON DELETE CASCADE,
                    source_kind TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    source_label TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(card_id, source_kind, source_id)
                );
                CREATE TABLE IF NOT EXISTS wiki_card_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    card_id INTEGER NOT NULL REFERENCES wiki_cards(id) ON DELETE CASCADE,
                    item_type TEXT NOT NULL,
                    field TEXT NOT NULL,
                    claim TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    sort_order INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_card_items_card ON wiki_card_items(card_id, sort_order, id);
                """
            )
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(notes)")}
            if "category" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN category TEXT NOT NULL DEFAULT '未分类'")
            if "subcategory" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN subcategory TEXT NOT NULL DEFAULT ''")
            if "ocr_status" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN ocr_status TEXT NOT NULL DEFAULT 'not_applicable'")
            if "ocr_text" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN ocr_text TEXT NOT NULL DEFAULT ''")
            if "ocr_error" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN ocr_error TEXT NOT NULL DEFAULT ''")
            if "ai_status" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN ai_status TEXT NOT NULL DEFAULT 'pending'")
            if "ai_error" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN ai_error TEXT NOT NULL DEFAULT ''")
            if "ai_reason" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN ai_reason TEXT NOT NULL DEFAULT ''")
            if "category_source" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN category_source TEXT NOT NULL DEFAULT 'auto'")
            if "subcategory_source" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN subcategory_source TEXT NOT NULL DEFAULT 'auto'")
            if "pinned" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            if "favorite" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
            if "sort_order" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
                rows = self.connection.execute(
                    """
                    SELECT id FROM notes
                    WHERE parent_note_id IS NULL AND source_type != 'welcome'
                    ORDER BY created_at DESC, id DESC
                    """
                ).fetchall()
                for index, row in enumerate(rows, 1):
                    self.connection.execute("UPDATE notes SET sort_order = ? WHERE id = ?", (index * 100, row["id"]))
            if "folder_id" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL")
            if "parent_note_id" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN parent_note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE")
            if "tab_name" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN tab_name TEXT NOT NULL DEFAULT ''")
            if "tab_order" not in columns:
                self.connection.execute("ALTER TABLE notes ADD COLUMN tab_order INTEGER NOT NULL DEFAULT 0")
            document_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(documents)")}
            if "chunk_profile" not in document_columns:
                self.connection.execute("ALTER TABLE documents ADD COLUMN chunk_profile TEXT NOT NULL DEFAULT 'standard'")
            chunk_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(document_chunks)")}
            if "source_start_order" not in chunk_columns:
                self.connection.execute("ALTER TABLE document_chunks ADD COLUMN source_start_order INTEGER NOT NULL DEFAULT 0")
                self.connection.execute("UPDATE document_chunks SET source_start_order = chunk_order")
            if "source_end_order" not in chunk_columns:
                self.connection.execute("ALTER TABLE document_chunks ADD COLUMN source_end_order INTEGER NOT NULL DEFAULT 0")
                self.connection.execute("UPDATE document_chunks SET source_end_order = chunk_order")
            self._migrate_legacy_folders()

    def create_note(
        self,
        title: str,
        content: str,
        image_data_url: str | None = None,
        image_name: str | None = None,
        category: str = "未分类",
        subcategory: str = "",
        tags: list[str] | None = None,
        folder_id: int | None = None,
        parent_note_id: int | None = None,
        tab_name: str = "",
        ai_status: str = "pending",
    ) -> Note:
        with self._lock:
            if len(content) > MAX_NOTE_CONTENT_CHARS:
                raise ValueError(f"单个页签最多支持 {MAX_NOTE_CONTENT_CHARS:,} 个字符")
            now = self._now()
            clean_title = self._clean_title(title, content)
            if folder_id is not None:
                category, subcategory = self._folder_to_category(folder_id)
            else:
                category = self._clean_category(category)
                subcategory = self._clean_category(subcategory, allow_empty=True)
            clean_tags = self._clean_tags(tags)
            category_source = "manual" if category != "未分类" else "auto"
            subcategory_source = "manual" if subcategory else "auto"
            attachment_path = None
            source_type = "text"
            ocr_status = "not_applicable"
            if image_data_url:
                attachment_path = self._save_image(image_data_url, image_name)
                source_type = "image"
                ocr_status = "pending"
            # 子页签继承父笔记的 folder_id
            if parent_note_id is not None:
                parent = self.get_note(parent_note_id)
                existing_tabs = self.connection.execute(
                    "SELECT COUNT(*) FROM notes WHERE parent_note_id = ?", (parent.id,)
                ).fetchone()[0]
                if existing_tabs >= MAX_TABS_PER_NOTE - 1:
                    raise ValueError(f"每篇笔记最多支持 {MAX_TABS_PER_NOTE} 个页签")
                folder_id = parent.folder_id
                category, subcategory = parent.category, parent.subcategory
            # 计算 tab_order：同组页签末尾
            tab_order = 0
            sort_order = 0
            if parent_note_id is not None:
                max_order = self.connection.execute(
                    "SELECT MAX(tab_order) AS m FROM notes WHERE parent_note_id = ? OR id = ?",
                    (parent_note_id, parent_note_id),
                ).fetchone()["m"]
                tab_order = (max_order or 0) + 1
            else:
                first_order = self.connection.execute(
                    "SELECT MIN(sort_order) AS first_order FROM notes WHERE parent_note_id IS NULL AND source_type != 'welcome'"
                ).fetchone()["first_order"]
                sort_order = (first_order if first_order is not None else 0) - 100
            markdown_path = self._markdown_path(now, clean_title)
            markdown = self._render_markdown(
                clean_title, content, now, source_type, attachment_path, category, subcategory, clean_tags, ocr_status
            )
            absolute_markdown_path = self.data_dir / markdown_path
            absolute_markdown_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_markdown_path.write_text(markdown, encoding="utf-8")
            with self.connection:
                cursor = self.connection.execute(
                    """
                    INSERT INTO notes(
                        title, content, markdown_path, source_type, attachment_path,
                        category, subcategory, ocr_status, ai_status, category_source, subcategory_source,
                        folder_id, parent_note_id, tab_name, tab_order, sort_order, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_title, content, markdown_path, source_type, attachment_path,
                        category, subcategory, ocr_status, ai_status, category_source, subcategory_source,
                        folder_id, parent_note_id, tab_name[:40], tab_order, sort_order, now, now,
                    ),
                )
                self._replace_tags(cursor.lastrowid, clean_tags, "manual")
            note = self.get_note(cursor.lastrowid)
            self._refresh_links(note.id, note.content)
            return note

    def ensure_welcome_note(self, image_path: Path) -> Note | None:
        """Create one visible onboarding note for a brand-new data directory.

        It is deliberately excluded from user search, statistics, RAG, and embeddings.
        Existing users with real notes never receive a surprise system note.
        """
        image_path = Path(image_path)
        with self._lock:
            existing = self.connection.execute(
                "SELECT id FROM notes WHERE source_type = ? AND parent_note_id IS NULL LIMIT 1",
                (WELCOME_SOURCE_TYPE,),
            ).fetchone()
            if existing is not None:
                return self._normalise_welcome_note(existing["id"])
            user_note = self.connection.execute(
                "SELECT 1 FROM notes WHERE source_type != ? LIMIT 1", (WELCOME_SOURCE_TYPE,)
            ).fetchone()
            if user_note is not None:
                return None
            if not image_path.is_file():
                raise FileNotFoundError(f"欢迎图片不存在: {image_path}")

            return self._create_welcome_note(image_path)

    def restore_welcome_note(self, image_path: Path) -> Note:
        """Explicitly restore the built-in onboarding note without touching user data."""
        image_path = Path(image_path)
        with self._lock:
            existing = self.connection.execute(
                "SELECT id FROM notes WHERE source_type = ? AND parent_note_id IS NULL LIMIT 1",
                (WELCOME_SOURCE_TYPE,),
            ).fetchone()
            if existing is not None:
                return self._normalise_welcome_note(existing["id"])
            if not image_path.is_file():
                raise FileNotFoundError(f"欢迎图片不存在: {image_path}")
            return self._create_welcome_note(image_path)

    def _create_welcome_note(self, image_path: Path) -> Note:
        now = self._now()
        relative_image_path = Path("attachments") / "images" / "pocket-memory-welcome.jpg"
        absolute_image_path = self.data_dir / relative_image_path
        absolute_image_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_image_path.write_bytes(image_path.read_bytes())
        markdown_path = self._markdown_path(now, WELCOME_TITLE)
        markdown = self._render_markdown(
            WELCOME_TITLE,
            WELCOME_CONTENT,
            now,
            WELCOME_SOURCE_TYPE,
            relative_image_path.as_posix(),
            "未分类",
            "",
            [],
            "not_applicable",
        )
        absolute_markdown_path = self.data_dir / markdown_path
        absolute_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_markdown_path.write_text(markdown, encoding="utf-8")
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO notes(
                    title, content, markdown_path, source_type, attachment_path,
                    category, subcategory, ocr_status, ai_status,
                    category_source, subcategory_source, pinned, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', 'manual', 'manual', 1, ?, ?)
                """,
                (
                    WELCOME_TITLE,
                    WELCOME_CONTENT,
                    markdown_path,
                    WELCOME_SOURCE_TYPE,
                    relative_image_path.as_posix(),
                    "未分类",
                    "",
                    "not_applicable",
                    now,
                    now,
                ),
            )
        return self.get_note(cursor.lastrowid)

    def _normalise_welcome_note(self, note_id: int) -> Note:
        """Keep the built-in welcome note out of the user's folder taxonomy.

        Older versions stored it under ``开始使用``. That created a misleading
        navigation folder even though welcome content is excluded from search,
        statistics, RAG, and embeddings. Only that previous system folder is
        removed, and only after it is confirmed empty.
        """
        note = self.get_note(note_id)
        if note.source_type != WELCOME_SOURCE_TYPE:
            return note
        previous_folder_id = note.folder_id
        if note.category == "未分类" and not note.subcategory and previous_folder_id is None:
            return note
        with self.connection:
            self.connection.execute(
                """
                UPDATE notes
                SET category = '未分类', subcategory = '', folder_id = NULL,
                    category_source = 'manual', subcategory_source = 'manual'
                WHERE id = ?
                """,
                (note_id,),
            )
            if previous_folder_id is not None:
                folder = self.connection.execute(
                    "SELECT id, name FROM folders WHERE id = ?", (previous_folder_id,)
                ).fetchone()
                remaining = self.connection.execute(
                    "SELECT COUNT(*) FROM notes WHERE folder_id = ?", (previous_folder_id,)
                ).fetchone()[0]
                if folder is not None and folder["name"] == "开始使用" and remaining == 0:
                    self.connection.execute("DELETE FROM folders WHERE id = ?", (previous_folder_id,))
        normalised = self.get_note(note_id)
        self._rewrite_markdown(normalised)
        return normalised

    def update_note(
        self,
        note_id: int,
        title: str = ...,
        content: str = ...,
        category: str = ...,
        subcategory: str = ...,
        tags: list[str] | None = ...,
        folder_id: int | None = ...,
        tab_name: str | None = None,
    ) -> Note:
        with self._lock:
            existing = self.get_note(note_id)
            now = self._now()
            category_was_supplied = category is not ...
            subcategory_was_supplied = subcategory is not ...
            tags_were_supplied = tags is not ...
            folder_was_supplied = folder_id is not ...
            # 部分更新：... 表示不修改，用 existing 值回填
            if title is ...:
                title = existing.title
            if content is ...:
                content = existing.content
            if len(content) > MAX_NOTE_CONTENT_CHARS:
                raise ValueError(f"单个页签最多支持 {MAX_NOTE_CONTENT_CHARS:,} 个字符")
            if category is ...:
                category = existing.category
            if subcategory is ...:
                subcategory = existing.subcategory
            if tags is ...:
                tags = existing.tags
            clean_title = self._clean_title(title, content)
            # folder_id 为 ... 表示不修改，None 表示移到未归类，整数表示移到指定 folder
            if folder_id is ...:
                folder_id = existing.folder_id
            if folder_id is not None:
                category, subcategory = self._folder_to_category(folder_id)
            else:
                category = self._clean_category(category)
                subcategory = self._clean_category(subcategory, allow_empty=True)
            clean_tags = self._clean_tags(tags)
            # Saving text must not silently turn prior AI metadata into manual
            # metadata. Only an explicit category/folder operation takes ownership.
            category_source = existing.category_source
            subcategory_source = existing.subcategory_source
            if category_was_supplied or folder_was_supplied:
                category_source = "manual" if category != "未分类" else "auto"
            if subcategory_was_supplied or folder_was_supplied:
                subcategory_source = "manual" if subcategory else "auto"
            # 写入历史版本快照：仅当内容/标签/分类等关键字段有变化时才记录，避免无意义的重复版本
            old_tags_str = ",".join(existing.tags or [])
            new_tags_str = ",".join(clean_tags)
            content_changed = (
                existing.title != clean_title
                or existing.content != content
                or existing.category != category
                or existing.subcategory != subcategory
                or old_tags_str != new_tags_str
            )
            if content_changed:
                self._save_version(existing, old_tags_str)
            markdown = self._render_markdown(
                clean_title,
                content,
                existing.created_at,
                existing.source_type,
                existing.attachment_path,
                category,
                subcategory,
                clean_tags,
                existing.ocr_status,
                existing.ocr_text,
                existing.ai_status,
                updated_at=now,
            )
            (self.data_dir / existing.markdown_path).write_text(markdown, encoding="utf-8")
            with self.connection:
                if tab_name is not None:
                    self.connection.execute(
                        """
                        UPDATE notes
                        SET title = ?, content = ?, category = ?, subcategory = ?,
                            category_source = ?, subcategory_source = ?, folder_id = ?, tab_name = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (clean_title, content, category, subcategory, category_source, subcategory_source, folder_id, tab_name[:40], now, note_id),
                    )
                else:
                    self.connection.execute(
                        """
                        UPDATE notes
                        SET title = ?, content = ?, category = ?, subcategory = ?,
                            category_source = ?, subcategory_source = ?, folder_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (clean_title, content, category, subcategory, category_source, subcategory_source, folder_id, now, note_id),
                    )
                if tags_were_supplied:
                    # Tags belong to the complete note, not an individual tab.
                    # Keep every tab aligned when the user edits them from either
                    # the main page or a child tab.
                    group_id = existing.parent_note_id or existing.id
                    group_member_ids = [
                        row["id"]
                        for row in self.connection.execute(
                            "SELECT id FROM notes WHERE id = ? OR parent_note_id = ?",
                            (group_id, group_id),
                        )
                    ]
                    for member_id in group_member_ids:
                        self._replace_tags(member_id, clean_tags, "manual")
                    if clean_tags:
                        placeholders = ",".join("?" * len(clean_tags))
                        self.connection.execute(
                            f"DELETE FROM note_tag_exclusions WHERE group_note_id = ? AND tag_name IN ({placeholders})",
                            (group_id, *clean_tags),
                        )
                # If this is the main note, synchronise explicit folder/category
                # updates to every tab. Tags only synchronise when the user edited
                # them, so ordinary text saves keep automatic tags automatic.
                if existing.parent_note_id is None:
                    child_rows = self.connection.execute(
                        "SELECT id FROM notes WHERE parent_note_id = ?", (note_id,)
                    ).fetchall()
                    for child in child_rows:
                        cid = child["id"]
                        self.connection.execute(
                            """
                            UPDATE notes
                            SET category = ?, subcategory = ?, category_source = ?, subcategory_source = ?, folder_id = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (category, subcategory, category_source, subcategory_source, folder_id, now, cid),
                        )
            if tags_were_supplied:
                for member in self.get_note_group(note_id):
                    self._rewrite_markdown(member)
            note = self.get_note(note_id)
            self._refresh_links(note.id, note.content)
            return note

    def delete_note(self, note_id: int) -> None:
        with self._lock:
            existing = self.get_note(note_id)
            with self.connection:
                self.connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                # 同步删除向量，避免删除笔记后 embeddings 表残留孤儿记录
                # （否则 embedding_count 会大于实际笔记数，且语义检索会因 get_note 失败而报错）
                self.connection.execute("DELETE FROM embeddings WHERE note_id = ?", (note_id,))
            (self.data_dir / existing.markdown_path).unlink(missing_ok=True)
            if existing.attachment_path:
                (self.data_dir / existing.attachment_path).unlink(missing_ok=True)

    def get_note(self, note_id: int) -> Note:
        with self._lock:
            row = self.connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise KeyError(note_id)
            note = Note(**dict(row))
            note.tags = self._get_tags(note_id)
            return note

    # ---------- 双向链接 ----------
    _LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")

    def _refresh_links(self, note_id: int, content: str) -> None:
        """解析笔记 content 中的 [[标题]]，重建该笔记的出链关系。

        在 create_note / update_note 后调用。重名标题取最近更新的目标笔记。
        """
        with self._lock:
            # 提取所有 [[标题]]，去重保序
            titles: list[str] = []
            seen: set[str] = set()
            for m in self._LINK_PATTERN.finditer(content or ""):
                t = m.group(1).strip()
                # 支持 [[标题|别名]] 语法，取 | 前的标题
                if "|" in t:
                    t = t.split("|", 1)[0].strip()
                if t and t not in seen:
                    seen.add(t)
                    titles.append(t)
            now = self._now()
            with self.connection:
                # 先删旧出链
                self.connection.execute("DELETE FROM note_links WHERE source_id = ?", (note_id,))
                for title in titles:
                    # 按标题精确匹配，重名取最近更新的一条
                    row = self.connection.execute(
                        "SELECT id FROM notes WHERE title = ? AND parent_note_id IS NULL "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (title,),
                    ).fetchone()
                    target_id = row["id"] if row else None
                    self.connection.execute(
                        "INSERT OR REPLACE INTO note_links(source_id, target_id, target_title, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (note_id, target_id, title, now),
                    )

    def get_outlinks(self, note_id: int) -> list[dict]:
        """获取笔记的出链（该笔记引用了哪些笔记）。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT target_id, target_title FROM note_links WHERE source_id = ? ORDER BY target_title",
                (note_id,),
            ).fetchall()
            return [{"target_id": r["target_id"], "target_title": r["target_title"]} for r in rows]

    def get_backlinks(self, note_id: int) -> list[dict]:
        """获取笔记的反链（哪些笔记引用了该笔记）。"""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT nl.source_id, n.title AS source_title
                FROM note_links nl
                LEFT JOIN notes n ON n.id = nl.source_id
                WHERE nl.target_id = ?
                ORDER BY n.updated_at DESC
                """,
                (note_id,),
            ).fetchall()
            return [{"source_id": r["source_id"], "source_title": r["source_title"] or "未命名笔记"} for r in rows]

    def backlink_count(self, note_id: int) -> int:
        """获取笔记的反链数量。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS c FROM note_links WHERE target_id = ?", (note_id,)
            ).fetchone()
            return row["c"] if row else 0

    def get_broken_links(self) -> list[dict]:
        """获取所有失效链接（target_id 为 NULL 的出链）。"""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT nl.source_id, n.title AS source_title, nl.target_title
                FROM note_links nl
                JOIN notes n ON n.id = nl.source_id
                WHERE nl.target_id IS NULL
                ORDER BY nl.target_title
                """,
            ).fetchall()
            return [
                {"source_id": r["source_id"], "source_title": r["source_title"] or "未命名笔记",
                 "target_title": r["target_title"]}
                for r in rows
            ]

    def find_note_by_title(self, title: str) -> Note | None:
        """按标题精确查找笔记（重名取最近更新的一条）。用于双链跳转。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT id FROM notes WHERE title = ? AND parent_note_id IS NULL "
                "ORDER BY updated_at DESC LIMIT 1",
                (title,),
            ).fetchone()
            if row is None:
                return None
            return self.get_note(row["id"])

    def get_note_tabs(self, note_id: int) -> list[dict]:
        """获取笔记所属组的所有页签（主笔记 + 子页签），按 tab_order 排序。
        主笔记本身是第一个页签（tab_name 为空或"首页"）。"""
        with self._lock:
            note = self.get_note(note_id)
            group_id = note.parent_note_id or note_id
            rows = self.connection.execute(
                """
                SELECT id, title, tab_name, tab_order, parent_note_id
                FROM notes
                WHERE id = ? OR parent_note_id = ?
                ORDER BY CASE WHEN parent_note_id IS NULL THEN 0 ELSE 1 END, tab_order, id
                """,
                (group_id, group_id),
            ).fetchall()
            return [{"id": r["id"], "title": r["title"], "tab_name": r["tab_name"], "tab_order": r["tab_order"]} for r in rows]

    def get_note_group(self, note_id: int) -> list[Note]:
        """Return a main note and every tab in its stable display order."""
        with self._lock:
            note = self.get_note(note_id)
            group_id = note.parent_note_id or note.id
            rows = self.connection.execute(
                """
                SELECT id FROM notes
                WHERE id = ? OR parent_note_id = ?
                ORDER BY CASE WHEN parent_note_id IS NULL THEN 0 ELSE 1 END, tab_order, id
                """,
                (group_id, group_id),
            ).fetchall()
            return [self.get_note(row["id"]) for row in rows]

    def remove_group_tag(self, note_id: int, tag: str) -> Note:
        """Remove a tag from a note group and remember the user's choice."""
        clean_tags = self._clean_tags([tag])
        if not clean_tags:
            raise ValueError("标签不能为空")
        tag_name = clean_tags[0]
        with self._lock, self.connection:
            note = self.get_note(note_id)
            group_id = note.parent_note_id or note.id
            member_rows = self.connection.execute(
                "SELECT id FROM notes WHERE id = ? OR parent_note_id = ?", (group_id, group_id)
            ).fetchall()
            member_ids = [row["id"] for row in member_rows]
            placeholders = ",".join("?" * len(member_ids))
            self.connection.execute(
                f"""
                DELETE FROM note_tags
                WHERE note_id IN ({placeholders})
                  AND tag_id IN (SELECT id FROM tags WHERE name = ?)
                """,
                (*member_ids, tag_name),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO note_tag_exclusions(group_note_id, tag_name) VALUES (?, ?)",
                (group_id, tag_name),
            )
            for member_id in member_ids:
                self._rewrite_markdown(self.get_note(member_id))
            return self.get_note(note_id)

    def tab_count(self, note_id: int) -> int:
        """返回该笔记的子页签数量（不含主笔记自身）。"""
        with self._lock:
            note = self.get_note(note_id)
            group_id = note.parent_note_id or note_id
            return self.connection.execute(
                "SELECT COUNT(*) FROM notes WHERE parent_note_id = ?", (group_id,)
            ).fetchone()[0]

    def export_notes(self) -> list[dict]:
        """导出主笔记列表（用于时间线和知识图谱）。"""
        notes = self.list_notes(limit=100000)
        return [note.as_dict() for note in notes]

    def export_all_notes(self) -> list[dict]:
        """导出所有笔记记录，包含主笔记和子页签。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT id FROM notes ORDER BY created_at, id"
            ).fetchall()
            return [self.get_note(row["id"]).as_dict() for row in rows]

    def create_backup(self, destination: Path) -> Path:
        """创建可恢复的 ZIP 备份，包含 SQLite 快照及全部用户文件。"""
        destination = Path(destination).resolve()
        with self._lock:
            if destination.is_relative_to(self.data_dir):
                raise ValueError("备份文件不能保存到当前数据目录内")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_db_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix="pocket-memory-backup-", suffix=".db", delete=False
                ) as temp_file:
                    temp_db_path = Path(temp_file.name)
                # The application-wide lock prevents concurrent writes. Every write
                # method already uses a committed SQLite transaction, so copying the
                # database file avoids Connection.backup() blocking in frozen builds.
                shutil.copy2(self.db_path, temp_db_path)

                with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.write(temp_db_path, self.db_path.name)
                    for path in self.data_dir.rglob("*"):
                        if not path.is_file():
                            continue
                        relative = path.relative_to(self.data_dir)
                        if relative == Path(self.db_path.name) or relative.name == ".pocket-memory.lock":
                            continue
                        if relative.parts and relative.parts[0] == "logs":
                            continue
                        archive.write(path, relative.as_posix())
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            {
                                "format": "pocket-memory-backup",
                                "version": 1,
                                "created_at": self._now(),
                                "note_count": self.note_count(),
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
            finally:
                if temp_db_path is not None:
                    temp_db_path.unlink(missing_ok=True)
        return destination

    def toggle_pin(self, note_id: int) -> int:
        """切换笔记置顶状态，返回新的 pinned 值（0 或 1）。"""
        with self._lock, self.connection:
            current = self.connection.execute(
                "SELECT pinned FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            if current is None:
                raise KeyError(note_id)
            new_value = 0 if current["pinned"] else 1
            self.connection.execute(
                "UPDATE notes SET pinned = ?, updated_at = ? WHERE id = ?",
                (new_value, self._now(), note_id),
            )
            return new_value

    def toggle_favorite(self, note_id: int) -> int:
        """Toggle the collection state for a complete note and all its tabs."""
        with self._lock, self.connection:
            note = self.get_note(note_id)
            group_id = note.parent_note_id or note.id
            current = self.connection.execute(
                "SELECT favorite FROM notes WHERE id = ?", (group_id,)
            ).fetchone()
            if current is None:
                raise KeyError(note_id)
            new_value = 0 if current["favorite"] else 1
            self.connection.execute(
                "UPDATE notes SET favorite = ?, updated_at = ? WHERE id = ? OR parent_note_id = ?",
                (new_value, self._now(), group_id, group_id),
            )
            return new_value

    def move_note(self, note_id: int, target_note_id: int, after: bool = False) -> None:
        """Persist a user-defined order within the pinned or regular note section."""
        with self._lock, self.connection:
            note = self.get_note(note_id)
            target = self.get_note(target_note_id)
            group_id = note.parent_note_id or note.id
            target_group_id = target.parent_note_id or target.id
            if group_id == target_group_id:
                return
            source = self.get_note(group_id)
            target_root = self.get_note(target_group_id)
            if bool(source.pinned) != bool(target_root.pinned):
                raise ValueError("置顶笔记与普通笔记请分别调整顺序")
            rows = self.connection.execute(
                """
                SELECT id FROM notes
                WHERE parent_note_id IS NULL AND source_type != 'welcome' AND pinned = ?
                ORDER BY sort_order ASC, created_at DESC, id DESC
                """,
                (source.pinned,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if group_id not in ids or target_group_id not in ids:
                raise ValueError("只能在当前普通笔记列表或置顶笔记列表内调整顺序")
            ids.remove(group_id)
            target_index = ids.index(target_group_id)
            ids.insert(target_index + (1 if after else 0), group_id)
            for index, current_id in enumerate(ids, 1):
                self.connection.execute("UPDATE notes SET sort_order = ? WHERE id = ?", (index * 100, current_id))

    def create_folder(self, parent_id: int | None, name: str) -> dict:
        with self._lock, self.connection:
            name = (name or "").strip()[:80]
            if not name:
                raise ValueError("名称不能为空")
            cursor = self.connection.execute(
                "INSERT INTO folders(parent_id, name, sort_order, created_at) VALUES (?, ?, 0, ?)",
                (parent_id, name, self._now()),
            )
            return self.get_folder(cursor.lastrowid)

    def get_folder(self, folder_id: int) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
            if row is None:
                raise KeyError(folder_id)
            return dict(row)

    def get_descendant_folder_ids(self, folder_id: int) -> list[int]:
        """返回 folder_id 自身及所有子孙 folder 的 id 列表（递归 CTE）。"""
        with self._lock:
            rows = self.connection.execute(
                "WITH RECURSIVE descendants(id) AS ("
                "SELECT id FROM folders WHERE id = ? "
                "UNION ALL "
                "SELECT f.id FROM folders f JOIN descendants d ON f.parent_id = d.id"
                ") SELECT id FROM descendants",
                (folder_id,),
            ).fetchall()
            return [row["id"] for row in rows]

    def list_folders(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM folders ORDER BY sort_order, name")]

    def update_folder(self, folder_id: int, name: str) -> dict:
        with self._lock, self.connection:
            name = (name or "").strip()[:80]
            if not name:
                raise ValueError("名称不能为空")
            self.connection.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
            # 递归更新该 folder 及所有子孙 folder 下笔记的 category/subcategory
            self._sync_descendant_categories(folder_id)
            return self.get_folder(folder_id)

    def _sync_descendant_categories(self, folder_id: int) -> None:
        """递归更新 folder 及其子孙 folder 下所有笔记的 category/subcategory。"""
        category, subcategory = self._folder_to_category(folder_id)
        self.connection.execute(
            "UPDATE notes SET category = ?, subcategory = ? WHERE folder_id = ?",
            (category, subcategory, folder_id),
        )
        children = self.connection.execute(
            "SELECT id FROM folders WHERE parent_id = ?", (folder_id,)
        ).fetchall()
        for child in children:
            self._sync_descendant_categories(child["id"])

    def delete_folder(self, folder_id: int) -> None:
        with self._lock, self.connection:
            # 子 folder 和笔记通过外键级联/置空
            self.connection.execute("UPDATE notes SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
            self.connection.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    def get_folders_tree(self) -> list[dict]:
        with self._lock:
            # AI organisation stores a category first. Keep the visible folder
            # tree in sync for older notes created before folder binding existed.
            self._reconcile_category_folders()
            folders = self.list_folders()
            direct_counts = {
                row["folder_id"]: row["c"]
                for row in self.connection.execute(
                    "SELECT folder_id, COUNT(*) AS c FROM notes "
                    "WHERE folder_id IS NOT NULL AND parent_note_id IS NULL "
                    "GROUP BY folder_id"
                )
            }
            by_parent: dict[int | None, list[dict]] = {}
            for f in folders:
                by_parent.setdefault(f["parent_id"], []).append(f)

            def build(parent_id: int | None) -> list[dict]:
                children = [build(f["id"]) for f in by_parent.get(parent_id, [])]
                return [
                    {
                        "id": f["id"],
                        "name": f["name"],
                        "parent_id": f["parent_id"],
                        "sort_order": f["sort_order"],
                        "note_count": direct_counts.get(f["id"], 0),
                        "children": children[i],
                    }
                    for i, f in enumerate(by_parent.get(parent_id, []))
                ]

            def with_total_count(nodes: list[dict]) -> list[dict]:
                """递归填充 note_count 为自身直接笔记数 + 所有子孙 folder 笔记数。"""
                for node in nodes:
                    node["note_count"] = node["note_count"] + sum(
                        child["note_count"] for child in with_total_count(node["children"])
                    )
                return nodes

            return with_total_count(build(None))

    def _folder_to_category(self, folder_id: int) -> tuple[str, str]:
        """从 folder 向上推导 category（顶级 folder 名）和 subcategory（二级 folder 名）。"""
        with self._lock:
            path: list[str] = []
            current_id: int | None = folder_id
            while current_id is not None:
                row = self.connection.execute(
                    "SELECT id, parent_id, name FROM folders WHERE id = ?", (current_id,)
                ).fetchone()
                if row is None:
                    break
                path.append(row["name"])
                current_id = row["parent_id"]
            if not path:
                return "未分类", ""
            path.reverse()
            category = path[0]
            subcategory = path[1] if len(path) > 1 else ""
            return category, subcategory

    def _ensure_category_folder(self, category: str, subcategory: str) -> int | None:
        """Return the matching folder path, creating it when a category is usable.

        The caller owns the transaction. Categories are stored as note metadata,
        while navigation uses folders; this helper keeps both representations
        aligned without changing an existing folder choice.
        """
        category = self._clean_category(category)
        subcategory = self._clean_category(subcategory, allow_empty=True)
        if category == "未分类":
            return None
        category_row = self.connection.execute(
            "SELECT id FROM folders WHERE parent_id IS NULL AND name = ?", (category,)
        ).fetchone()
        category_id = category_row["id"] if category_row else self.connection.execute(
            "INSERT INTO folders(parent_id, name, sort_order, created_at) VALUES (NULL, ?, 0, ?)",
            (category, self._now()),
        ).lastrowid
        if not subcategory:
            return category_id
        subcategory_row = self.connection.execute(
            "SELECT id FROM folders WHERE parent_id = ? AND name = ?", (category_id, subcategory)
        ).fetchone()
        return subcategory_row["id"] if subcategory_row else self.connection.execute(
            "INSERT INTO folders(parent_id, name, sort_order, created_at) VALUES (?, ?, 0, ?)",
            (category_id, subcategory, self._now()),
        ).lastrowid

    def _reconcile_category_folders(self) -> None:
        """Bind unassigned categorised note groups to their visible folder path.

        It is idempotent and only fills a missing ``folder_id``. A user-selected
        folder is never moved by an AI category refresh.
        """
        with self._lock, self.connection:
            roots = self.connection.execute(
                """
                SELECT id, category, subcategory FROM notes
                WHERE parent_note_id IS NULL
                  AND folder_id IS NULL
                  AND category != '未分类'
                  AND source_type != 'welcome'
                """
            ).fetchall()
            for root in roots:
                folder_id = self._ensure_category_folder(root["category"], root["subcategory"])
                if folder_id is None:
                    continue
                # Pages are a single note group for navigation as well.
                self.connection.execute(
                    "UPDATE notes SET folder_id = ? WHERE (id = ? OR parent_note_id = ?) AND folder_id IS NULL",
                    (folder_id, root["id"], root["id"]),
                )

    def _migrate_legacy_folders(self) -> None:
        """Compatibility name for the idempotent category-to-folder repair."""
        self._reconcile_category_folders()

    def list_notes(
        self,
        limit: int = 200,
        category: str | None = None,
        subcategory: str | None = None,
        tag: str | None = None,
        since: str | None = None,
        folder_id: int | None = None,
        folder_ids: list[int] | None = None,
        include_welcome: bool = False,
        favorite: bool | None = None,
    ) -> list[Note]:
        with self._lock:
            if folder_ids is None and folder_id is not None:
                folder_ids = self.get_descendant_folder_ids(folder_id) or [folder_id]
            clauses, parameters = self._filter_clauses(category, subcategory, tag, since, folder_ids, favorite)
            # 默认只显示主笔记（parent_note_id IS NULL），子页签不单独出现在时间线
            clauses.append("notes.parent_note_id IS NULL")
            if not include_welcome:
                clauses.append("notes.source_type != 'welcome'")
            rows = self.connection.execute(
                f"""
                SELECT DISTINCT notes.*
                FROM notes
                {'JOIN note_tags ON note_tags.note_id = notes.id JOIN tags ON tags.id = note_tags.tag_id' if tag else ''}
                {'WHERE ' + ' AND '.join(clauses) if clauses else ''}
                ORDER BY notes.pinned DESC, notes.sort_order ASC, notes.created_at DESC, notes.id DESC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
            return [self.get_note(row["id"]) for row in rows]

    def search_notes(
        self,
        query: str,
        limit: int = 100,
        category: str | None = None,
        subcategory: str | None = None,
        tag: str | None = None,
        since: str | None = None,
        folder_id: int | None = None,
        folder_ids: list[int] | None = None,
        favorite: bool | None = None,
    ) -> list[Note]:
        query = query.strip()
        if not query:
            return self.list_notes(
                limit, category, subcategory, tag, since, folder_id, folder_ids, favorite=favorite
            )
        with self._lock:
            if folder_ids is None and folder_id is not None:
                folder_ids = self.get_descendant_folder_ids(folder_id) or [folder_id]
            clauses, parameters = self._filter_clauses(category, subcategory, tag, since, folder_ids, favorite)
            # 搜索时也只返回主笔记，但匹配范围包含子页签内容
            clauses.extend(("notes.parent_note_id IS NULL", "notes.source_type != 'welcome'"))
            filter_sql = " AND " + " AND ".join(clauses) if clauses else ""
            tag_join = "JOIN note_tags ON note_tags.note_id = notes.id JOIN tags ON tags.id = note_tags.tag_id" if tag else ""

            # 多词拆分：如果查询包含连接词（和、与、空格等），拆分成多个词做 AND 匹配
            terms = self._split_query(query)
            if len(terms) <= 1:
                # 单词搜索：保持原逻辑（子串匹配 + FTS）
                tab_match = (
                    "OR notes.id IN (SELECT parent_note_id FROM notes sub "
                    "WHERE sub.parent_note_id IS NOT NULL "
                    "AND (instr(sub.title, ?) > 0 OR instr(sub.content, ?) > 0))"
                )
                substring_rows = self.connection.execute(
                    f"""
                    SELECT DISTINCT notes.*
                    FROM notes
                    {tag_join}
                    WHERE (instr(notes.title, ?) > 0 OR instr(notes.content, ?) > 0 OR instr(notes.ocr_text, ?) > 0
                        {tab_match})
                    {filter_sql}
                    ORDER BY notes.created_at DESC, notes.id DESC
                    LIMIT ?
                    """,
                    (query, query, query, query, query, *parameters, limit),
                ).fetchall()
                notes = [self.get_note(row["id"]) for row in substring_rows]
            else:
                # 多词搜索：每个词都必须出现在 title/content/ocr_text 中（含子页签）
                # 逐词筛选：先匹配第一个词，再在结果中过滤剩余词
                first_term = terms[0]
                tab_match = (
                    "OR notes.id IN (SELECT parent_note_id FROM notes sub "
                    "WHERE sub.parent_note_id IS NOT NULL "
                    "AND (instr(sub.title, ?) > 0 OR instr(sub.content, ?) > 0))"
                )
                substring_rows = self.connection.execute(
                    f"""
                    SELECT DISTINCT notes.*
                    FROM notes
                    {tag_join}
                    WHERE (instr(notes.title, ?) > 0 OR instr(notes.content, ?) > 0 OR instr(notes.ocr_text, ?) > 0
                        {tab_match})
                    {filter_sql}
                    ORDER BY notes.created_at DESC, notes.id DESC
                    LIMIT ?
                    """,
                    (first_term, first_term, first_term, first_term, first_term, *parameters, limit),
                ).fetchall()
                notes = []
                for row in substring_rows:
                    note = self.get_note(row["id"])
                    # 检查所有词是否都出现在 note 的某个字段中（含子页签）
                    all_text = (note.title or "") + " " + (note.content or "") + " " + (note.ocr_text or "")
                    for tab in self.get_note_tabs(note.id):
                        tab_note = self.get_note(tab["id"])
                        if tab_note.id != note.id:
                            all_text += " " + (tab_note.title or "") + " " + (tab_note.content or "") + " " + (tab_note.ocr_text or "")
                    all_text_lower = all_text.lower()
                    if all(term.lower() in all_text_lower for term in terms):
                        notes.append(note)
            if len(query) < 3 or len(notes) >= limit:
                return notes
            fts_rows = self.connection.execute(
                f"""
                SELECT DISTINCT notes.*
                FROM notes_fts
                JOIN notes ON notes.id = notes_fts.rowid
                {tag_join}
                WHERE notes_fts MATCH ?
                {filter_sql}
                ORDER BY bm25(notes_fts), notes.created_at DESC
                LIMIT ?
                """,
                (self._fts_query(query), *parameters, limit),
            ).fetchall()
            seen = {note.id for note in notes}
            notes.extend(self.get_note(row["id"]) for row in fts_rows if row["id"] not in seen)
            return notes[:limit]

    def get_taxonomy(self) -> dict:
        with self._lock:
            # 分类/标签计数都只统计主笔记（parent_note_id IS NULL）
            categories: dict[str, dict] = {}
            for row in self.connection.execute(
                """
                SELECT category, subcategory, COUNT(*) AS count
                FROM notes
                WHERE parent_note_id IS NULL AND source_type != 'welcome'
                GROUP BY category, subcategory
                ORDER BY category, subcategory
                """
            ):
                category = categories.setdefault(row["category"], {"name": row["category"], "count": 0, "children": []})
                category["count"] += row["count"]
                if row["subcategory"]:
                    category["children"].append({"name": row["subcategory"], "count": row["count"]})
            # 标签计数只统计主笔记（parent_note_id IS NULL），
            # 子页签的标签是从父笔记同步的，重复计数会让用户困惑
            tags = [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT tags.name, COUNT(DISTINCT notes.id) AS count
                    FROM tags
                    JOIN note_tags ON note_tags.tag_id = tags.id
                    JOIN notes ON notes.id = note_tags.note_id
                    WHERE notes.parent_note_id IS NULL AND notes.source_type != 'welcome'
                    GROUP BY tags.id ORDER BY count DESC, tags.name
                    """
                )
            ]
            total = self.connection.execute(
                "SELECT COUNT(*) FROM notes WHERE parent_note_id IS NULL AND source_type != 'welcome'"
            ).fetchone()[0]
            return {"total": total, "categories": list(categories.values()), "tags": tags}

    def tag_vocabulary(self, limit: int = TAG_VOCABULARY_LIMIT) -> list[str]:
        """Return the most useful existing tags for constrained AI generation."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT tags.name, COUNT(DISTINCT notes.id) AS count
                FROM tags
                JOIN note_tags ON note_tags.tag_id = tags.id
                JOIN notes ON notes.id = note_tags.note_id
                WHERE notes.parent_note_id IS NULL AND notes.source_type != 'welcome'
                GROUP BY tags.id
                HAVING count >= 2
                ORDER BY count DESC, tags.name
                LIMIT ?
                """,
                (max(limit * 3, limit),),
            ).fetchall()
            return [
                row["name"] for row in rows
                if self._is_auto_tag_allowed(row["name"])
            ][:limit]

    def tag_governance(self) -> dict:
        """Expose an auditable tag inventory and conservative merge suggestions."""
        with self._lock:
            rows = [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT tags.name, COUNT(DISTINCT notes.id) AS count,
                           SUM(CASE WHEN note_tags.source = 'manual' THEN 1 ELSE 0 END) AS manual_uses,
                           SUM(CASE WHEN note_tags.source = 'auto' THEN 1 ELSE 0 END) AS auto_uses
                    FROM tags
                    JOIN note_tags ON note_tags.tag_id = tags.id
                    JOIN notes ON notes.id = note_tags.note_id
                    WHERE notes.parent_note_id IS NULL AND notes.source_type != 'welcome'
                    GROUP BY tags.id
                    ORDER BY count DESC, tags.name
                    """
                )
            ]
            core = [row for row in rows if row["count"] >= 2]
            suggestions = []
            for candidate in (row for row in rows if row["count"] == 1 and not row["manual_uses"]):
                candidate_key = self._tag_key(candidate["name"])
                for target in core:
                    target_key = self._tag_key(target["name"])
                    similar = (
                        candidate_key in target_key
                        or target_key in candidate_key
                        or SequenceMatcher(None, candidate_key, target_key).ratio() >= 0.82
                    )
                    if similar:
                        suggestions.append({
                            "from": candidate["name"],
                            "to": target["name"],
                            "reason": "低频且名称高度相近",
                        })
                        break
            return {
                "total": len(rows),
                "core_tags": core[:TAG_VOCABULARY_LIMIT],
                "rare_tags": [row for row in rows if row["count"] == 1][:TAG_VOCABULARY_LIMIT],
                "suggestions": suggestions[:20],
            }

    def merge_tags(self, source_name: str, target_name: str) -> int:
        """Merge one reviewed tag into another and rewrite affected Markdown files."""
        source_name = self._clean_tags([source_name])[0] if self._clean_tags([source_name]) else ""
        target_name = self._clean_tags([target_name])[0] if self._clean_tags([target_name]) else ""
        if not source_name or not target_name:
            raise ValueError("标签名称不能为空")
        if source_name == target_name:
            raise ValueError("请选择两个不同的标签")
        with self._lock:
            source = self.connection.execute("SELECT id FROM tags WHERE name = ?", (source_name,)).fetchone()
            if source is None:
                raise KeyError(f"标签不存在: {source_name}")
            target = self.connection.execute("SELECT id FROM tags WHERE name = ?", (target_name,)).fetchone()
            with self.connection:
                if target is None:
                    cursor = self.connection.execute("INSERT INTO tags(name) VALUES (?)", (target_name,))
                    target_id = cursor.lastrowid
                else:
                    target_id = target["id"]
                note_ids = [
                    row["note_id"] for row in self.connection.execute(
                        "SELECT note_id FROM note_tags WHERE tag_id = ?", (source["id"],)
                    )
                ]
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO note_tags(note_id, tag_id, source)
                    SELECT note_id, ?, source FROM note_tags WHERE tag_id = ?
                    """,
                    (target_id, source["id"]),
                )
                self.connection.execute("DELETE FROM note_tags WHERE tag_id = ?", (source["id"],))
                self.connection.execute("DELETE FROM tags WHERE id = ?", (source["id"],))
            for note_id in note_ids:
                self._rewrite_markdown(self.get_note(note_id))
            return len(note_ids)

    # ---------- 导入文档与切片 ----------

    def replace_note_chunks(self, note_id: int, chunks: list[dict]) -> list[NoteChunk]:
        clean = [chunk for chunk in chunks if str(chunk.get("text", "")).strip()]
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM note_chunks WHERE note_id = ?", (note_id,))
            for order, chunk in enumerate(clean):
                self.connection.execute(
                    """
                    INSERT INTO note_chunks(note_id, title, text, location, kind, chunk_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note_id,
                        str(chunk.get("title") or "未命名笔记")[:180],
                        str(chunk["text"]).strip(),
                        str(chunk.get("location") or "正文")[:240],
                        str(chunk.get("kind") or "content")[:32],
                        order,
                    ),
                )
        return self.list_note_chunks(note_id)

    def list_note_chunks(self, note_id: int) -> list[NoteChunk]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM note_chunks WHERE note_id = ? ORDER BY chunk_order, id", (note_id,)
            ).fetchall()
            return [NoteChunk(**dict(row)) for row in rows]

    def get_note_chunk(self, chunk_id: int) -> NoteChunk:
        with self._lock:
            row = self.connection.execute("SELECT * FROM note_chunks WHERE id = ?", (chunk_id,)).fetchone()
            if row is None:
                raise KeyError(chunk_id)
            return NoteChunk(**dict(row))

    def upsert_note_chunk_embedding(self, chunk_id: int, vector: np.ndarray) -> None:
        vector = np.asarray(vector, dtype=np.float32)
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO note_chunk_embeddings(chunk_id, vector, dimension, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    vector = excluded.vector,
                    dimension = excluded.dimension,
                    updated_at = excluded.updated_at
                """,
                (chunk_id, vector.tobytes(), vector.size, self._now()),
            )

    def list_note_chunk_embeddings(self) -> list[tuple[int, np.ndarray]]:
        with self._lock:
            return [
                (row["chunk_id"], np.frombuffer(row["vector"], dtype=np.float32))
                for row in self.connection.execute("SELECT chunk_id, vector FROM note_chunk_embeddings")
            ]

    def note_chunk_embedding_count(self) -> int:
        with self._lock:
            return self.connection.execute("SELECT COUNT(*) FROM note_chunk_embeddings").fetchone()[0]

    _DOCUMENT_EXTENSIONS = {".txt", ".docx", ".xlsx", ".pdf"}
    MAX_DOCUMENT_BYTES = 10 * 1024 * 1024

    def _decode_document_upload(self, filename: str, data_url: str) -> tuple[str, str, str, bytes]:
        """Validate browser upload data and preserve a safe display path."""
        # Keep a browser-provided folder-relative name for the library UI, while
        # only using its final path component when creating a local file.
        raw_name = str(filename or "").replace("\\", "/").strip()
        parts = [part for part in raw_name.split("/") if part and part not in {".", ".."}]
        name = "/".join(parts) or "未命名文档"
        file_name = parts[-1] if parts else name
        suffix = Path(file_name).suffix.lower()
        if suffix not in self._DOCUMENT_EXTENSIONS:
            raise ValueError("仅支持 TXT、DOCX、XLSX 和 PDF 文件")
        match = re.fullmatch(r"data:[^;,]+;base64,([A-Za-z0-9+/=\r\n]+)", str(data_url or ""))
        if not match:
            raise ValueError("无效的文件数据")
        try:
            payload = base64.b64decode(match.group(1), validate=True)
        except binascii.Error as exc:
            raise ValueError("无效的文件数据") from exc
        if not payload:
            raise ValueError("文件为空")
        if len(payload) > self.MAX_DOCUMENT_BYTES:
            raise ValueError(f"单个导入文件不能超过 {self.MAX_DOCUMENT_BYTES // 1024 // 1024}MB")
        return name, file_name, suffix, payload

    def _document_file_target(self, file_name: str, suffix: str) -> tuple[str, Path, Path]:
        safe_stem = re.sub(r"[\\/:*?\"<>|]+", "_", Path(file_name).stem).strip(" .") or "未命名文档"
        safe_name = f"{self._timestamp()}_{safe_stem[:80]}_{uuid4().hex[:8]}{suffix}"
        relative = Path("attachments") / "documents" / safe_name
        return safe_stem, relative, self.data_dir / relative

    def create_document_from_data_url(self, filename: str, data_url: str, chunk_profile: str = "standard") -> Document:
        """Persist an original office document before background extraction.

        The browser sends a data URL so the local-only HTTP server can retain its
        JSON-only API. The size limit intentionally applies to decoded bytes,
        not the larger base64 representation.
        """
        name, file_name, suffix, payload = self._decode_document_upload(filename, data_url)
        chunk_profile = self._clean_document_chunk_profile(chunk_profile)
        safe_stem, relative, target = self._document_file_target(file_name, suffix)

        target.write_bytes(payload)
        now = self._now()
        title = safe_stem[:120]
        try:
            with self._lock, self.connection:
                cursor = self.connection.execute(
                    """
                    INSERT INTO documents(
                        title, original_name, stored_path, file_type, size_bytes,
                        sha256, chunk_profile, status, error, extracted_chars, chunk_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', 0, 0, ?, ?)
                    """,
                    (title, name, relative.as_posix(), suffix.removeprefix("."), len(payload),
                     hashlib.sha256(payload).hexdigest(), chunk_profile, now, now),
                )
            return self.get_document(cursor.lastrowid)
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def replace_document_from_data_url(self, document_id: int, filename: str, data_url: str) -> Document:
        """Replace an original while retaining its stable document ID and citations."""
        previous = self.get_document(document_id)
        name, file_name, suffix, payload = self._decode_document_upload(filename, data_url)
        safe_stem, relative, target = self._document_file_target(file_name, suffix)
        target.write_bytes(payload)
        now = self._now()
        try:
            with self._lock, self.connection:
                # Derived chunks and vectors are no longer valid for the new original.
                self.connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
                self.connection.execute("DELETE FROM document_chunk_adjustments WHERE document_id = ?", (document_id,))
                self.connection.execute(
                    """
                    UPDATE documents
                    SET title = ?, original_name = ?, stored_path = ?, file_type = ?, size_bytes = ?,
                        sha256 = ?, status = 'pending', error = '', extracted_chars = 0, chunk_count = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        safe_stem[:120], name, relative.as_posix(), suffix.removeprefix("."), len(payload),
                        hashlib.sha256(payload).hexdigest(), now, document_id,
                    ),
                )
            previous_path = self.data_dir / previous.stored_path
            if previous_path != target:
                previous_path.unlink(missing_ok=True)
            return self.get_document(document_id)
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def document_matches_sha256(self, document_id: int, sha256: str) -> bool:
        try:
            return self.get_document(document_id).sha256 == sha256
        except KeyError:
            return False

    def get_document(self, document_id: int) -> Document:
        with self._lock:
            row = self.connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            if row is None:
                raise KeyError(document_id)
            return Document(**dict(row))

    def list_documents(self, limit: int = 200) -> list[Document]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM documents ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [Document(**dict(row)) for row in rows]

    def set_document_status(self, document_id: int, status: str, error: str = "") -> Document:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE documents SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, str(error or "")[:800], self._now(), document_id),
            )
        return self.get_document(document_id)

    @staticmethod
    def _clean_document_chunk_profile(profile: str | None) -> str:
        profile = str(profile or "standard").strip().lower()
        if profile not in {"precise", "standard", "context"}:
            raise ValueError("切片策略仅支持 precise、standard 或 context")
        return profile

    def set_document_chunk_profile(self, document_id: int, profile: str) -> Document:
        """Change the automatic strategy and discard incompatible manual boundaries."""
        profile = self._clean_document_chunk_profile(profile)
        with self._lock, self.connection:
            self.get_document(document_id)
            self.connection.execute("DELETE FROM document_chunk_adjustments WHERE document_id = ?", (document_id,))
            self.connection.execute(
                "UPDATE documents SET chunk_profile = ?, status = 'pending', error = '', updated_at = ? WHERE id = ?",
                (profile, self._now(), document_id),
            )
        return self.get_document(document_id)

    def document_chunk_adjustment_count(self, document_id: int) -> int:
        with self._lock:
            return int(self.connection.execute(
                "SELECT COUNT(*) FROM document_chunk_adjustments WHERE document_id = ?", (document_id,)
            ).fetchone()[0])

    def add_document_chunk_merge(self, document_id: int, start_order: int, end_order: int) -> None:
        """Store a safe merge rule for contiguous chunks from the same source context."""
        chunks = self.list_document_chunks(document_id)
        left = next((chunk for chunk in chunks if chunk.source_start_order == int(start_order)), None)
        right = next((chunk for chunk in chunks if chunk.source_end_order == int(end_order)), None)
        if left is None or right is None or left.source_end_order + 1 != right.source_start_order:
            raise ValueError("只能合并当前相邻的切片")
        if (left.page, left.section, left.sheet) != (right.page, right.section, right.sheet):
            raise ValueError("不能跨 PDF 页、Word 标题或 Excel 工作表合并")
        document = self.get_document(document_id)
        merge_limits = {"precise": 850, "standard": 1100, "context": 1200}
        if len(left.text) + len(right.text) + 2 > merge_limits[document.chunk_profile]:
            raise ValueError("合并后内容过长；请保持两个切片分别检索")
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO document_chunk_adjustments(
                    document_id, document_sha256, profile, start_order, end_order, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (document_id, document.sha256, document.chunk_profile, left.source_start_order, right.source_end_order, self._now()),
            )

    def clear_document_chunk_adjustments(self, document_id: int) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM document_chunk_adjustments WHERE document_id = ?", (document_id,))

    def apply_document_chunk_adjustments(self, document: Document, chunks: list[dict]) -> list[dict]:
        """Apply only persisted, source-contiguous merge rules to freshly extracted text."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT start_order, end_order FROM document_chunk_adjustments
                WHERE document_id = ? AND document_sha256 = ? AND profile = ?
                ORDER BY start_order, end_order
                """,
                (document.id, document.sha256, document.chunk_profile),
            ).fetchall()
        if not rows:
            return chunks
        ranges: list[list[int]] = []
        for row in rows:
            start, end = int(row["start_order"]), int(row["end_order"])
            if start < 0 or end >= len(chunks) or start >= end:
                continue
            if ranges and start <= ranges[-1][1] + 1:
                ranges[-1][1] = max(ranges[-1][1], end)
            else:
                ranges.append([start, end])
        result: list[dict] = []
        index = 0
        while index < len(chunks):
            merge_range = next((item for item in ranges if item[0] == index), None)
            if merge_range is None:
                item = dict(chunks[index])
                item["source_start_order"] = index
                item["source_end_order"] = index
                result.append(item)
                index += 1
                continue
            start, end = merge_range
            members = chunks[start:end + 1]
            first = dict(members[0])
            if any((item.get("page"), item.get("section", ""), item.get("sheet", "")) !=
                   (first.get("page"), first.get("section", ""), first.get("sheet", "")) for item in members[1:]):
                for offset, item in enumerate(members, start):
                    clean = dict(item)
                    clean["source_start_order"] = offset
                    clean["source_end_order"] = offset
                    result.append(clean)
            else:
                first["text"] = "\n\n".join(str(item.get("text", "")).strip() for item in members).strip()
                first["location"] = first.get("location") if first.get("location") == members[-1].get("location") else f"{first.get('location', '正文')} 至 {members[-1].get('location', '正文')}"
                first["source_start_order"] = start
                first["source_end_order"] = end
                result.append(first)
            index = end + 1
        return result

    def replace_document_chunks(
        self, document_id: int, chunks: list[dict], expected_sha256: str | None = None
    ) -> list[DocumentChunk] | None:
        """Atomically replace derived text while retaining the original file."""
        clean = [chunk for chunk in chunks if str(chunk.get("text", "")).strip()]
        with self._lock, self.connection:
            document = self.get_document(document_id)
            if expected_sha256 and document.sha256 != expected_sha256:
                return None
            self.connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
            for order, chunk in enumerate(clean):
                self.connection.execute(
                    """
                    INSERT INTO document_chunks(
                        document_id, title, text, location, page, section, sheet, cell_range, chunk_order,
                        source_start_order, source_end_order
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        str(chunk.get("title") or document.title)[:180],
                        str(chunk["text"]).strip(),
                        str(chunk.get("location") or "正文")[:240],
                        chunk.get("page"),
                        str(chunk.get("section") or "")[:240],
                        str(chunk.get("sheet") or "")[:120],
                        str(chunk.get("cell_range") or "")[:120],
                        order,
                        int(chunk.get("source_start_order", order)),
                        int(chunk.get("source_end_order", order)),
                    ),
                )
            self.connection.execute(
                """
                UPDATE documents
                SET status = 'indexing', error = '', extracted_chars = ?, chunk_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (sum(len(str(chunk["text"])) for chunk in clean), len(clean), self._now(), document_id),
            )
        return self.list_document_chunks(document_id)

    def list_document_chunks(self, document_id: int) -> list[DocumentChunk]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM document_chunks WHERE document_id = ? ORDER BY chunk_order, id", (document_id,)
            ).fetchall()
            return [DocumentChunk(**dict(row)) for row in rows]

    def get_document_chunk(self, chunk_id: int) -> DocumentChunk:
        with self._lock:
            row = self.connection.execute("SELECT * FROM document_chunks WHERE id = ?", (chunk_id,)).fetchone()
            if row is None:
                raise KeyError(chunk_id)
            return DocumentChunk(**dict(row))

    def search_document_chunks(self, query: str, limit: int = 50) -> list[DocumentChunk]:
        query = str(query or "").strip()
        if not query:
            return []
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM document_chunks
                WHERE instr(title, ?) > 0 OR instr(text, ?) > 0
                ORDER BY id DESC LIMIT ?
                """,
                (query, query, limit),
            ).fetchall()
            chunks = [DocumentChunk(**dict(row)) for row in rows]
            if len(query) < 3 or len(chunks) >= limit:
                return chunks
            fts_rows = self.connection.execute(
                """
                SELECT document_chunks.* FROM document_chunks_fts
                JOIN document_chunks ON document_chunks.id = document_chunks_fts.rowid
                WHERE document_chunks_fts MATCH ?
                ORDER BY bm25(document_chunks_fts) LIMIT ?
                """,
                (self._fts_query(query), limit),
            ).fetchall()
            seen = {chunk.id for chunk in chunks}
            chunks.extend(DocumentChunk(**dict(row)) for row in fts_rows if row["id"] not in seen)
            return chunks[:limit]

    def upsert_document_chunk_embedding(self, chunk_id: int, vector: np.ndarray) -> None:
        vector = np.asarray(vector, dtype=np.float32)
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO document_chunk_embeddings(chunk_id, vector, dimension, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    vector = excluded.vector,
                    dimension = excluded.dimension,
                    updated_at = excluded.updated_at
                """,
                (chunk_id, vector.tobytes(), vector.size, self._now()),
            )

    def list_document_chunk_embeddings(self) -> list[tuple[int, np.ndarray]]:
        with self._lock:
            return [
                (row["chunk_id"], np.frombuffer(row["vector"], dtype=np.float32))
                for row in self.connection.execute("SELECT chunk_id, vector FROM document_chunk_embeddings")
            ]

    def delete_document(self, document_id: int) -> None:
        with self._lock:
            document = self.get_document(document_id)
            with self.connection:
                self.connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            (self.data_dir / document.stored_path).unlink(missing_ok=True)

    def create_wiki_card(self, title: str, template: str, sources: list[dict], items: list[dict]) -> int:
        now = self._now()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO wiki_cards(title, template, status, created_at, updated_at) VALUES (?, ?, 'draft', ?, ?)",
                (title, template, now, now),
            )
            card_id = int(cursor.lastrowid)
            for source in sources:
                self.connection.execute(
                    """
                    INSERT INTO wiki_card_sources(card_id, source_kind, source_id, source_label, source_updated_at, source_sha256)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (card_id, source["kind"], int(source["id"]), source["label"], source["updated_at"], source.get("sha256", "")),
                )
            for order, item in enumerate(items):
                self.connection.execute(
                    """
                    INSERT INTO wiki_card_items(card_id, item_type, field, claim, evidence_json, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (card_id, item["item_type"], item["field"], item["claim"], json.dumps(item.get("evidence", []), ensure_ascii=False), order),
                )
        return card_id

    def _wiki_card_sources(self, card_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT source_kind, source_id, source_label, source_updated_at, source_sha256 FROM wiki_card_sources WHERE card_id = ? ORDER BY source_kind, source_id",
            (card_id,),
        ).fetchall()
        return [
            {"kind": row["source_kind"], "id": row["source_id"], "label": row["source_label"], "updated_at": row["source_updated_at"], "sha256": row["source_sha256"]}
            for row in rows
        ]

    def _wiki_card_is_stale(self, card_id: int, sources: list[dict]) -> bool:
        for source in sources:
            try:
                if source["kind"] == "note":
                    current = self.get_note(int(source["id"]))
                    current_fingerprint = hashlib.sha256(
                        "\0".join((str(current.title or ""), str(current.content or ""), str(current.ocr_text or ""))).encode("utf-8")
                    ).hexdigest()
                    if current.updated_at != source["updated_at"] or current_fingerprint != source.get("sha256", ""):
                        return True
                elif source["kind"] == "document":
                    current = self.get_document(int(source["id"]))
                    if current.updated_at != source["updated_at"] or current.sha256 != source.get("sha256", ""):
                        return True
                else:
                    return True
            except KeyError:
                return True
        return False

    def get_wiki_card(self, card_id: int) -> dict:
        with self._lock, self.connection:
            row = self.connection.execute("SELECT * FROM wiki_cards WHERE id = ?", (card_id,)).fetchone()
            if row is None:
                raise KeyError(card_id)
            sources = self._wiki_card_sources(card_id)
            stale = self._wiki_card_is_stale(card_id, sources)
            if stale and row["status"] != "stale":
                now = self._now()
                self.connection.execute("UPDATE wiki_cards SET status = 'stale', updated_at = ? WHERE id = ?", (now, card_id))
                row = self.connection.execute("SELECT * FROM wiki_cards WHERE id = ?", (card_id,)).fetchone()
            item_rows = self.connection.execute(
                "SELECT item_type, field, claim, evidence_json FROM wiki_card_items WHERE card_id = ? ORDER BY sort_order, id", (card_id,)
            ).fetchall()
            items = []
            for item in item_rows:
                payload = dict(item)
                try:
                    payload["evidence"] = json.loads(payload.pop("evidence_json") or "[]")
                except json.JSONDecodeError:
                    payload["evidence"] = []
                    payload.pop("evidence_json", None)
                items.append(payload)
            card = dict(row)
            return {"id": card["id"], "title": card["title"], "template": card["template"], "status": card["status"], "created_at": card["created_at"], "updated_at": card["updated_at"], "confirmed_at": card["confirmed_at"], "source_refs": sources, "items": items}

    def list_wiki_cards(self) -> list[dict]:
        with self._lock:
            ids = [row["id"] for row in self.connection.execute("SELECT id FROM wiki_cards ORDER BY updated_at DESC, id DESC").fetchall()]
        return [self.get_wiki_card(card_id) for card_id in ids]

    def confirm_wiki_card(self, card_id: int) -> dict:
        with self._lock, self.connection:
            card = self.get_wiki_card(card_id)
            if card["status"] == "stale":
                raise ValueError("原始资料已更新，请重新生成专题卡片后再确认")
            now = self._now()
            self.connection.execute("UPDATE wiki_cards SET status = 'confirmed', confirmed_at = ?, updated_at = ? WHERE id = ?", (now, now, card_id))
        return self.get_wiki_card(card_id)

    def delete_wiki_card(self, card_id: int) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute("DELETE FROM wiki_cards WHERE id = ?", (card_id,))
            if cursor.rowcount == 0:
                raise KeyError(card_id)

    def document_count(self) -> int:
        with self._lock:
            return self.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def document_chunk_embedding_count(self) -> int:
        with self._lock:
            return self.connection.execute("SELECT COUNT(*) FROM document_chunk_embeddings").fetchone()[0]

    def note_count(self) -> int:
        with self._lock:
            return self.connection.execute(
                "SELECT COUNT(*) FROM notes WHERE source_type != 'welcome'"
            ).fetchone()[0]

    def embedding_count(self) -> int:
        with self._lock:
            return self.connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def delete_orphan_embeddings(self) -> int:
        """删除 embeddings 表中 note_id 已不存在于 notes 表的孤儿记录，返回删除条数。"""
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM embeddings WHERE note_id NOT IN (SELECT id FROM notes)"
            )
            return cursor.rowcount

    def upsert_embedding(self, note_id: int, vector: np.ndarray) -> None:
        vector = np.asarray(vector, dtype=np.float32)
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO embeddings(note_id, vector, dimension, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    vector = excluded.vector,
                    dimension = excluded.dimension,
                    updated_at = excluded.updated_at
                """,
                (note_id, vector.tobytes(), vector.size, self._now()),
            )

    def list_embeddings(self) -> list[tuple[int, np.ndarray]]:
        with self._lock:
            return [
                (row["note_id"], np.frombuffer(row["vector"], dtype=np.float32))
                for row in self.connection.execute("SELECT note_id, vector FROM embeddings")
            ]

    def update_ocr(self, note_id: int, status: str, text: str, error: str) -> Note:
        with self._lock:
            existing = self.get_note(note_id)
            now = self._now()
            # OCR 文本有实质变化时保存历史版本
            if existing.ocr_text != text and (existing.ocr_text or text):
                self._save_version(existing, ",".join(existing.tags or []))
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE notes
                    SET ocr_status = ?, ocr_text = ?, ocr_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, text, error, now, note_id),
                )
            updated = self.get_note(note_id)
            markdown = self._render_markdown(
                updated.title,
                updated.content,
                updated.created_at,
                updated.source_type,
                updated.attachment_path,
                updated.category,
                updated.subcategory,
                updated.tags or [],
                updated.ocr_status,
                updated.ocr_text,
                updated.ai_status,
                updated_at=updated.updated_at,
            )
            (self.data_dir / existing.markdown_path).write_text(markdown, encoding="utf-8")
            return updated

    def update_ai_status(self, note_id: int, status: str, error: str = "") -> Note:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE notes SET ai_status = ?, ai_error = ? WHERE id = ?",
                (status, error, note_id),
            )
        return self.get_note(note_id)

    # ---------- 历史版本 ----------
    def _save_version(self, note: Note, tags_str: str) -> None:
        """将当前笔记状态保存为历史版本快照。保留最近 50 个版本，超出自动清理。"""
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO note_versions(note_id, title, content, category, subcategory, tags, ocr_text, tab_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.id, note.title, note.content, note.category, note.subcategory,
                    tags_str, note.ocr_text, note.tab_name, note.updated_at,
                ),
            )
            # 清理超出上限的旧版本
            self.connection.execute(
                """
                DELETE FROM note_versions
                WHERE note_id = ? AND id NOT IN (
                    SELECT id FROM note_versions WHERE note_id = ? ORDER BY id DESC LIMIT 50
                )
                """,
                (note.id, note.id),
            )

    def list_versions(self, note_id: int) -> list[dict]:
        """返回某笔记的历史版本列表（最新在前）。包含页签：note_id 为主笔记时聚合所有子页签的版本。"""
        with self._lock:
            # 收集主笔记及其所有子页签的 id
            note_ids = [note_id]
            children = self.connection.execute(
                "SELECT id FROM notes WHERE parent_note_id = ?", (note_id,)
            ).fetchall()
            note_ids.extend(row["id"] for row in children)
            placeholders = ",".join("?" * len(note_ids))
            rows = self.connection.execute(
                f"""
                SELECT v.id, v.note_id, v.title, v.content, v.category, v.subcategory,
                       v.tags, v.ocr_text, v.tab_name, v.created_at,
                       n.tab_name AS current_tab_name, n.parent_note_id
                FROM note_versions v
                LEFT JOIN notes n ON n.id = v.note_id
                WHERE v.note_id IN ({placeholders})
                ORDER BY v.id DESC
                """,
                note_ids,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_version(self, version_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT v.id, v.note_id, v.title, v.content, v.category, v.subcategory,
                       v.tags, v.ocr_text, v.tab_name, v.created_at
                FROM note_versions v WHERE v.id = ?
                """,
                (version_id,),
            ).fetchone()
            return dict(row) if row else None

    def restore_version(self, version_id: int) -> Note:
        """恢复到指定历史版本：用版本快照覆盖当前笔记内容。"""
        with self._lock:
            version = self.get_version(version_id)
            if not version:
                raise KeyError(f"版本 {version_id} 不存在")
            # 恢复前先保存当前状态为新版本，确保可撤销
            current = self.get_note(version["note_id"])
            self._save_version(current, ",".join(current.tags or []))
            tags = [t for t in version["tags"].split(",") if t] if version["tags"] else []
            return self.update_note(
                version["note_id"],
                title=version["title"],
                content=version["content"],
                category=version["category"],
                subcategory=version["subcategory"],
                tags=tags,
                tab_name=version["tab_name"] or None,
            )

    def apply_ai_suggestions(self, note_id: int, category: str, subcategory: str, tags: list[str], reason: str = "") -> Note:
        """Apply one organisation result to the main note and all its tabs.

        Automatic values refresh on every run. Values explicitly set by a user
        remain protected, and removed tags are excluded until a user adds them back.
        """
        with self._lock:
            requested = self.get_note(note_id)
            group_id = requested.parent_note_id or requested.id
            members = self.get_note_group(group_id)
            group_note = next(note for note in members if note.id == group_id)
            next_category = group_note.category if group_note.category_source == "manual" else self._clean_category(category)
            next_subcategory = (
                group_note.subcategory
                if group_note.subcategory_source == "manual"
                else self._clean_category(subcategory, allow_empty=True)
            )
            category_source = "manual" if group_note.category_source == "manual" else "auto"
            subcategory_source = "manual" if group_note.subcategory_source == "manual" else "auto"
            member_ids = [note.id for note in members]
            placeholders = ",".join("?" * len(member_ids))
            manual_tags = [
                row["name"]
                for row in self.connection.execute(
                    f"""
                    SELECT DISTINCT tags.name FROM tags JOIN note_tags ON note_tags.tag_id = tags.id
                    WHERE note_tags.note_id IN ({placeholders}) AND note_tags.source = 'manual'
                    ORDER BY tags.name
                    """,
                    member_ids,
                )
            ]
            excluded_tags = {
                row["tag_name"].casefold()
                for row in self.connection.execute(
                    "SELECT tag_name FROM note_tag_exclusions WHERE group_note_id = ?", (group_id,)
                )
            }
            automatic_tags = [
                tag for tag in self._clean_tags(tags, limit=None)
                if (
                    tag.casefold() not in excluded_tags
                    and tag not in manual_tags
                    and self._is_auto_tag_allowed(tag)
                )
            ]
            automatic_tags = automatic_tags[:max(0, MAX_TAGS_PER_NOTE - len(manual_tags))]
            now = self._now()
            with self.connection:
                # A folder chosen manually always wins. For an unassigned new
                # note, make the AI category visible in navigation at once.
                folder_id = group_note.folder_id
                if folder_id is None:
                    folder_id = self._ensure_category_folder(next_category, next_subcategory)
                for member in members:
                    self.connection.execute(
                        """
                        UPDATE notes
                        SET category = ?, subcategory = ?, category_source = ?, subcategory_source = ?,
                            folder_id = COALESCE(folder_id, ?), ai_status = 'done', ai_error = '', ai_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (next_category, next_subcategory, category_source, subcategory_source, folder_id, reason[:200], now, member.id),
                    )
                    self.connection.execute("DELETE FROM note_tags WHERE note_id = ? AND source = 'auto'", (member.id,))
                    self._add_tags(member.id, manual_tags, "manual")
                    self._add_tags(member.id, automatic_tags, "auto")
            for member_id in member_ids:
                self._rewrite_markdown(self.get_note(member_id))
            return self.get_note(note_id)

    def _purge_blocklisted_auto_tags(self) -> None:
        """Remove legacy generic AI tags without touching user-owned tags.

        Earlier builds could promote broad terms such as ``证据链`` into the
        reusable vocabulary.  The cleanup is deliberately source-aware: a
        manually added tag is always retained, while only automatic entries
        blocked by the current governance policy are removed.
        """
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT note_tags.note_id, tags.name
                FROM note_tags
                JOIN tags ON tags.id = note_tags.tag_id
                WHERE note_tags.source = 'auto'
                """
            ).fetchall()
            affected_note_ids = {
                row["note_id"]
                for row in rows
                if not self._is_auto_tag_allowed(row["name"])
            }
            if not affected_note_ids:
                return
            with self.connection:
                for row in rows:
                    if not self._is_auto_tag_allowed(row["name"]):
                        self.connection.execute(
                            """
                            DELETE FROM note_tags
                            WHERE note_id = ?
                              AND source = 'auto'
                              AND tag_id IN (SELECT id FROM tags WHERE name = ?)
                            """,
                            (row["note_id"], row["name"]),
                        )
            for note_id in affected_note_ids:
                self._rewrite_markdown(self.get_note(note_id))

    def excluded_group_tags(self, note_id: int) -> list[str]:
        with self._lock:
            note = self.get_note(note_id)
            group_id = note.parent_note_id or note.id
            return [
                row["tag_name"]
                for row in self.connection.execute(
                    "SELECT tag_name FROM note_tag_exclusions WHERE group_note_id = ? ORDER BY tag_name", (group_id,)
                )
            ]

    def pending_ai_note_ids(self) -> list[int]:
        with self._lock:
            return [
                row["id"]
                for row in self.connection.execute(
                    "SELECT id FROM notes WHERE ai_status IN ('pending', 'processing') ORDER BY id"
                )
            ]

    def migrate_data_dir(self, new_data_dir: Path) -> Path:
        new_data_dir = new_data_dir.resolve()
        with self._lock:
            if new_data_dir == self.data_dir:
                return new_data_dir
            if new_data_dir.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(new_data_dir):
                raise ValueError("新旧数据目录不能互相包含")
            if any(new_data_dir.iterdir()) if new_data_dir.exists() else False:
                raise ValueError("目标目录必须为空")
            self.close()
            new_data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                self.data_dir,
                new_data_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".pocket-memory.lock", "logs"),
            )
            self._connect(new_data_dir)
            return new_data_dir

    def switch_data_dir(self, new_data_dir: Path) -> Path:
        """Switch to an empty or existing Pocket Memory data directory.

        Unlike :meth:`migrate_data_dir`, this operation never copies the
        current database, notes, or attachments.  A non-empty target must
        already contain Pocket Memory's database so choosing a random folder
        cannot silently add application files beside unrelated user content.
        """
        new_data_dir = new_data_dir.resolve()
        with self._lock:
            if new_data_dir == self.data_dir:
                return new_data_dir
            if new_data_dir.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(new_data_dir):
                raise ValueError("新旧数据目录不能互相包含")
            if new_data_dir.exists() and any(new_data_dir.iterdir()):
                if not (new_data_dir / "pocket_memory.db").is_file():
                    raise ValueError("目标目录不是 Pocket Memory 数据目录，请选择空目录或已有数据目录")
            self.close()
            self._connect(new_data_dir)
            return new_data_dir

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _save_image(self, data_url: str, image_name: str | None) -> str:
        match = re.fullmatch(r"data:image/([a-zA-Z0-9.+-]+);base64,(.+)", data_url, re.DOTALL)
        if not match:
            raise ValueError("无效的图片数据")
        extension = {"jpeg": "jpg", "svg+xml": "svg"}.get(match.group(1).lower(), match.group(1).lower())
        if extension not in {"png", "jpg", "webp", "gif", "bmp"}:
            raise ValueError("暂不支持这种图片格式")
        stem = self._slug(Path(image_name or "clipboard").stem)
        relative_path = Path("attachments") / "images" / f"{self._timestamp()}_{stem}_{uuid4().hex[:8]}.{extension}"
        try:
            image_bytes = base64.b64decode(match.group(2), validate=True)
        except binascii.Error as exc:
            raise ValueError("无效的图片数据") from exc
        (self.data_dir / relative_path).write_bytes(image_bytes)
        return relative_path.as_posix()

    def _markdown_path(self, now: str, title: str) -> str:
        month = now[:7]
        filename = f"{self._timestamp()}_{self._slug(title)}_{uuid4().hex[:8]}.md"
        return (Path("notes") / month / filename).as_posix()

    @staticmethod
    def _render_markdown(
        title: str,
        content: str,
        created_at: str,
        source_type: str,
        attachment_path: str | None,
        category: str,
        subcategory: str,
        tags: list[str],
        ocr_status: str = "not_applicable",
        ocr_text: str = "",
        ai_status: str = "pending",
        updated_at: str | None = None,
    ) -> str:
        lines = [
            "---",
            f'title: "{title.replace(chr(34), chr(39))}"',
            f"source_type: {source_type}",
            f'category: "{category}"',
            f'subcategory: "{subcategory}"',
            f"tags: [{', '.join(tags)}]",
            f"ocr_status: {ocr_status}",
            f"ai_status: {ai_status}",
            f"created_at: {created_at}",
            f"updated_at: {updated_at or created_at}",
            "---",
            "",
            f"# {title}",
            "",
        ]
        if attachment_path:
            lines.extend([f"![图片](../../{attachment_path})", ""])
        lines.extend([content.strip(), ""])
        if ocr_text:
            lines.extend(["## OCR 识别文本", "", ocr_text.strip(), ""])
        return "\n".join(lines)

    def _replace_tags(self, note_id: int, tags: list[str], source: str) -> None:
        self.connection.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        self._add_tags(note_id, tags, source)

    def _add_tags(self, note_id: int, tags: list[str], source: str) -> None:
        for tag in tags:
            self.connection.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
            self.connection.execute(
                """
                INSERT OR IGNORE INTO note_tags(note_id, tag_id, source)
                SELECT ?, id, ? FROM tags WHERE name = ?
                """,
                (note_id, source, tag),
            )

    def _rewrite_markdown(self, note: Note) -> None:
        markdown = self._render_markdown(
            note.title,
            note.content,
            note.created_at,
            note.source_type,
            note.attachment_path,
            note.category,
            note.subcategory,
            note.tags or [],
            note.ocr_status,
            note.ocr_text,
            ai_status=note.ai_status,
            updated_at=note.updated_at,
        )
        (self.data_dir / note.markdown_path).write_text(markdown, encoding="utf-8")

    def _get_tags(self, note_id: int) -> list[str]:
        return [
            row["name"]
            for row in self.connection.execute(
                """
                SELECT tags.name
                FROM tags JOIN note_tags ON note_tags.tag_id = tags.id
                WHERE note_tags.note_id = ?
                ORDER BY tags.name
                """,
                (note_id,),
            )
        ]

    # 注：_get_tags 是内部方法，调用方（get_note 等）已持有 self._lock，此处不再重复加锁，避免死锁。

    @staticmethod
    def _filter_clauses(
        category: str | None,
        subcategory: str | None,
        tag: str | None,
        since: str | None = None,
        folder_ids: list[int] | None = None,
        favorite: bool | None = None,
    ) -> tuple[list[str], list[object]]:
        clauses, parameters = [], []
        if category:
            clauses.append("notes.category = ?")
            parameters.append(category)
        if subcategory:
            clauses.append("notes.subcategory = ?")
            parameters.append(subcategory)
        if tag:
            clauses.append("tags.name = ?")
            parameters.append(tag)
        if since:
            clauses.append("notes.created_at >= ?")
            parameters.append(since)
        if folder_ids:
            placeholders = ",".join("?" * len(folder_ids))
            clauses.append(f"notes.folder_id IN ({placeholders})")
            parameters.extend(folder_ids)
        if favorite:
            clauses.append("notes.favorite = 1")
        return clauses, parameters

    @staticmethod
    def _clean_category(value: str | None, allow_empty: bool = False) -> str:
        value = (value or "").strip()[:40]
        return value if value or allow_empty else "未分类"

    @staticmethod
    def _clean_tags(tags: list[str] | None, limit: int | None = MAX_TAGS_PER_NOTE) -> list[str]:
        unique = []
        for tag in tags or []:
            cleaned = re.sub(r"\s+", " ", str(tag).strip().lstrip("#"))[:30]
            if cleaned and cleaned not in unique:
                unique.append(cleaned)
        return unique if limit is None else unique[:limit]

    @staticmethod
    def _tag_key(tag: str) -> str:
        return re.sub(r"[\s_\-]+", "", tag).casefold()

    @classmethod
    def _is_auto_tag_allowed(cls, tag: str) -> bool:
        blocked = {cls._tag_key(item) for item in AUTO_TAG_BLOCKLIST}
        return cls._tag_key(tag) not in blocked

    @staticmethod
    def _clean_title(title: str, content: str) -> str:
        title = title.strip()
        if not title:
            title = next((line.strip() for line in content.splitlines() if line.strip()), "未命名笔记")
        return title[:80]

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
        return (slug[:40] or "note").rstrip(". ")

    @staticmethod
    def _fts_query(query: str) -> str:
        # 支持多词搜索：把查询拆分成多个词，每个词用引号包裹，用空格连接（FTS5 默认 AND）
        terms = NoteStore._split_query(query)
        if not terms:
            return '"' + query.replace('"', '""') + '"'
        return " ".join('"' + term.replace('"', '""') + '"' for term in terms)

    @staticmethod
    def _split_query(query: str) -> list[str]:
        """把查询按空格和中文连接词拆分成多个搜索词，用于多词 AND 匹配。"""
        # 按空格、和、与、及、,、，、+、& 拆分
        parts = re.split(r'[\s+,，&]+|和|与|及', query)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")
