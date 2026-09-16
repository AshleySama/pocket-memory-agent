from __future__ import annotations

import base64
import binascii
import inspect
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import urlopen
from urllib.error import URLError

from pocket_memory.version import APP_VERSION, UPDATE_CHECK_URL

from pocket_memory.config import AppConfig
from pocket_memory.documents import DocumentImportService
from pocket_memory.embedding import EmbeddingService
from pocket_memory.intelligence import IntelligenceService, resolve_model_path, writable_models_path
from pocket_memory.model_download import ModelDownloadService
from pocket_memory.ocr import OcrService
from pocket_memory.rag import IntentRouter, RagPipeline
from pocket_memory.reporting import ReportService
from pocket_memory.storage import NoteStore
from pocket_memory.tools import ToolRegistry

logger = logging.getLogger(__name__)
MAX_JSON_BODY = 16 * 1024 * 1024  # 16MB 上限，防止异常大 body 吃满内存


def is_loopback_host(host: str) -> bool:
    """Only loopback addresses are safe for a personal, unauthenticated note server."""
    return host.strip().lower() in {"127.0.0.1", "::1", "localhost"}


class _SilentAbort(Exception):
    """_read_json 已自行响应，调用方应静默中止。"""


class PocketMemoryServer(ThreadingHTTPServer):
    # A stalled browser request must never prevent the local daemon from exiting.
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        server_address: tuple[str, int],
        store: NoteStore,
        config: AppConfig,
        choose_directory: Callable[[Path | None], Path | None],
        ocr_service: OcrService | None = None,
        embedding_service: EmbeddingService | None = None,
        intelligence_service: IntelligenceService | None = None,
        model_download_service: ModelDownloadService | None = None,
    ):
        if not is_loopback_host(server_address[0]):
            raise ValueError("Pocket Memory 仅允许监听本机回环地址")
        self.store = store
        self.config = config
        self.choose_directory = choose_directory
        self.embedding_service = embedding_service or EmbeddingService(store)
        if intelligence_service is None:
            from pocket_memory.intelligence import LlamaCppTextEngine
            engine = LlamaCppTextEngine(model_path=resolve_model_path(config.llm_model_path))
            intelligence_service = IntelligenceService(
                store, engine=engine, on_complete=self._after_ai, auto_apply=config.ai_auto_apply
            )
        self.intelligence_service = intelligence_service
        self.ocr_service = ocr_service or OcrService(store, on_complete=self._after_ocr)
        self.model_download_service = model_download_service or ModelDownloadService(
            writable_models_path(),
            bundled_model_path=resolve_model_path(config.llm_model_path),
        )
        self.document_service = DocumentImportService(store, self.embedding_service, self.ocr_service.engine)
        self.rag_pipeline = RagPipeline(store, self.embedding_service)
        self.report_service = ReportService(store, self.intelligence_service.engine)
        self.tool_registry = ToolRegistry(store.data_dir)
        self._shutdown_requested = False  # 标记是否收到 shutdown 请求
        super().__init__(server_address, PocketMemoryHandler)
        # 写入 lock 文件，供守护进程探测
        from pocket_memory.daemon import write_lock
        write_lock(store.data_dir, os.getpid(), server_address[0], self.server_port)
        logger.info("server 已启动，lock 写入 %s", store.data_dir / ".pocket-memory.lock")
        self.embedding_service.rebuild()
        self.intelligence_service.recover()

    def _after_ocr(self, note_id: int) -> None:
        self.embedding_service.enqueue(note_id)
        if self.config.ai_auto_apply:
            self.intelligence_service.enqueue(note_id)

    def _after_ai(self, note_id: int) -> None:
        # AI 整理改变了 category/tags，向量文本包含这些字段，需要重新计算 embedding
        self.embedding_service.enqueue(note_id)

    def migrate_data_dir(self, new_data_dir: Path) -> Path:
        """Move all server-owned state to a new data directory as one operation."""
        old_data_dir = self.store.data_dir
        migrated = self.store.migrate_data_dir(new_data_dir)
        if migrated == old_data_dir:
            return migrated
        self.tool_registry.close()
        self.tool_registry = ToolRegistry(migrated)
        from pocket_memory.daemon import clear_lock, write_lock

        clear_lock(old_data_dir)
        write_lock(migrated, os.getpid(), self.server_address[0], self.server_port)
        logger.info("数据目录已迁移: %s -> %s", old_data_dir, migrated)
        return migrated

    def switch_data_dir(self, new_data_dir: Path) -> Path:
        """Switch active storage without copying the current user's data."""
        old_data_dir = self.store.data_dir
        selected = self.store.switch_data_dir(new_data_dir)
        if selected == old_data_dir:
            return selected
        self.tool_registry.close()
        self.tool_registry = ToolRegistry(selected)
        welcome_image = Path(__file__).resolve().parent.parent / "frontend" / "PocketMemory_welcome.jpg"
        if welcome_image.is_file():
            self.store.ensure_welcome_note(welcome_image)
        from pocket_memory.daemon import clear_lock, write_lock

        clear_lock(old_data_dir)
        write_lock(selected, os.getpid(), self.server_address[0], self.server_port)
        logger.info("数据目录已切换: %s -> %s", old_data_dir, selected)
        return selected

    def server_close(self) -> None:
        self.ocr_service.shutdown()
        self.document_service.shutdown()
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.tool_registry.close()
        # 清理 lock 文件
        from pocket_memory.daemon import clear_lock
        clear_lock(self.store.data_dir)
        logger.info("server 已关闭，lock 已清理")
        super().server_close()


class PocketMemoryHandler(BaseHTTPRequestHandler):
    server: PocketMemoryServer
    static_dir = Path(__file__).resolve().parent.parent / "frontend"

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except _SilentAbort:
            pass
        except KeyError:
            self._safe_error(HTTPStatus.NOT_FOUND, "资源不存在")
        except ValueError as exc:
            self._safe_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            logger.exception("Unhandled error in GET %s", self.path)
            self._safe_error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except _SilentAbort:
            pass
        except KeyError:
            self._safe_error(HTTPStatus.NOT_FOUND, "资源不存在")
        except ValueError as exc:
            self._safe_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            logger.exception("Unhandled error in POST %s", self.path)
            self._safe_error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def do_PUT(self) -> None:
        try:
            self._handle_put()
        except _SilentAbort:
            pass
        except KeyError:
            self._safe_error(HTTPStatus.NOT_FOUND, "资源不存在")
        except ValueError as exc:
            self._safe_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            logger.exception("Unhandled error in PUT %s", self.path)
            self._safe_error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def do_DELETE(self) -> None:
        try:
            self._handle_delete()
        except _SilentAbort:
            pass
        except KeyError:
            self._safe_error(HTTPStatus.NOT_FOUND, "资源不存在")
        except ValueError as exc:
            self._safe_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            logger.exception("Unhandled error in DELETE %s", self.path)
            self._safe_error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            # 健康检查端点：守护进程探测用，返回最小化状态
            from pocket_memory.version import APP_VERSION

            self._json({"ok": True, "pid": os.getpid(), "app_version": APP_VERSION})
        elif parsed.path == "/api/notes":
            filters = self._filters(parsed.query)
            notes = self.server.store.list_notes(
                **filters, include_welcome=not filters
            )
            self._json([self._note_with_tabs(note) for note in notes])
        elif parsed.path == "/api/documents":
            self._json([document.as_dict() for document in self.server.store.list_documents()])
        elif parsed.path == "/api/reports/templates":
            self._json(self.server.report_service.templates())
        elif parsed.path == "/api/documents/status":
            self._json(self.server.document_service.status())
        elif parsed.path.startswith("/api/documents/") and parsed.path.endswith("/chunks"):
            document_id = int(parsed.path[len("/api/documents/"):-len("/chunks")])
            document = self.server.store.get_document(document_id)
            self._json({
                "document": document.as_dict(),
                "chunks": [chunk.as_dict() for chunk in self.server.store.list_document_chunks(document_id)],
                "adjustment_count": self.server.store.document_chunk_adjustment_count(document_id),
            })
        elif parsed.path.startswith("/api/documents/") and parsed.path.endswith("/file"):
            document_id = int(parsed.path[len("/api/documents/"):-len("/file")])
            document = self.server.store.get_document(document_id)
            path = self.server.store.data_dir / document.stored_path
            if parse_qs(parsed.query).get("download", [""])[0] == "1":
                self._send_download_file(path, Path(document.original_name).name, "application/octet-stream")
            else:
                self._file(path, root=self.server.store.data_dir)
        elif parsed.path == "/api/search":
            parameters = parse_qs(parsed.query)
            query = parameters.get("q", [""])[0]
            mode = parameters.get("mode", ["hybrid"])[0]
            filters = self._filters(parsed.query)
            if mode in {"semantic", "hybrid"}:
                # 语义检索直接用 _matches_filters 过滤，需在此展开 folder_id 为子孙集合，
                # 与 list_notes/hybrid_search 行为一致（点击大分类覆盖所有小分类笔记）。
                if filters.get("folder_id") is not None and "folder_ids" not in filters:
                    filters["folder_ids"] = set(self.server.store.get_descendant_folder_ids(filters.pop("folder_id")))
                search_topics = RagPipeline.search_anchor_terms(query)
                results = [
                    chunk
                    for chunk in self.server.rag_pipeline.retrieve(
                        query,
                        limit=20,
                        semantic_min_score=(
                            self.server.embedding_service.semantic_mode_threshold
                            if mode == "semantic"
                            else self.server.embedding_service.hybrid_score_threshold
                        ),
                        strict_topic_match=bool(search_topics),
                        required_topic_terms=search_topics,
                    )
                    if self.server.embedding_service._matches_filters(chunk["note"], filters)
                ]
                payload = [self.server.rag_pipeline.chunk_payload(chunk, mode) for chunk in results]
                # Keep the long-standing short-keyword search experience.  A
                # two-character query can be matched by SQLite FTS even when it
                # is too short to form a meaningful evidence chunk; this is a
                # list-search fallback only, never an AI answer source.
                compact_query = re.sub(r"\s+", "", query)
                if mode == "hybrid" and not payload and len(compact_query) <= 2:
                    notes = self.server.store.search_notes(query, **filters)
                    self._json([self._note_with_tabs(note) for note in notes])
                else:
                    self._json(payload)
                return
            elif mode == "keyword":
                notes = self.server.store.search_notes(query, **filters)
            self._json([self._note_with_tabs(note) for note in notes])
        elif parsed.path == "/api/taxonomy":
            self._json(self.server.store.get_taxonomy())
        elif parsed.path == "/api/tags/governance":
            self._json(self.server.store.tag_governance())
        elif parsed.path == "/api/folders":
            self._json(self.server.store.get_folders_tree())
        elif parsed.path == "/api/graph":
            self._graph()
        elif parsed.path.startswith("/api/notes/") and not "/" in parsed.path[len("/api/notes/"):]:
            # GET /api/notes/{id} - 获取单条笔记
            note_id = int(parsed.path[len("/api/notes/"):])
            self._json(self._note_with_tabs(self.server.store.get_note(note_id)))
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/tabs"):
            note_id = int(parsed.path[len("/api/notes/"):-len("/tabs")])
            self._json(self.server.store.get_note_tabs(note_id))
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/versions"):
            # GET /api/notes/{id}/versions - 历史版本列表
            note_id = int(parsed.path[len("/api/notes/"):-len("/versions")])
            self._json(self.server.store.list_versions(note_id))
        elif parsed.path.startswith("/api/versions/"):
            # GET /api/versions/{id} - 单个历史版本详情
            version_id = int(parsed.path[len("/api/versions/"):])
            self._json(self.server.store.get_version(version_id))
        elif parsed.path == "/api/ocr/status":
            self._json(self.server.ocr_service.status())
        elif parsed.path == "/api/embedding/status":
            self._json(self.server.embedding_service.status())
        elif parsed.path == "/api/intelligence/status":
            self._json(self.server.intelligence_service.status())
        elif parsed.path == "/api/intelligence/models":
            self._json({"models": self.server.intelligence_service.list_models(),
                        "current": self.server.config.llm_model_path})
        elif parsed.path == "/api/models/default-download":
            self._json(self.server.model_download_service.status())
        elif parsed.path == "/api/onboarding/status":
            # 检查 data_dir 下 .onboarded 标记文件是否存在
            marker = self.server.store.data_dir / ".onboarded"
            self._json({"onboarded": marker.exists()})
        elif parsed.path == "/api/tools/timers":
            self._json(self.server.tool_registry.list_active())
        elif parsed.path == "/api/tools/schedules":
            self._json(self.server.tool_registry.list_schedules())
        elif parsed.path == "/api/settings":
            self._json({
                "data_dir": str(self.server.store.data_dir),
                "theme": self.server.config.theme,
                "ai_auto_apply": self.server.config.ai_auto_apply,
                "background": self._custom_asset_url("background-custom"),
            })
        elif parsed.path == "/api/update/check":
            self._handle_update_check()
        elif parsed.path == "/api/export":
            self._export_json()
        elif parsed.path == "/api/export/backup":
            self._export_backup()
        elif parsed.path == "/api/links/broken":
            self._json({"links": self.server.store.get_broken_links()})
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/backlinks"):
            note_id = int(parsed.path.split("/")[3])
            self._json({"backlinks": self.server.store.get_backlinks(note_id)})
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/outlinks"):
            note_id = int(parsed.path.split("/")[3])
            self._json({"outlinks": self.server.store.get_outlinks(note_id)})
        elif parsed.path.startswith("/data/"):
            self._file(
                self.server.store.data_dir / unquote(parsed.path.removeprefix("/data/")),
                root=self.server.store.data_dir,
            )
        else:
            relative = parsed.path.lstrip("/") or "index.html"
            self._file(self.static_dir / relative, root=self.static_dir)

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/notes":
            body = self._read_json()
            note = self.server.store.create_note(
                body.get("title", ""),
                body.get("content", ""),
                body.get("image_data_url"),
                body.get("image_name"),
                body.get("category", "未分类"),
                body.get("subcategory", ""),
                body.get("tags", []),
                body.get("folder_id"),
                ai_status="pending" if self.server.config.ai_auto_apply else "not_requested",
            )
            if note.attachment_path:
                self.server.ocr_service.enqueue(note.id)
                note = self.server.store.get_note(note.id)
            self.server.embedding_service.enqueue(note.id)
            if self.server.config.ai_auto_apply:
                self.server.intelligence_service.enqueue(note.id)
            self._json(note.as_dict(), HTTPStatus.CREATED)
        elif parsed.path == "/api/documents":
            body = self._read_json()
            document = self.server.store.create_document_from_data_url(
                body.get("name", ""), body.get("data_url", ""), body.get("chunk_profile", "standard")
            )
            self.server.document_service.enqueue(document.id)
            self._json(document.as_dict(), HTTPStatus.CREATED)
        elif parsed.path.startswith("/api/documents/") and parsed.path.endswith("/chunk-profile"):
            document_id = int(parsed.path[len("/api/documents/"):-len("/chunk-profile")])
            body = self._read_json()
            document = self.server.store.set_document_chunk_profile(document_id, body.get("profile", "standard"))
            self.server.document_service.reindex(document.id)
            self._json(self.server.store.get_document(document.id).as_dict(), HTTPStatus.ACCEPTED)
        elif parsed.path.startswith("/api/documents/") and parsed.path.endswith("/chunk-adjustments"):
            document_id = int(parsed.path[len("/api/documents/"):-len("/chunk-adjustments")])
            body = self._read_json()
            action = str(body.get("action", "")).strip()
            if action == "merge":
                self.server.store.add_document_chunk_merge(
                    document_id, int(body.get("start_order")), int(body.get("end_order"))
                )
            elif action == "reset":
                self.server.store.clear_document_chunk_adjustments(document_id)
            else:
                raise ValueError("不支持的切片调整操作")
            self.server.document_service.reindex(document_id)
            self._json({"document": self.server.store.get_document(document_id).as_dict()}, HTTPStatus.ACCEPTED)
        elif parsed.path.startswith("/api/documents/") and parsed.path.endswith("/reindex"):
            document_id = int(parsed.path[len("/api/documents/"):-len("/reindex")])
            self._read_json()
            self.server.document_service.reindex(document_id)
            self._json(self.server.store.get_document(document_id).as_dict(), HTTPStatus.ACCEPTED)
        elif parsed.path == "/api/folders":
            body = self._read_json()
            name = str(body.get("name", "")).strip()
            if not name:
                self._json({"error": "名称不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            parent_id = body.get("parent_id")
            folder = self.server.store.create_folder(parent_id, name)
            self._json(folder, HTTPStatus.CREATED)
        elif parsed.path == "/api/welcome/restore":
            welcome_image = self.static_dir / "PocketMemory_welcome.jpg"
            note = self.server.store.restore_welcome_note(welcome_image)
            self._json(self._note_with_tabs(note), HTTPStatus.CREATED)
        elif parsed.path == "/api/moc/generate":
            self._generate_moc()
        elif parsed.path == "/api/reports/draft":
            body = self._read_json()
            report = self.server.report_service.create_draft(
                body.get("title", ""), body.get("template", "project"), body.get("sources", [])
            )
            self._json(report, HTTPStatus.CREATED)
        elif parsed.path == "/api/embedding/rebuild":
            self.server.embedding_service.rebuild()
            self._json(self.server.embedding_service.status(), HTTPStatus.ACCEPTED)
        elif parsed.path == "/api/ask":
            request_started = time.perf_counter()
            body = self._read_json()
            question = str(body.get("question", "")).strip()
            if not question:
                self._json({"error": "问题不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            # 问答默认是单轮独立检索。忽略旧客户端传来的 history / conversation，
            # 避免上一题的主体或答案污染当前问题的召回。
            history: list[dict] = []
            conversation = {"mode": "single_turn", "subject": question, "questions": [question]}
            needs_action_target_clarification = self._needs_action_target_clarification(question)
            # Explicit notebook statistics are deterministic database queries.
            # Run them before checking local model runtimes: users should still
            # be able to count their own notes while a model is downloading or
            # temporarily unavailable.
            current_intent = IntentRouter.detect(question, store=self.server.store)
            if current_intent.type == "stats":
                result = self.server.rag_pipeline.route(question, intent=current_intent)
                timing = self._ask_timing(request_started, retrieval_seconds=0.0)
                if body.get("stream"):
                    self._send_simple_stream(result.answer, result.kind, result.data, timing=timing,
                                             conversation=conversation)
                else:
                    self._json({"answer": result.answer, "sources": [], "kind": result.kind,
                                "data": result.data, "expanded_terms": [], "timing": timing,
                                "conversation": conversation})
                return
            if not self.server.embedding_service.engine.available:
                self._json({"error": self.server.embedding_service.engine.unavailable_reason,
                            "code": "embedding_required"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if not self.server.intelligence_service.engine.available:
                engine = self.server.intelligence_service.engine
                model_path = getattr(engine, "model_path", None)
                model_missing = isinstance(model_path, Path) and not model_path.is_file()
                self._json({"error": engine.unavailable_reason,
                            "code": "model_missing" if model_missing else "model_runtime_unavailable",
                            "model_download": self.server.model_download_service.status()}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if needs_action_target_clarification or self.server.rag_pipeline.requires_clarification(question):
                answer = (
                    self._action_target_clarification_answer(question)
                    if needs_action_target_clarification
                    else "请明确要咨询的对象名称，例如具体的 Agent、合同或项目名称。"
                )
                timing = self._ask_timing(request_started, retrieval_seconds=0.0)
                if body.get("stream"):
                    self._send_simple_stream(answer, "clarification", {}, timing=timing,
                                             conversation=conversation)
                else:
                    self._json({"answer": answer, "sources": [], "kind": "clarification",
                                "expanded_terms": [], "data": {}, "timing": timing,
                                "conversation": conversation})
                return
            engine = self.server.intelligence_service.engine
            # 意图路由：规则优先 + LLM 兜底分类，覆盖时间/列表/统计/元数据/分类/摘要/比较/实体/关联/通用问答
            retrieval_question = question
            # Keep the pipeline bound to the live service. This also makes a
            # controlled engine reload immediately visible to retrieval.
            self.server.rag_pipeline.embedding_service = self.server.embedding_service
            retrieval_started = time.perf_counter()
            # Intent is a property of the current turn.  The expanded question
            # is only for retrieval: classifying it would let a previous
            # statistics request hijack an unrelated short follow-up.
            result = self.server.rag_pipeline.route(
                retrieval_question,
                engine,
                intent=current_intent,
                evidence_question=question,
            )
            retrieval_seconds = time.perf_counter() - retrieval_started
            timing = self._ask_timing(request_started, retrieval_seconds=retrieval_seconds)
            expanded_terms = result.expanded_terms
            # stats/taxonomy：答案已预生成，无需调用 LLM
            if result.kind in ("stats", "taxonomy"):
                if body.get("stream"):
                    self._send_simple_stream(result.answer, result.kind, result.data, timing=timing,
                                             conversation=conversation)
                else:
                    self._json({"answer": result.answer, "sources": [], "kind": result.kind,
                                "data": result.data, "expanded_terms": [], "timing": timing,
                                "conversation": conversation})
                return
            # 列表型意图（time/metadata/list/entity/related）：生成笔记清单，不调 LLM 流式复述
            # 用户只想知道"有哪些笔记符合"，下方有笔记链接可点击查看内容
            if result.kind in ("time", "metadata", "list", "entity", "related") and result.chunks:
                sources = self.server.rag_pipeline.source_payloads(question, result.chunks)
                answer = self._build_list_answer(result.kind, sources)
                if body.get("stream"):
                    self._send_simple_stream(answer, result.kind, result.data, sources=sources, timing=timing,
                                             conversation=conversation)
                else:
                    self._json({"answer": answer, "sources": sources, "kind": result.kind,
                                "expanded_terms": [], "data": result.data, "timing": timing,
                                "conversation": conversation})
                return
            sources = self.server.rag_pipeline.source_payloads(question, result.chunks)
            contexts = self.server.rag_pipeline.contexts(result.chunks, sources=sources)
            if not contexts:
                empty_answer = result.empty_answer or "没有找到足够相似的笔记，暂时无法基于笔记回答。"
                if body.get("stream"):
                    self._send_simple_stream(empty_answer, result.kind, result.data, timing=timing,
                                             conversation=conversation)
                else:
                    self._json({"answer": empty_answer, "sources": [], "kind": result.kind,
                                "expanded_terms": expanded_terms, "data": result.data, "timing": timing,
                                "conversation": conversation})
                return
            gen_question = result.prompt_override or question
            if body.get("stream"):
                self._ask_stream(gen_question, contexts, sources, history=history,
                                 expanded_terms=expanded_terms, kind=result.kind,
                                 data=result.data, display_question=question,
                                 request_started=request_started, retrieval_seconds=retrieval_seconds,
                                 conversation=conversation)
            else:
                answer, answer_sources = self._answer_with_history(engine, gen_question, contexts, history, sources)
                self._json({"answer": answer, "sources": answer_sources, "kind": result.kind,
                            "expanded_terms": expanded_terms, "data": result.data,
                            "timing": self._ask_timing(request_started, retrieval_seconds=retrieval_seconds),
                            "conversation": conversation})
        elif parsed.path == "/api/ask/llama-index":
            body = self._read_json()
            question = str(body.get("question", "")).strip()
            if not question:
                self._json({"error": "问题不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                self._json(self._llama_index_answer(question))
            except Exception as exc:
                self._json({"error": f"LlamaIndex 实验问答失败: {exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/tags/remove"):
            note_id = int(parsed.path[len("/api/notes/"):-len("/tags/remove")])
            body = self._read_json()
            try:
                note = self.server.store.remove_group_tag(note_id, str(body.get("tag", "")))
            except (KeyError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(note.as_dict())
        elif parsed.path == "/api/notes/reorder":
            body = self._read_json()
            try:
                self.server.store.move_note(
                    int(body.get("note_id")), int(body.get("target_note_id")), bool(body.get("after"))
                )
            except (KeyError, TypeError, ValueError) as exc:
                self._json({"error": str(exc) or "无法调整该笔记的位置"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": True})
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/intelligence"):
            note_id = int(parsed.path.split("/")[-2])
            self.server.intelligence_service.enqueue(note_id, apply=True)
            self._json(self.server.store.get_note(note_id).as_dict(), HTTPStatus.ACCEPTED)
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/ocr"):
            note_id = int(parsed.path.split("/")[-2])
            note = self.server.store.get_note(note_id)
            if not note.attachment_path:
                self._json({"error": "这条笔记没有图片"}, HTTPStatus.BAD_REQUEST)
                return
            self.server.ocr_service.enqueue(note_id)
            self._json(self.server.store.get_note(note_id).as_dict(), HTTPStatus.ACCEPTED)
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/tabs"):
            # 为笔记新建页签
            parent_note_id = int(parsed.path.split("/")[-2])
            body = self._read_json()
            tab_name = str(body.get("tab_name", "")).strip()
            try:
                note = self.server.store.create_note(
                    title=tab_name or "新页签",
                    content=body.get("content", ""),
                    parent_note_id=parent_note_id,
                    tab_name=tab_name,
                    ai_status="pending" if self.server.config.ai_auto_apply else "not_requested",
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.server.embedding_service.enqueue(note.id)
            if self.server.config.ai_auto_apply:
                self.server.intelligence_service.enqueue(note.id)
            self._json(note.as_dict(), HTTPStatus.CREATED)
        elif parsed.path.startswith("/api/versions/") and parsed.path.endswith("/restore"):
            # POST /api/versions/{id}/restore - 恢复到指定历史版本
            version_id = int(parsed.path[len("/api/versions/"):-len("/restore")])
            self._read_json()  # 消费空 body
            try:
                note = self.server.store.restore_version(version_id)
                self.server.embedding_service.enqueue(note.id)
                self._json(note.as_dict())
            except KeyError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        elif parsed.path == "/api/settings/choose-data-dir":
            selected = self.server.choose_directory(self.server.store.data_dir)
            self._json({"data_dir": str(selected) if selected else None})
        elif parsed.path == "/api/settings/migrate":
            body = self._read_json()
            target_value = str(body.get("data_dir", "")).strip()
            if not target_value:
                self._json({"error": "data_dir 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            target = Path(target_value).expanduser()
            migrated = self.server.migrate_data_dir(target)
            self.server.config.set_data_dir(migrated)
            self._json({"data_dir": str(migrated)})
        elif parsed.path == "/api/settings/switch-data-dir":
            body = self._read_json()
            target_value = str(body.get("data_dir", "")).strip()
            if not target_value:
                self._json({"error": "data_dir 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            selected = self.server.switch_data_dir(Path(target_value).expanduser())
            self.server.config.set_data_dir(selected)
            self._json({"data_dir": str(selected)})
        elif parsed.path == "/api/settings/theme":
            body = self._read_json()
            theme = str(body.get("theme", "")).strip()
            if not theme:
                self._json({"error": "theme 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            self.server.config.set_theme(theme)
            self._json({"theme": theme})
        elif parsed.path == "/api/settings/background":
            body = self._read_json()
            data_url = str(body.get("image_data_url", "")).strip()
            if not data_url:
                self._json({"error": "image_data_url 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            url = self._save_custom_asset("background-custom", data_url)
            self._json({"url": url})
        elif parsed.path == "/api/settings/ai-mode":
            body = self._read_json()
            enabled = bool(body.get("auto_apply", True))
            self.server.config.set_ai_auto_apply(enabled)
            self.server.intelligence_service.auto_apply = enabled
            self._json({"ai_auto_apply": enabled})
        elif parsed.path == "/api/tags/merge":
            body = self._read_json()
            try:
                affected = self.server.store.merge_tags(str(body.get("from", "")), str(body.get("to", "")))
            except (KeyError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"affected_notes": affected})
        elif parsed.path == "/api/tools":
            body = self._read_json()
            user_input = str(body.get("input", "")).strip()
            if not user_input:
                self._json({"error": "输入不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            intent = self.server.intelligence_service.engine.parse_tool_intent(user_input)
            tool_name = intent.get("tool", "none")
            if tool_name == "none":
                self._json(intent)
                return
            args = intent.get("args", {}) or {}
            result = self.server.tool_registry.execute(tool_name, args)
            self._json({"tool": tool_name, "args": args, "result": result})
        elif parsed.path == "/api/tools/timers/cancel":
            body = self._read_json()
            timer_id = str(body.get("timer_id", ""))
            ok = self.server.tool_registry.cancel_timer(timer_id)
            self._json({"ok": ok})
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/pin"):
            note_id = int(parsed.path[len("/api/notes/"):-len("/pin")])
            new_value = self.server.store.toggle_pin(note_id)
            self._json({"id": note_id, "pinned": new_value})
        elif parsed.path.startswith("/api/notes/") and parsed.path.endswith("/favorite"):
            note_id = int(parsed.path[len("/api/notes/"):-len("/favorite")])
            new_value = self.server.store.toggle_favorite(note_id)
            self._json({"id": note_id, "favorite": new_value})
        elif parsed.path == "/api/shutdown":
            # 优雅退出：仅接受本机请求，先响应再异步关闭
            self._json({"ok": True, "message": "shutting down"})
            threading.Thread(target=self._async_shutdown, name="pocket-memory-shutdown", daemon=True).start()
        elif parsed.path == "/api/intelligence/unload":
            # 手动卸载 LLM 模型，释放内存（不影响 server 运行）
            self.server.intelligence_service.unload_model()
            self._json({"ok": True, "message": "model unloaded"})
        elif parsed.path == "/api/intelligence/models":
            # 切换 LLM 模型：先卸载当前模型，更新配置，下次调用自动加载新模型
            body = self._read_json()
            model_rel = str(body.get("model_path", "")).strip()
            if not model_rel:
                self._json({"error": "model_path 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                self.server.intelligence_service.switch_model(model_rel)
                self.server.config.set_llm_model_path(model_rel)
                self._json({"ok": True, "message": "模型已切换，下次调用时自动加载"})
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif parsed.path == "/api/models/default-download":
            self._read_json()
            self._json(self.server.model_download_service.start(), HTTPStatus.ACCEPTED)
        elif parsed.path == "/api/models/default-download/cancel":
            self._read_json()
            self._json(self.server.model_download_service.cancel(), HTTPStatus.ACCEPTED)
        elif parsed.path == "/api/onboarding/complete":
            # 标记已完成引导（写入 .onboarded 文件，持久化到磁盘）
            marker = self.server.store.data_dir / ".onboarded"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1", encoding="utf-8")
            self._json({"ok": True})
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _async_shutdown(self) -> None:
        """异步关闭 server：等待响应发送完成后退出。"""
        import time
        time.sleep(0.3)  # 等待 HTTP 响应发送完毕
        logger.info("收到 shutdown 请求，开始关闭 server")
        self.server._shutdown_requested = True
        # shutdown() 必须在非 serve_forever 线程调用，当前已在 handler 线程
        self.server.shutdown()

    def _handle_put(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/documents/") and parsed.path.endswith("/file"):
            document_id = int(parsed.path[len("/api/documents/"):-len("/file")])
            body = self._read_json()
            document = self.server.store.replace_document_from_data_url(
                document_id, body.get("name", ""), body.get("data_url", "")
            )
            self.server.document_service.enqueue(document.id)
            self._json(self.server.store.get_document(document.id).as_dict(), HTTPStatus.ACCEPTED)
        elif parsed.path.startswith("/api/notes/"):
            note_id = int(parsed.path.rsplit("/", 1)[1])
            body = self._read_json()
            # 部分更新：只有 body 中出现的字段才更新，其余保持原值
            kwargs = {}
            if "title" in body:
                kwargs["title"] = body["title"]
            if "content" in body:
                kwargs["content"] = body["content"]
            if "category" in body:
                kwargs["category"] = body["category"]
            if "subcategory" in body:
                kwargs["subcategory"] = body["subcategory"]
            if "tags" in body:
                kwargs["tags"] = body["tags"]
            if "folder_id" in body:
                kwargs["folder_id"] = body["folder_id"]
            if "tab_name" in body:
                kwargs["tab_name"] = body["tab_name"]
            try:
                note = self.server.store.update_note(note_id, **kwargs)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.server.embedding_service.enqueue(note.id)
            if self.server.config.ai_auto_apply:
                self.server.intelligence_service.enqueue(note.id)
            self._json(self._note_with_tabs(note))
        elif parsed.path.startswith("/api/folders/"):
            folder_id = int(parsed.path.rsplit("/", 1)[1])
            body = self._read_json()
            name = str(body.get("name", "")).strip()
            if not name:
                self._json({"error": "名称不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            folder = self.server.store.update_folder(folder_id, name)
            self._json(folder)
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _handle_delete(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/notes/"):
            note_id = int(parsed.path.rsplit("/", 1)[1])
            self.server.store.delete_note(note_id)
            self._json({"deleted": note_id})
        elif parsed.path.startswith("/api/documents/"):
            document_id = int(parsed.path[len("/api/documents/"):])
            self.server.store.delete_document(document_id)
            self._json({"deleted": document_id})
        elif parsed.path.startswith("/api/folders/"):
            folder_id = int(parsed.path.rsplit("/", 1)[1])
            self.server.store.delete_folder(folder_id)
            self._json({"deleted": folder_id})
        elif parsed.path == "/api/settings/background":
            self._delete_custom_asset("background-custom")
            self._json({"ok": True})
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _custom_asset_url(self, prefix: str) -> str | None:
        data_dir = self.server.store.data_dir
        for ext in ("png", "jpg", "jpeg", "webp", "gif", "bmp"):
            target = data_dir / f"{prefix}.{ext}"
            if target.is_file():
                return f"/data/{prefix}.{ext}"
        return None

    def _save_custom_asset(self, prefix: str, data_url: str) -> str:
        match = re.fullmatch(r"data:image/([a-zA-Z0-9.+-]+);base64,(.+)", data_url, re.DOTALL)
        if not match:
            self._json({"error": "无效的图片数据"}, HTTPStatus.BAD_REQUEST)
            raise _SilentAbort()
        ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(match.group(1).lower(), match.group(1).lower())
        if ext not in {"png", "jpg", "webp", "gif", "bmp"}:
            self._json({"error": "暂不支持这种图片格式"}, HTTPStatus.BAD_REQUEST)
            raise _SilentAbort()
        data_dir = self.server.store.data_dir
        # 清除同 prefix 的旧文件
        for old_ext in ("png", "jpg", "jpeg", "webp", "gif", "bmp"):
            (data_dir / f"{prefix}.{old_ext}").unlink(missing_ok=True)
        try:
            image_bytes = base64.b64decode(match.group(2), validate=True)
        except binascii.Error:
            self._json({"error": "无效的图片数据"}, HTTPStatus.BAD_REQUEST)
            raise _SilentAbort()
        target = data_dir / f"{prefix}.{ext}"
        target.write_bytes(image_bytes)
        return f"/data/{prefix}.{ext}"

    def _delete_custom_asset(self, prefix: str) -> None:
        data_dir = self.server.store.data_dir
        for ext in ("png", "jpg", "jpeg", "webp", "gif", "bmp"):
            (data_dir / f"{prefix}.{ext}").unlink(missing_ok=True)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json({"error": "无效的 Content-Length"}, HTTPStatus.BAD_REQUEST)
            raise _SilentAbort()
        if length < 0 or length > MAX_JSON_BODY:
            self._json({"error": "请求体过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            raise _SilentAbort()
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "无效的 JSON"}, HTTPStatus.BAD_REQUEST)
            raise _SilentAbort()
        if not isinstance(payload, dict):
            self._json({"error": "JSON 请求体必须是对象"}, HTTPStatus.BAD_REQUEST)
            raise _SilentAbort()
        return payload

    def _note_with_tabs(self, note) -> dict:
        """返回 note dict 并附带 tab_count（子页签数量）和 backlink_count（反链数）。"""
        payload = note.as_dict()
        payload["tab_count"] = self.server.store.tab_count(note.id)
        payload["backlink_count"] = self.server.store.backlink_count(note.id)
        return payload

    @staticmethod
    def _filters(query: str) -> dict:
        parameters = parse_qs(query)
        result = {
            key: parameters[key][0]
            for key in ("category", "subcategory", "tag", "since")
            if parameters.get(key, [""])[0]
        }
        if parameters.get("favorite", [""])[0].lower() in {"1", "true", "yes"}:
            result["favorite"] = True
        if parameters.get("folder_id", [""])[0]:
            try:
                result["folder_id"] = int(parameters["folder_id"][0])
            except ValueError:
                pass
        return result

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_update_check(self) -> None:
        """从服务器获取 version.json，与当前版本比较，返回是否有更新。"""
        if not UPDATE_CHECK_URL:
            self._json({"has_update": False, "current_version": APP_VERSION, "enabled": False})
            return
        try:
            with urlopen(UPDATE_CHECK_URL, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (URLError, json.JSONDecodeError, OSError) as exc:
            logger.debug("版本检查失败: %s", exc)
            self._json({"has_update": False, "current_version": APP_VERSION})
            return

        latest = str(data.get("version", "0.0.0"))

        def _parse(ver: str) -> tuple[int, ...]:
            parts = []
            for p in ver.split("."):
                try:
                    parts.append(int(p))
                except ValueError:
                    parts.append(0)
            return tuple(parts)

        has_update = _parse(latest) > _parse(APP_VERSION)
        self._json({
            "has_update": has_update,
            "current_version": APP_VERSION,
            "latest_version": latest,
            "download_url": data.get("download_url", ""),
            "release_notes": data.get("release_notes", ""),
        })

    def _export_json(self) -> None:
        """导出全部笔记为 JSON 文件，由浏览器负责保存。"""
        from datetime import datetime
        notes = self.server.store.export_all_notes()
        payload = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "data_dir": str(self.server.store.data_dir),
            "count": len(notes),
            "notes": notes,
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"pocket-memory-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

        self._send_download(body, filename, "application/json; charset=utf-8")

    def _export_backup(self) -> None:
        """导出完整 ZIP，并交由浏览器保存，避免写入用户下载目录。"""
        from datetime import datetime

        filename = f"pocket-memory-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        with tempfile.NamedTemporaryFile(prefix="pocket-memory-backup-", suffix=".zip", delete=False) as temp_file:
            backup_path = Path(temp_file.name)
        try:
            self.server.store.create_backup(backup_path)
            self._send_download_file(backup_path, filename, "application/zip")
        finally:
            backup_path.unlink(missing_ok=True)

    def _graph(self) -> None:
        """生成知识图谱数据：节点=笔记+标签，边=标签归属+[[双链]]。

        优化：
        - 标签节点仅保留出现在 3 篇及以上笔记中的，减少噪声
        - 每个节点附带 degree（连接数），前端据此过滤孤立节点
        - 孤立笔记节点（无标签、无双链）仍返回，前端默认折叠
        """
        # 欢迎页是产品引导，不是用户知识资产；不把它混入关系图。
        notes = [note for note in self.server.store.export_notes() if note.get("source_type") != "welcome"]
        all_nodes: list[dict] = []
        edges: list[dict] = []
        tag_counts: dict[str, int] = {}

        for note in notes:
            for tag in note.get("tags") or []:
                name = str(tag).strip()
                if name:
                    tag_counts[name] = tag_counts.get(name, 0) + 1

        # 标签阈值提高到 3，减少噪声
        keep_tags = {name for name, count in tag_counts.items() if count >= 3}

        for note in notes:
            all_nodes.append({
                "id": f"note:{note['id']}",
                "label": note.get("title") or "无标题",
                "type": "note",
                "category": note.get("category") or "其他",
                "noteId": note["id"],
                "pinned": bool(note.get("pinned")),
            })
            for tag in note.get("tags") or []:
                name = str(tag).strip()
                if name in keep_tags:
                    edges.append({"source": f"note:{note['id']}", "target": f"tag:{name}", "type": "tag"})

        for name, count in tag_counts.items():
            if name in keep_tags:
                all_nodes.append({"id": f"tag:{name}", "label": name, "type": "tag", "count": count})

        # 双链边：直接读 note_links 持久化表（仅有效链接）
        link_rows = self.server.store.connection.execute(
            "SELECT source_id, target_id FROM note_links WHERE target_id IS NOT NULL"
        ).fetchall()
        for row in link_rows:
            if row["source_id"] != row["target_id"]:
                edges.append({
                    "source": f"note:{row['source_id']}",
                    "target": f"note:{row['target_id']}",
                    "type": "link",
                })

        # 失效链接：target_id 为 NULL 的出链。
        # 目标笔记不存在，用虚拟节点表示（红色空心），前端用 showBroken 开关控制显示。
        # 同一个失效标题被多篇笔记引用时，虚拟节点去重。
        broken_links = self.server.store.get_broken_links()
        broken_nodes_map: dict[str, dict] = {}
        for bl in broken_links:
            bid = f"broken:{bl['target_title']}"
            if bid not in broken_nodes_map:
                broken_nodes_map[bid] = {
                    "id": bid,
                    "label": bl["target_title"],
                    "type": "broken",
                }
            edges.append({
                "source": f"note:{bl['source_id']}",
                "target": bid,
                "type": "broken",
            })
        all_nodes.extend(broken_nodes_map.values())

        # 去重边
        seen: set[tuple] = set()
        unique_edges: list[dict] = []
        for edge in edges:
            key = (tuple(sorted([edge["source"], edge["target"]])), edge["type"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(edge)

        # 计算每个节点的连接数（degree），前端据此过滤孤立节点
        degree: dict[str, int] = {}
        for edge in unique_edges:
            degree[edge["source"]] = degree.get(edge["source"], 0) + 1
            degree[edge["target"]] = degree.get(edge["target"], 0) + 1
        for node in all_nodes:
            node["degree"] = degree.get(node["id"], 0)

        self._json({"nodes": all_nodes, "edges": unique_edges})

    def _generate_moc(self) -> None:
        """为指定文件夹生成 MOC（Map of Content）笔记。

        流程：收集 folder 下笔记 → LLM 生成 MOC 内容 → 创建笔记（含 [[双链]]）→ 仅入向量队列。
        """
        body = self._read_json()
        folder_id = body.get("folder_id")
        category = body.get("category")  # 可选：按分类而非文件夹生成
        if folder_id is None and not category:
            self._json({"error": "需要指定 folder_id 或 category"}, HTTPStatus.BAD_REQUEST)
            return

        store = self.server.store
        if folder_id is not None:
            notes = store.list_notes(limit=100000, folder_id=folder_id)
            folder = store.get_folder(folder_id) if hasattr(store, "get_folder") else None
            scope_name = folder.get("name", "未分类") if folder else "未分类"
        else:
            notes = store.list_notes(limit=100000, category=category)
            scope_name = category

        if not notes:
            self._json({"error": f"「{scope_name}」下没有笔记，无法生成 MOC"}, HTTPStatus.BAD_REQUEST)
            return

        # 过滤掉已有 MOC 笔记：避免把上一次的 MOC 内容当作"笔记"再喂给 LLM，
        # 否则会 MOC 套 MOC，内容滚雪球累积，且 LLM 会照搬旧 MOC 里的占位符。
        moc_prefix = "🗺️ MOC:"
        notes_for_moc = [n for n in notes if not n.title.startswith(moc_prefix)]
        if not notes_for_moc:
            self._json({"error": f"「{scope_name}」下没有普通笔记（已排除已有 MOC），无法生成 MOC"}, HTTPStatus.BAD_REQUEST)
            return

        # 构造笔记摘要供 LLM 使用
        notes_summary = [
            {
                "title": n.title,
                "excerpt": (n.content or "")[:200],
                "tags": n.tags or [],
                "category": n.category,
            }
            for n in notes_for_moc
        ]

        # 调用 LLM 生成 MOC 内容（含 [[双链]]）
        engine = self.server.intelligence_service.engine
        try:
            moc_content = engine.generate_moc(scope_name, notes_summary)
        except Exception as exc:
            self._json({"error": f"MOC 生成失败: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 已有同名 MOC 则更新内容（保留 note id，维持双链 target_id 有效），
        # 否则新建。避免重复生成导致 MOC 笔记堆积。
        moc_title = f"🗺️ MOC: {scope_name}"
        existing_moc = next((n for n in notes if n.title == moc_title), None)
        if existing_moc:
            moc_note = store.update_note(existing_moc.id, title=moc_title, content=moc_content)
        else:
            moc_note = store.create_note(
                title=moc_title,
                content=moc_content,
                category=category or scope_name,
                subcategory="",
                tags=["MOC"],
                folder_id=folder_id,
            )
        # 仅入向量队列（可搜索），不触发 AI 整理（MOC 不需要被分类）
        self.server.embedding_service.enqueue(moc_note.id)
        payload = self._note_with_tabs(moc_note)
        payload["updated"] = bool(existing_moc)
        self._json(payload, HTTPStatus.CREATED if not existing_moc else HTTPStatus.OK)

    @staticmethod
    def _ask_timing(request_started: float, *, retrieval_seconds: float,
                    first_token_seconds: float | None = None,
                    generation_seconds: float | None = None,
                    model_warm: bool | None = None) -> dict:
        """Build user-visible timing telemetry without retaining question content."""
        timing = {
            "retrieval_seconds": round(max(retrieval_seconds, 0.0), 3),
            "total_seconds": round(max(time.perf_counter() - request_started, 0.0), 3),
        }
        if first_token_seconds is not None:
            timing["first_token_seconds"] = round(max(first_token_seconds, 0.0), 3)
        if generation_seconds is not None:
            timing["generation_seconds"] = round(max(generation_seconds, 0.0), 3)
        if model_warm is not None:
            timing["model_warm"] = model_warm
        return timing

    def _send_simple_stream(self, answer: str, kind: str, data: dict | None = None,
                            sources: list[dict] | None = None, timing: dict | None = None,
                            conversation: dict | None = None) -> None:
        """发送不含 token 流的 SSE：仅 sources + done，用于预生成答案（stats/taxonomy/列表型/空结果）。"""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def write_event(payload: dict) -> None:
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        src = sources or []
        write_event({"type": "sources", "sources": src, "expanded_terms": [], "kind": kind,
                     "data": data or {}, "timing": timing or {}, "conversation": conversation or {}})
        write_event({"type": "done", "answer": answer, "sources": src, "kind": kind,
                     "data": data or {}, "timing": timing or {}, "conversation": conversation or {}})

    # 列表型意图的答案模板：用简短清单代替 LLM 流式复述笔记内容
    _LIST_ANSWER_TEMPLATES = {
        "time": "找到 {n} 条笔记：\n{list}",
        "metadata": "找到 {n} 条符合条件的笔记：\n{list}",
        "list": "找到 {n} 条相关笔记：\n{list}",
        "entity": "找到 {n} 条相关笔记：\n{list}",
        "related": "找到 {n} 条语义相关笔记：\n{list}",
    }

    def _build_list_answer(self, kind: str, sources: list[dict]) -> str:
        """生成列表型意图的简短答案：笔记标题清单，不复述笔记内容。"""
        if not sources:
            return "没有找到相关笔记。"
        template = self._LIST_ANSWER_TEMPLATES.get(kind, "找到 {n} 条笔记：\n{list}")
        lines = []
        for i, src in enumerate(sources, 1):
            title = src.get("title", "未命名笔记")
            reason = src.get("reason", "")
            # 时间意图带上创建时间；其他意图只显示标题
            if kind == "time" and reason:
                lines.append(f"{i}. {title}（{reason}）")
            else:
                lines.append(f"{i}. {title}")
        return template.format(n=len(sources), list="\n".join(lines))

    def _ask_stream(self, question: str, contexts: list[dict], sources: list[dict], history: list[dict] | None = None, expanded_terms: list[str] | None = None, kind: str = "qa", data: dict | None = None, display_question: str | None = None, request_started: float | None = None, retrieval_seconds: float = 0.0, conversation: dict | None = None) -> None:
        """SSE 流式问答：先发 sources，再逐 token 发答案，最后发 done。"""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def write_event(data: dict) -> None:
            payload = json.dumps(data, ensure_ascii=False)
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

        request_started = request_started if request_started is not None else time.perf_counter()
        engine = self.server.intelligence_service.engine
        model_warm = bool(getattr(engine, "model_loaded", False))
        write_event({"type": "sources", "sources": sources, "expanded_terms": expanded_terms or [],
                     "kind": kind, "data": data or {},
                     "timing": self._ask_timing(request_started, retrieval_seconds=retrieval_seconds,
                                                model_warm=model_warm),
                     "conversation": conversation or {}})

        full_answer = ""
        generation_started = time.perf_counter()
        first_token_at: float | None = None
        try:
            for delta in self._answer_stream_with_history(engine, question, contexts, history):
                if first_token_at is None and delta:
                    first_token_at = time.perf_counter()
                full_answer += delta
                write_event({"type": "token", "text": delta})
        except Exception as exc:
            write_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return

        clean_answer = getattr(self.server.intelligence_service.engine, "_clean_answer", None)
        cleaned = clean_answer(full_answer) if callable(clean_answer) else full_answer.strip()
        cleaned, answer_sources = self.server.rag_pipeline.enforce_answer_contract(
            display_question or question, cleaned, sources
        )
        timing = self._ask_timing(
            request_started,
            retrieval_seconds=retrieval_seconds,
            first_token_seconds=(first_token_at - request_started) if first_token_at else None,
            generation_seconds=time.perf_counter() - generation_started,
            model_warm=model_warm,
        )
        write_event({"type": "done", "answer": cleaned, "sources": answer_sources,
                     "kind": kind, "data": data or {}, "timing": timing,
                     "conversation": conversation or {}})

    @staticmethod
    def _accepts_history(method: Callable) -> bool:
        """Keep the engine adapter compatible with simple third-party engines."""
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.name == "history" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    @staticmethod
    def _needs_action_target_clarification(question: str) -> bool:
        """单轮问答中，缺少对象的界面操作问题应先要求用户说清对象。"""
        compact = re.sub(r"\s+", "", str(question or ""))
        match = re.fullmatch(
            r"(?:为什么|为何|怎么)(?:会)?(?:不能|无法|不可以|没法)"
            r"(回退|删除|移除|清空|保存|上传|下载|导出|合并|还原|打开)[？?。！!]*",
            compact,
        )
        return bool(match)

    @staticmethod
    def _action_target_clarification_answer(question: str) -> str:
        action = re.search(r"(?:不能|无法|不可以|没法)(回退|删除|移除|清空|保存|上传|下载|导出|合并|还原|打开)", question)
        examples = {
            "回退": "为什么不能还原自动切片？",
            "还原": "为什么不能还原自动切片？",
            "删除": "为什么不能删除这条笔记？",
            "移除": "为什么不能删除这个标签？",
            "清空": "为什么不能清空笔记？",
            "保存": "为什么不能保存当前笔记？",
            "上传": "为什么不能上传这个文件？",
            "下载": "为什么不能下载模型？",
            "导出": "为什么不能导出完整备份？",
            "合并": "为什么不能合并这两个切片？",
            "打开": "为什么不能打开这个文档？",
        }
        target = action.group(1) if action else "操作"
        return f"请明确要{target}的对象或功能，例如“{examples.get(target, '当前操作为什么失败？')}”。"

    def _answer_with_history(self, engine, question: str, contexts: list[dict], history: list[dict], sources: list[dict]) -> tuple[str, list[dict]]:
        method = engine.answer
        if self._accepts_history(method):
            # Conversation history resolves retrieval only. It is not source
            # evidence and a small local model may otherwise repeat an earlier
            # unsupported answer as if it were a fact in the current response.
            answer = method(question, contexts, history=[])
        else:
            answer = method(question, contexts)
        return self.server.rag_pipeline.enforce_answer_contract(question, answer, sources)

    @classmethod
    def _answer_stream_with_history(cls, engine, question: str, contexts: list[dict], history: list[dict]):
        method = engine.answer_stream
        if cls._accepts_history(method):
            return method(question, contexts, history=[])
        return method(question, contexts)

    def _llama_index_answer(self, question: str) -> dict:
        root = Path(__file__).resolve().parent.parent
        python = root / ".venv-rag-frameworks" / "Scripts" / "python.exe"
        script = root / "scripts" / "llamaindex_query.py"
        if not python.is_file():
            raise RuntimeError("没有找到 .venv-rag-frameworks，请先安装 LlamaIndex 实验依赖")
        if not script.is_file():
            raise RuntimeError("没有找到 scripts/llamaindex_query.py")
        env = dict(os.environ)
        env.setdefault("HAYSTACK_TELEMETRY_ENABLED", "False")
        env.setdefault("HAYSTACK_HOME", str(root / ".haystack-test"))
        env.setdefault("PYTHONIOENCODING", "utf-8")
        completed = subprocess.run(
            [
                str(python),
                str(script),
                "--data-dir",
                str(self.server.store.data_dir),
                "--question",
                question,
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}")
        lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
        if not lines:
            raise RuntimeError(completed.stdout.strip() or "LlamaIndex 没有返回 JSON")
        return json.loads(lines[-1])

    def _file(self, path: Path, root: Path | None = None) -> None:
        try:
            resolved = path.resolve()
            if root and not resolved.is_relative_to(root.resolve()):
                raise FileNotFoundError
            if not resolved.is_file():
                raise FileNotFoundError
            body = resolved.read_bytes()
        except FileNotFoundError:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if root == self.static_dir:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, body: bytes, filename: str, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", self._download_content_disposition(filename))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download_file(self, path: Path, filename: str, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", self._download_content_disposition(filename))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        try:
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            logger.info("备份下载在传输完成前被客户端取消")

    @staticmethod
    def _download_content_disposition(filename: str) -> str:
        """Build an HTTP-safe download header while retaining Chinese filenames."""
        name = Path(str(filename or "download")).name
        fallback = name.encode("ascii", "ignore").decode("ascii").replace('"', "_").replace("\\", "_") or "download"
        return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"

    def log_message(self, format: str, *args: object) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _safe_error(self, status: HTTPStatus, message: str) -> None:
        """在异常处理路径中安全地返回错误 JSON。"""
        try:
            self._json({"error": message}, status)
        except Exception:
            pass  # 连接已断开等，无法写回响应
