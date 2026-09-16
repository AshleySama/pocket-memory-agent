from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pocket_memory.config import AppConfig
from pocket_memory.daemon import get_lock_path
from pocket_memory.embedding import EmbeddingService
from pocket_memory.intelligence import IntelligenceService
from pocket_memory.ocr import OcrService
from pocket_memory.rag import RagPipeline
from pocket_memory.server import PocketMemoryHandler, PocketMemoryServer, is_loopback_host
from pocket_memory.storage import NoteStore
from pocket_memory.reporting import ReportService


class UnavailableOcrEngine:
    available = False
    unavailable_reason = "测试环境未启用 OCR"


class UnavailableEmbeddingEngine:
    available = False
    unavailable_reason = "测试环境未启用语义搜索"


class UnavailableTextEngine:
    available = False
    unavailable_reason = "测试环境未启用自动整理"


class SimpleEmbeddingEngine:
    available = True
    unavailable_reason = ""

    def embed(self, text: str):
        import numpy as np

        if "供应商" in text or "风险" in text:
            return np.asarray([1, 0, 0], dtype=np.float32)
        return np.asarray([0, 1, 0], dtype=np.float32)


class AnswerTextEngine(UnavailableTextEngine):
    available = True
    unavailable_reason = ""

    def answer(self, question: str, contexts: list[dict]) -> str:
        return f"根据笔记，供应商存在风险线索。[1]"


class EchoContextTextEngine(UnavailableTextEngine):
    available = True
    unavailable_reason = ""

    def answer(self, question: str, contexts: list[dict]) -> str:
        return contexts[0]["text"]


class InsufficientAnswerTextEngine(UnavailableTextEngine):
    available = True
    unavailable_reason = ""

    def answer(self, question: str, contexts: list[dict]) -> str:
        return "现有笔记里没有足够信息"


class ReportTextEngine(UnavailableTextEngine):
    available = True
    unavailable_reason = ""

    def generate_report_claims(self, title, template, evidence_blocks):
        return [
            {
                "topic": "项目事实",
                "claim": f"资料说明：{item['text'][:36]}",
                "quote": item["text"][:100],
                "evidence": [item["key"]],
            }
            for item in evidence_blocks[:3]
        ]

    def generate_report_structure(self, title, template, claims):
        return {
            "summary": {"text": "所选资料说明了本地知识库试点的建设目标，以及已经完成的导入、来源定位和验证范围等核心能力。", "claim_ids": [item["id"] for item in claims[:2]]},
            "sections": [
                {"title": "试点目标", "intro": "资料描述了试点范围、预期目标和需要完成的本地知识库核心能力。", "claim_ids": [claims[0]["id"]]},
                {"title": "当前进展", "intro": "资料记录了已完成的导入和引用定位能力，并说明了当前已验证的功能范围。", "claim_ids": [claims[1]["id"]]},
            ],
        }


class FakeModelDownloadService:
    def __init__(self) -> None:
        self.started = False
        self.cancelled = False

    def status(self) -> dict:
        return {
            "id": "qwen3-4b-instruct-2507-q4-k-m",
            "state": "downloading" if self.started and not self.cancelled else "idle",
            "installed": False,
            "can_cancel": self.started and not self.cancelled,
            "expected_size": 123,
            "downloaded_bytes": 10 if self.started else 0,
            "total_bytes": 123,
            "error": "",
        }

    def start(self) -> dict:
        self.started = True
        return self.status()

    def cancel(self) -> dict:
        self.cancelled = True
        return self.status()


class PocketMemoryServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = NoteStore(root / "data")
        self.config = AppConfig(root / "config.json", self.store.data_dir)
        self.ocr_service = OcrService(self.store, UnavailableOcrEngine())
        self.embedding_service = EmbeddingService(self.store, UnavailableEmbeddingEngine())
        self.intelligence_service = IntelligenceService(self.store, UnavailableTextEngine())
        self.server = PocketMemoryServer(
            ("127.0.0.1", 0),
            self.store,
            self.config,
            lambda initial_dir: None,
            self.ocr_service,
            self.embedding_service,
            self.intelligence_service,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read())

    def request_error(self, path: str, method: str = "GET", payload: object | None = None) -> tuple[int, object]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        return raised.exception.code, json.loads(raised.exception.read())

    def test_note_api_lifecycle_and_short_chinese_search(self) -> None:
        status, note = self.request(
            "/api/notes",
            "POST",
            {"title": "采购风险", "content": "供应商异常监控方案"},
        )
        self.assertEqual(status, 201)

        _, matches = self.request("/api/search?q=%E5%95%86%E5%BC%82")
        self.assertEqual([item["id"] for item in matches], [note["id"]])

        _, updated = self.request(
            f"/api/notes/{note['id']}",
            "PUT",
            {"title": "采购风控", "content": "PR PO GR IR 异常检查"},
        )
        self.assertEqual(updated["title"], "采购风控")

        _, deleted = self.request(f"/api/notes/{note['id']}", "DELETE")
        self.assertEqual(deleted, {"deleted": note["id"]})
        _, notes = self.request("/api/notes")
        self.assertEqual(notes, [])

    def test_frontend_is_served(self) -> None:
        with urlopen(self.base_url + "/") as response:
            html = response.read().decode("utf-8")
        self.assertIn("Pocket Memory", html)
        self.assertIn("/app.js", html)
        self.assertIn("笔记编辑", html)
        self.assertNotIn('data-result-scope="web"', html)

    def test_status_reports_the_running_app_version(self) -> None:
        from pocket_memory.version import APP_VERSION

        status, payload = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["app_version"], APP_VERSION)

    def test_ask_reports_a_recoverable_missing_model_state(self) -> None:
        self.embedding_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())

        status, payload = self.request_error("/api/ask", "POST", {"question": "测试智能问答"})

        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "model_runtime_unavailable")
        self.assertIn("model_download", payload)

    def test_clipboard_image_api_copies_attachment(self) -> None:
        image = base64.b64encode(b"fake-png").decode("ascii")
        _, note = self.request(
            "/api/notes",
            "POST",
            {
                "title": "粘贴截图",
                "content": "稍后接入 OCR",
                "image_data_url": f"data:image/png;base64,{image}",
                "image_name": "clipboard.png",
            },
        )
        self.assertEqual(note["source_type"], "image")
        self.assertTrue((self.store.data_dir / note["attachment_path"]).exists())
        self.assertEqual(note["ocr_status"], "unavailable")
        status, retried = self.request(f"/api/notes/{note['id']}/ocr", "POST", {})
        self.assertEqual(status, 202)
        self.assertEqual(retried["ocr_status"], "unavailable")

    def test_invalid_image_and_json_return_client_errors(self) -> None:
        status, body = self.request_error(
            "/api/notes",
            "POST",
            {"title": "坏图片", "content": "x", "image_data_url": "data:image/png;base64,%%%"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "无效的图片数据")

        status, body = self.request_error("/api/notes", "POST", ["not", "an", "object"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "JSON 请求体必须是对象")

    def test_report_api_uses_only_selected_sources_and_keeps_local_graph_available(self) -> None:
        self.server.report_service = ReportService(self.store, ReportTextEngine())
        _, first = self.request("/api/notes", "POST", {"title": "项目资料", "content": "项目目标是完成本地知识库试点。"})
        _, second = self.request("/api/notes", "POST", {"title": "项目进展", "content": "已完成导入和引用定位。"})
        status, report = self.request(
            "/api/reports/draft",
            "POST",
            {"title": "知识库试点", "template": "project", "sources": [{"kind": "note", "id": first["id"]}, {"kind": "note", "id": second["id"]}]},
        )
        self.assertEqual(status, 201)
        self.assertEqual(report["template"], "project")
        self.assertEqual(len(report["sources"]), 2)
        self.assertTrue(report["sections"])
        status, _ = self.request_error("/api/wiki/cards")
        graph_status, graph = self.request("/api/graph")
        self.assertEqual(status, 404)
        self.assertEqual(graph_status, 200)
        self.assertEqual({node["noteId"] for node in graph["nodes"] if node["type"] == "note"}, {first["id"], second["id"]})

    def test_welcome_note_can_be_restored_without_touching_existing_notes(self) -> None:
        _, note = self.request("/api/notes", "POST", {"title": "现有资料", "content": "保持不变"})

        status, welcome = self.request("/api/welcome/restore", "POST", {})

        self.assertEqual(status, 201)
        self.assertEqual(welcome["source_type"], "welcome")
        _, notes = self.request("/api/notes")
        self.assertEqual({item["id"] for item in notes}, {note["id"], welcome["id"]})

    def test_document_import_api_persists_file_and_reports_ready(self) -> None:
        encoded = base64.b64encode("采购阈值是 50 万元。".encode("utf-8")).decode("ascii")
        status, document = self.request(
            "/api/documents",
            "POST",
            {"name": "采购说明.txt", "data_url": f"data:text/plain;base64,{encoded}", "chunk_profile": "context"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(document["file_type"], "txt")
        self.assertEqual(document["chunk_profile"], "context")
        for _ in range(30):
            _, documents = self.request("/api/documents")
            current = next(item for item in documents if item["id"] == document["id"])
            if current["status"] in {"ready", "failed"}:
                break
            time.sleep(0.05)
        self.assertEqual(current["status"], "ready")
        self.assertGreaterEqual(current["chunk_count"], 1)
        status, indexed = self.request(f"/api/documents/{document['id']}/chunks")
        self.assertEqual(status, 200)
        self.assertEqual(indexed["document"]["id"], document["id"])
        self.assertEqual(indexed["document"]["chunk_profile"], "context")
        self.assertEqual(indexed["adjustment_count"], 0)
        self.assertIn("采购阈值", indexed["chunks"][0]["text"])
        self.assertEqual(indexed["chunks"][0]["location"], "正文")
        self.assertEqual(indexed["chunks"][0]["source_start_order"], 0)
        self.assertEqual(indexed["chunks"][0]["source_end_order"], 0)
        with urlopen(self.base_url + f"/api/documents/{document['id']}/file") as response:
            self.assertIn("50", response.read().decode("utf-8"))
        status, result = self.request(f"/api/documents/{document['id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], document["id"])

    def test_document_replace_api_keeps_document_id_and_rebuilds_chunks(self) -> None:
        initial = base64.b64encode("旧版阈值为 10 万元。".encode("utf-8")).decode("ascii")
        _, document = self.request(
            "/api/documents", "POST", {"name": "制度.txt", "data_url": f"data:text/plain;base64,{initial}"}
        )
        replacement = base64.b64encode("新版阈值为 50 万元。".encode("utf-8")).decode("ascii")
        status, updated = self.request(
            f"/api/documents/{document['id']}/file",
            "PUT",
            {"name": "制度更新版.txt", "data_url": f"data:text/plain;base64,{replacement}"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(updated["id"], document["id"])
        for _ in range(30):
            current = self.store.get_document(document["id"])
            if current.status in {"ready", "failed"}:
                break
            time.sleep(0.05)
        self.assertEqual(current.status, "ready")
        chunks = self.store.list_document_chunks(document["id"])
        self.assertTrue(any("新版阈值" in chunk.text for chunk in chunks))
        self.assertFalse(any("旧版阈值" in chunk.text for chunk in chunks))

    def test_ask_api_returns_document_location_and_file_url(self) -> None:
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.rag_pipeline = RagPipeline(self.store, self.server.embedding_service)
        self.server.intelligence_service = IntelligenceService(self.store, EchoContextTextEngine())
        encoded = base64.b64encode("供应商风险阈值为 50 万元，需要升级审批。".encode("utf-8")).decode("ascii")
        _, document = self.request(
            "/api/documents",
            "POST",
            {"name": "供应商制度.txt", "data_url": f"data:text/plain;base64,{encoded}"},
        )
        for _ in range(30):
            current = self.store.get_document(document["id"])
            if current.status in {"ready", "failed"}:
                break
            time.sleep(0.05)
        self.server.embedding_service._process_document(document["id"])

        status, payload = self.request("/api/ask", "POST", {"question": "供应商风险阈值是多少"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["sources"][0]["type"], "document")
        self.assertEqual(payload["sources"][0]["location"], "正文")
        source = payload["sources"][0]
        self.assertIsInstance(source["chunk_id"], int)
        self.assertEqual(source["chunk_order"], 0)
        self.assertEqual(source["url"], f"/api/documents/{document['id']}/file?chunk_id={source['chunk_id']}")

    def test_document_download_uses_utf8_filename_header(self) -> None:
        encoded = base64.b64encode(b"xlsx-bytes").decode("ascii")
        _, document = self.request(
            "/api/documents",
            "POST",
            {"name": "预算明细.xlsx", "data_url": f"data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{encoded}"},
        )

        with urlopen(self.base_url + f"/api/documents/{document['id']}/file?download=1") as response:
            self.assertEqual(response.status, 200)
            disposition = response.headers["Content-Disposition"]
            self.assertIn("attachment;", disposition)
            self.assertIn("filename*=UTF-8''", disposition)
            self.assertIn("%E9%A2%84%E7%AE%97", disposition)
            self.assertEqual(response.read(), b"xlsx-bytes")

    def test_missing_note_returns_not_found(self) -> None:
        status, body = self.request_error("/api/notes/99999", "PUT", {"title": "不存在"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "资源不存在")

    def test_complete_backup_api_contains_attachment_and_database(self) -> None:
        image = base64.b64encode(b"backup-image").decode("ascii")
        self.request(
            "/api/notes",
            "POST",
            {"title": "备份测试", "content": "保留附件", "image_data_url": f"data:image/png;base64,{image}"},
        )
        with urlopen(self.base_url + "/api/export/backup") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/zip")
            self.assertIn("attachment;", response.headers["Content-Disposition"])
            body = response.read()

        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIn("pocket_memory.db", archive.namelist())
            self.assertTrue(any(name.startswith("attachments/images/") for name in archive.namelist()))

    def test_json_export_is_downloadable_and_contains_all_notes(self) -> None:
        _, created = self.request("/api/notes", "POST", {"title": "JSON 导出", "content": "全部笔记"})

        with urlopen(self.base_url + "/api/export") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertIn("attachment;", response.headers["Content-Disposition"])
            exported = json.loads(response.read())

        self.assertEqual(exported["count"], 1)
        self.assertEqual(exported["notes"][0]["id"], created["id"])

    def test_server_only_allows_loopback_hosts(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("0.0.0.0"))

    def test_data_migration_moves_server_lock_and_tool_registry(self) -> None:
        _, note = self.request("/api/notes", "POST", {"title": "迁移", "content": "迁移后可读取"})
        old_data_dir = self.store.data_dir
        target = Path(self.temp.name) / "migrated-data"

        status, result = self.request("/api/settings/migrate", "POST", {"data_dir": str(target)})

        self.assertEqual(status, 200)
        self.assertEqual(Path(result["data_dir"]), target.resolve())
        self.assertFalse(get_lock_path(old_data_dir).exists())
        self.assertTrue(get_lock_path(target).exists())
        self.assertEqual(self.server.tool_registry.data_dir, target.resolve())
        _, notes = self.request("/api/notes")
        self.assertEqual([item["id"] for item in notes], [note["id"]])

    def test_data_directory_switch_reopens_existing_data_without_copying_current_notes(self) -> None:
        _, original = self.request("/api/notes", "POST", {"title": "原目录", "content": "不应复制到切换目录"})
        old_data_dir = self.store.data_dir
        target = Path(self.temp.name) / "existing-data"
        target_store = NoteStore(target)
        target_note = target_store.create_note("另一套资料", "只在目标数据目录中存在")
        target_store.close()

        status, result = self.request("/api/settings/switch-data-dir", "POST", {"data_dir": str(target)})

        self.assertEqual(status, 200)
        self.assertEqual(Path(result["data_dir"]), target.resolve())
        self.assertEqual(self.config.data_dir, target.resolve())
        self.assertFalse(get_lock_path(old_data_dir).exists())
        self.assertTrue(get_lock_path(target).exists())
        self.assertEqual(self.server.tool_registry.data_dir, target.resolve())
        _, notes = self.request("/api/notes")
        self.assertEqual([item["id"] for item in notes], [target_note.id])
        self.assertTrue((old_data_dir / "pocket_memory.db").is_file())
        self.assertEqual(self.store.get_note(original["id"]).title, "另一套资料")

    def test_ocr_status_api(self) -> None:
        _, status = self.request("/api/ocr/status")
        self.assertEqual(status, {"available": False, "reason": "测试环境未启用 OCR"})

    def test_embedding_status_api(self) -> None:
        _, status = self.request("/api/embedding/status")
        self.assertEqual(
            status,
            {
                "available": False,
                "reason": "测试环境未启用语义搜索",
                "indexed": 0,
                "total": 0,
                "note_chunks_indexed": 0,
                "document_chunks_indexed": 0,
            },
        )

    def test_intelligence_status_api(self) -> None:
        _, status = self.request("/api/intelligence/status")
        self.assertEqual(status, {"available": False, "reason": "测试环境未启用自动整理", "pending": 0})

    def test_disabling_auto_organisation_skips_new_note_ai_status(self) -> None:
        _, setting = self.request("/api/settings/ai-mode", "POST", {"auto_apply": False})
        self.assertFalse(setting["ai_auto_apply"])

        status, note = self.request(
            "/api/notes", "POST", {"title": "手工整理", "content": "不应自动调用模型"}
        )

        self.assertEqual(status, 201)
        self.assertEqual(note["ai_status"], "not_requested")
        self.assertEqual(self.store.get_note(note["id"]).ai_status, "not_requested")

    def test_taxonomy_and_filters_api(self) -> None:
        _, note = self.request(
            "/api/notes",
            "POST",
            {
                "title": "SQL 备忘",
                "content": "SELECT supplier_id FROM purchase_orders",
                "category": "技术",
                "subcategory": "SQL",
                "tags": ["采购", "查询"],
            },
        )
        _, filtered = self.request("/api/notes?category=%E6%8A%80%E6%9C%AF&subcategory=SQL")
        self.assertEqual([item["id"] for item in filtered], [note["id"]])
        _, tagged = self.request("/api/notes?tag=%E9%87%87%E8%B4%AD")
        self.assertEqual([item["id"] for item in tagged], [note["id"]])
        _, taxonomy = self.request("/api/taxonomy")
        self.assertEqual(taxonomy["categories"][0]["name"], "技术")
        self.assertEqual({tag["name"] for tag in taxonomy["tags"]}, {"采购", "查询"})

    def test_tag_governance_api_can_merge_reviewed_tags(self) -> None:
        _, first = self.request("/api/notes", "POST", {"content": "项目复盘", "tags": ["项目管理"]})
        self.request("/api/notes", "POST", {"content": "项目计划", "tags": ["项目 管理"]})

        _, governance = self.request("/api/tags/governance")
        self.assertEqual(governance["total"], 2)
        _, merged = self.request("/api/tags/merge", "POST", {"from": "项目 管理", "to": "项目管理"})
        self.assertEqual(merged["affected_notes"], 1)
        _, note = self.request(f"/api/notes/{first['id']}")
        self.assertEqual(note["tags"], ["项目管理"])

    def test_remove_tag_api_removes_the_tag_from_every_tab(self) -> None:
        parent = self.store.create_note("项目复盘", "主页", tags=["采购", "风险"])
        child = self.store.create_note(
            "风险页", "页签内容", parent_note_id=parent.id, tab_name="风险"
        )

        status, payload = self.request(
            f"/api/notes/{child.id}/tags/remove", "POST", {"tag": "风险"}
        )

        self.assertEqual(status, 200)
        self.assertNotIn("风险", payload["tags"])
        self.assertNotIn("风险", self.store.get_note(parent.id).tags)
        self.assertEqual(self.store.excluded_group_tags(parent.id), ["风险"])

    def test_favorite_and_reorder_note_apis(self) -> None:
        first = self.store.create_note("第一篇", "内容")
        second = self.store.create_note("第二篇", "内容")
        third = self.store.create_note("第三篇", "内容")

        status, favorite = self.request(f"/api/notes/{first.id}/favorite", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(favorite["favorite"], 1)
        _, favorites = self.request("/api/notes?favorite=1")
        self.assertEqual([note["id"] for note in favorites], [first.id])

        status, payload = self.request(
            "/api/notes/reorder", "POST", {"note_id": first.id, "target_note_id": second.id, "after": False}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        _, notes = self.request("/api/notes")
        self.assertEqual([note["id"] for note in notes], [third.id, first.id, second.id])

        welcome = self.store.create_note("欢迎", "系统说明")
        self.store.connection.execute("UPDATE notes SET source_type = 'welcome' WHERE id = ?", (welcome.id,))
        status, payload = self.request_error(
            "/api/notes/reorder", "POST", {"note_id": second.id, "target_note_id": welcome.id, "after": False}
        )
        self.assertEqual(status, 400)
        self.assertIn("当前", payload["error"])

    def test_ask_api_returns_answer_and_sources(self) -> None:
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.intelligence_service = IntelligenceService(self.store, AnswerTextEngine())
        note = self.store.create_note("供应商风险", "需要复核供应商异常交易")
        self.server.embedding_service._process(note.id)

        status, payload = self.request("/api/ask", "POST", {"question": "供应商有没有风险"})

        self.assertEqual(status, 200)
        self.assertIn("供应商", payload["answer"])
        self.assertEqual(payload["sources"][0]["id"], note.id)
        self.assertIn("score", payload["sources"][0])
        self.assertIn("timing", payload)
        self.assertIn("retrieval_seconds", payload["timing"])
        self.assertIn("total_seconds", payload["timing"])

    def test_plain_note_count_uses_deterministic_stats_not_rag(self) -> None:
        self.store.create_note("第一条", "内容一")
        self.store.create_note("第二条", "内容二")

        status, payload = self.request("/api/ask", "POST", {"question": "一共 几个笔记"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["kind"], "stats")
        self.assertIn("你共有 2 条笔记", payload["answer"])
        self.assertEqual(payload["sources"], [])

    def test_concept_question_never_falls_back_to_notebook_statistics(self) -> None:
        """Only explicit personal-library aggregations may bypass RAG."""
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.intelligence_service = IntelligenceService(self.store, EchoContextTextEngine())
        note = self.store.create_note(
            "AI 安全术语",
            "提示词注入是指攻击者通过恶意指令干扰模型原有任务或诱导其泄露不应输出的信息。",
        )
        self.server.embedding_service._process(note.id)

        for question in ("提示词注入是什么？", "提示词注入是啥", "提示词注入啥意思？"):
            with self.subTest(question=question):
                status, payload = self.request("/api/ask", "POST", {"question": question})
                self.assertEqual(status, 200)
                self.assertEqual(payload["kind"], "qa")
                self.assertIn("提示词注入是指", payload["answer"])
                self.assertEqual(payload["sources"][0]["id"], note.id)

        # The retrieval query may include a previous turn, but that previous
        # dashboard request must not decide the current turn's intent.
        status, payload = self.request(
            "/api/ask",
            "POST",
            {
                "question": "提示词注入",
                "history": [
                    {"role": "user", "content": "我的笔记有多少条？"},
                    {"role": "assistant", "content": "你共有 1 条笔记。"},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["kind"], "qa")
        self.assertIn("提示词注入是指", payload["answer"])
        self.assertEqual(payload["sources"][0]["id"], note.id)

    def test_definition_question_rejects_a_partial_topic_even_with_unrelated_history(self) -> None:
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.rag_pipeline = RagPipeline(self.store, self.server.embedding_service)
        self.server.intelligence_service = IntelligenceService(self.store, EchoContextTextEngine())
        partial = self.store.create_note("Prompt 编写", "演示时可以把提示词粘贴到 Chat 框。")
        self.server.embedding_service._process(partial.id)

        status, payload = self.request(
            "/api/ask",
            "POST",
            {
                "question": "提示词",
                "history": [
                    {"role": "user", "content": "我的笔记有多少条？"},
                    {"role": "assistant", "content": "你共有 1 条笔记。"},
                ],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["kind"], "qa")
        self.assertIn("没有找到能直接解释「提示词」的笔记证据", payload["answer"])
        self.assertEqual(payload["sources"], [])

    def test_ask_api_uses_relevant_chunks_not_full_note_prefix(self) -> None:
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.intelligence_service = IntelligenceService(self.store, EchoContextTextEngine())
        note = self.store.create_note(
            "李白诗词",
            "无关开头内容，介绍很多背景。\n\n《赠汪伦》李白\n桃花潭水深千尺，不及汪伦送我情。\n这首诗表达朋友送别的深厚情谊。",
        )
        self.server.embedding_service._process(note.id)

        status, payload = self.request("/api/ask", "POST", {"question": "桃花潭水表达什么情感"})

        self.assertEqual(status, 200)
        self.assertIn("桃花潭水", payload["answer"])
        self.assertNotIn("无关开头内容", payload["answer"])
        self.assertIn("url", payload["sources"][0])
        self.assertIn("matched_terms", payload["sources"][0])

    def test_semantic_search_returns_relevant_excerpt_and_source_link(self) -> None:
        self.embedding_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        note = self.store.create_note(
            "诗词知识",
            "前置说明不应该作为搜索摘要。\n\n《春夜喜雨》杜甫\n好雨知时节，当春乃发生。\n随风潜入夜，润物细无声。",
        )
        self.server.embedding_service._process(note.id)

        _, payload = self.request("/api/search?q=%E6%B6%A6%E7%89%A9%E7%BB%86%E6%97%A0%E5%A3%B0&mode=semantic")

        self.assertEqual(payload[0]["id"], note.id)
        self.assertIn("润物细无声", payload[0]["matched_excerpt"])
        self.assertNotIn("前置说明", payload[0]["matched_excerpt"])
        self.assertIn("source_url", payload[0])

    def test_hybrid_search_requires_a_complete_topic_and_returns_chunk_evidence(self) -> None:
        self.embedding_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.rag_pipeline = RagPipeline(self.store, self.server.embedding_service)
        partial = self.store.create_note("提示词使用", "演示时可以把提示词粘贴到 Chat 框。")
        self.server.embedding_service._process(partial.id)

        _, empty = self.request("/api/search?q=%E6%8F%90%E7%A4%BA%E8%AF%8D%E6%B3%A8%E5%85%A5&mode=hybrid")
        self.assertEqual(empty, [])

        exact = self.store.create_note(
            "提示词注入防护",
            "提示词注入会诱导模型忽略原有指令，应限制不可信文本的指令权限。",
        )
        self.server.embedding_service._process(exact.id)

        _, payload = self.request("/api/search?q=%E6%8F%90%E7%A4%BA%E8%AF%8D%E6%B3%A8%E5%85%A5&mode=hybrid")
        self.assertEqual(payload[0]["id"], exact.id)
        self.assertIn("提示词注入", payload[0]["matched_excerpt"])
        self.assertEqual(payload[0]["source_location"], "正文 1")

    def test_ask_uses_cited_evidence_fallback_for_empty_llm_answer(self) -> None:
        """模型误拒答但已有检索证据时，API 必须回退到可追溯关键句。"""
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.intelligence_service = IntelligenceService(self.store, InsufficientAnswerTextEngine())
        note = self.store.create_note(
            "《静夜思》李白",
            "《静夜思》李白\n床前明月光，疑是地上霜。\n举头望明月，低头思故乡。\n释义：这首诗表达游子思乡之情。",
        )
        self.server.embedding_service._process(note.id)

        _, payload = self.request(
            "/api/ask",
            "POST",
            {"question": "李白《静夜思》完整诗句是什么？"},
        )

        self.assertIn("床前明月光", payload["answer"])
        self.assertIn("[1]", payload["answer"])

    def test_current_question_ignores_prior_conversation_for_retrieval(self) -> None:
        """A complete new question must never borrow previous-turn retrieval terms."""
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.rag_pipeline = RagPipeline(self.store, self.server.embedding_service)
        self.server.intelligence_service = IntelligenceService(self.store, EchoContextTextEngine())
        role_note = self.store.create_note(
            "工业互联网与人工智能",
            "工业互联网承担连接设备与数据基础的角色；人工智能负责从已有信息中发现模式、生成摘要并提出建议。",
        )
        risk_note = self.store.create_note(
            "项目复盘",
            "风险一：导入资料主题过杂，会让检索命中很多相似词，却缺少能直接回答问题的证据。\n"
            "风险二：原文难以定位，用户无法核对答案来源。",
        )
        self.server.embedding_service._process(role_note.id)
        self.server.embedding_service._process(risk_note.id)
        history = [
            {"role": "user", "content": "工业互联网和人工智能各自承担什么角色？"},
            {"role": "assistant", "content": "工业互联网负责连接，人工智能负责分析与生成。"},
        ]
        _, payload = self.request(
            "/api/ask", "POST", {"question": "项目有哪些风险", "history": history}
        )
        self.assertEqual(payload["sources"][0]["id"], risk_note.id)
        self.assertIn("风险一", payload["answer"])
        self.assertNotIn("工业互联网", payload["answer"])
        self.assertEqual(payload["conversation"]["mode"], "single_turn")

    def test_ambiguous_operation_question_does_not_answer_from_an_unrelated_history(self) -> None:
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.rag_pipeline = RagPipeline(self.store, self.server.embedding_service)
        self.server.intelligence_service = IntelligenceService(self.store, EchoContextTextEngine())
        note = self.store.create_note("采购规则", "PO订单上传合同前需要完成审批。")
        self.server.embedding_service._process(note.id)

        for question, expected in (("为什么不能回退", "回退的对象或功能"), ("为什么不能删除", "删除的对象或功能")):
            with self.subTest(question=question):
                _, payload = self.request(
                    "/api/ask",
                    "POST",
                    {
                        "question": question,
                        "history": [
                            {"role": "user", "content": "PO订单是否需要上传合同？"},
                            {"role": "assistant", "content": "需要完成审批。"},
                        ],
                    },
                )

                self.assertEqual(payload["kind"], "clarification")
                self.assertIn(expected, payload["answer"])
                self.assertEqual(payload["sources"], [])
                self.assertEqual(payload["conversation"]["mode"], "single_turn")

    def test_default_model_download_api_exposes_only_the_approved_download(self) -> None:
        downloader = FakeModelDownloadService()
        self.server.model_download_service = downloader

        _, before = self.request("/api/models/default-download")
        self.assertEqual(before["state"], "idle")
        status, started = self.request("/api/models/default-download", "POST", {})
        self.assertEqual(status, 202)
        self.assertTrue(started["can_cancel"])
        _, cancelled = self.request("/api/models/default-download/cancel", "POST", {})
        self.assertFalse(cancelled["can_cancel"])

    def test_source_url_serves_url_encoded_markdown_path(self) -> None:
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.intelligence_service = IntelligenceService(self.store, AnswerTextEngine())
        note = self.store.create_note("《静夜思》李白", "《静夜思》\n床前明月光，疑是地上霜。")
        self.server.embedding_service._process(note.id)

        _, payload = self.request("/api/ask", "POST", {"question": "床前明月光"})
        source_url = payload["sources"][0]["url"]

        with urlopen(self.base_url + source_url) as response:
            markdown = response.read().decode("utf-8")

        self.assertIn("床前明月光", markdown)

    def test_source_title_uses_matched_segment_title(self) -> None:
        self.embedding_service.shutdown()
        self.intelligence_service.shutdown()
        self.server.embedding_service = EmbeddingService(self.store, SimpleEmbeddingEngine())
        self.server.intelligence_service = IntelligenceService(self.store, AnswerTextEngine())
        note = self.store.create_note(
            "《静夜思》李白",
            "《静夜思》李白\n床前明月光，疑是地上霜。\n\n《望庐山瀑布》李白\n飞流直下三千尺，疑是银河落九天。\n释义：夸张手法描写瀑布壮阔。",
        )
        self.server.embedding_service._process(note.id)

        _, payload = self.request("/api/ask", "POST", {"question": "望庐山瀑布运用了什么修辞手法"})

        self.assertEqual(payload["sources"][0]["title"], "《望庐山瀑布》李白")
        self.assertIn("夸张手法", payload["sources"][0]["evidence"])


if __name__ == "__main__":
    unittest.main()
