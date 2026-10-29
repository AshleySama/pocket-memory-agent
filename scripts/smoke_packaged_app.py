# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Exercise one packaged EXE using disposable data and an isolated Windows config.

Never opens a browser, downloads a model, or contacts an existing app instance.
An optional --llm-model GGUF is read in place; weights are never copied.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def request(base: str, path: str, payload: dict | None = None, *, timeout: float = 30, raw: bool = False):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(base + path, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        data = response.read()
    return data if raw else json.loads(data.decode("utf-8"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def source_kind(source: dict) -> str:
    """Accept the retained source contract and the legacy response spelling."""
    return source.get("source_type") or source.get("type") or ""


def wait_until(operation, predicate, *, timeout: float = 90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = operation()
        if predicate(last):
            return last
        time.sleep(0.25)
    raise TimeoutError(f"Condition not reached; last result: {last!r}")


def ocr_image() -> str:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 130), "white")
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
    font = ImageFont.truetype(str(font_path), 42) if font_path.is_file() else ImageFont.load_default(size=42)
    ImageDraw.Draw(image).text((24, 35), "POCKET MEMORY 2026", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def import_workbook(base: str, directory: Path) -> dict:
    from openpyxl import Workbook

    path = directory / "打包合同台账.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "合同台账"
    sheet.append(["合同编号", "负责人", "付款状态", "付款比例"])
    sheet.append(["EQ-C31", "林岚", "待付款", "0%"])
    sheet.append(["EQ-C310", "周远", "已付款", "80%"])
    workbook.save(path)
    workbook.close()
    document = request(base, "/api/documents", {
        "name": path.name,
        "data_url": "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,"
                    + base64.b64encode(path.read_bytes()).decode("ascii"),
    })
    imported = wait_until(
        lambda: request(base, f"/api/documents/{document['id']}/chunks"),
        lambda value: value["document"]["status"] in {"ready", "failed", "error"},
    )
    require(imported["document"]["status"] == "ready" and imported["chunks"], f"Real XLSX import failed: {imported}")
    wait_until(lambda: request(base, "/api/embedding/status"),
               lambda value: value.get("document_chunks_indexed", 0) >= len(imported["chunks"]))
    return imported


def check_table_answers(base: str, imported: dict, evidence_sink: dict | None = None, *, release_mechanics: bool = False) -> dict:
    document_id = imported["document"]["id"]
    positive = request(base, "/api/ask", {
        "question": "合同 EQ-C31 的负责人、付款状态和付款比例分别是什么？", "diagnostics": True,
    }, timeout=180)
    answer = positive.get("answer", "")
    if evidence_sink is not None:
        evidence_sink["positive"] = positive
    require("林岚" in answer and re.search(r"(?<!\d)0%", answer)
            and any(term in answer for term in ("待付款", "未付款", "尚未付款")),
            f"XLSX target row answer is incomplete: {positive}")
    require(not any(term in answer for term in ("EQ-C310", "周远", "80%")), f"Neighbouring row leaked into answer: {positive}")
    sources = [source for source in positive.get("sources", [])
               if source_kind(source) == "document" and source.get("id") == document_id]
    require(sources, "XLSX answer has no citation to the imported document")
    require(any(all(term in source.get("evidence", "") for term in ("EQ-C31", "林岚", "待付款", "0%")) for source in sources),
            "Visible XLSX evidence lost target identity or row fields")
    adjacent_context = any(term in source.get("evidence", "") for source in sources for term in ("EQ-C310", "周远", "80%"))
    if not release_mechanics:
        require(not adjacent_context, "Visible XLSX evidence includes the neighbouring contract")
    require(any(source.get("location") for source in sources), "XLSX citation has no source location")
    negative = request(base, "/api/ask", {
        "question": "合同 EQ-C31 的合同金额是多少？", "diagnostics": True,
    }, timeout=180)
    negative_text = negative.get("answer", "")
    if evidence_sink is not None:
        evidence_sink["absent_price"] = negative
    require(any(term in negative_text for term in ("未提及", "未提供", "没有", "无法", "不足", "未找到", "未记录", "未列出", "未载明", "不包含")),
            f"Missing XLSX price was not refused: {negative}")
    require(not negative.get("sources"), "Missing XLSX amount returned unrelated answer citations")
    require(not re.search(r"\d+(?:\.\d+)?\s*(?:亿元|万元|元)", negative_text), f"Unlabelled contract amount invented: {negative}")
    require(not any(term in negative_text for term in ("EQ-C310", "周远", "80%")), f"Absent price borrowed neighbouring values: {negative}")
    return {"document": imported["document"], "chunk_count": len(imported["chunks"]), "positive": positive, "absent_price": negative,
            "evidence_scope_passed": not adjacent_context,
            "known_limitations": ["Correct target answer and document citation, but displayed evidence also includes adjacent row"] if adjacent_context else []}


def check_explicit_field_answers(base: str) -> dict:
    """Check original rules, incident causes, budget history and retention safety."""
    from pocket_memory.chunking import split_note_text

    fixture_path = Path(__file__).resolve().parents[1] / "tests/fixtures/rag_eval_realistic_office_100.json"
    fixture_bytes = fixture_path.read_bytes()
    fixture = json.loads(fixture_bytes.decode("utf-8-sig"))
    cases = [case for case in fixture["cases"] if case["id"] in {"exact-001", "exact-003", "exact-011", "exact-022", "paraphrase-001", "distractor-003", "followup-006", "paraphrase-011"}]
    require(len(cases) == 8, "Business-rule smoke fixture lost its original cases")
    titles = {title for case in cases for title in case["expected_titles"]}
    documents = [document for document in fixture["documents"] if document["title"] in titles]
    require(len(documents) == 4, "Field-gate smoke fixture lost its original evidence")
    previous = request(base, "/api/embedding/status")
    created_note_ids = {}
    for document in documents:
        created = request(base, "/api/notes", {key: document[key] for key in ("title", "content", "category")})
        created_note_ids[document["title"]] = created["id"]
    expected_chunks = previous["note_chunks_indexed"] + sum(len(split_note_text(doc["content"])) for doc in documents)
    wait_until(lambda: request(base, "/api/embedding/status"),
               lambda value: value.get("indexed", 0) >= previous["indexed"] + len(documents)
               and value.get("note_chunks_indexed", 0) >= expected_chunks)
    results = {}
    case_requests = {}
    for case in cases:
        payload = {"question": case["question"], "diagnostics": True}
        if case.get("history"):
            payload["history"] = case["history"]
        case_requests[case["id"]] = payload
        response = request(base, "/api/ask", payload, timeout=180)
        results[case["id"]] = response
        answer = response.get("answer", "")
        sources = [source for source in response.get("sources", []) if source.get("title") in case["expected_titles"]]
        visible = "\n".join(source.get("evidence", "") for source in sources)
        require(sources, f"Packaged field answer has no expected source: {case['id']}: {response}")
        if case["id"] == "exact-001":
            roles = ("部门负责人", "预算负责人", "采购负责人")
            require(all(role in answer for role in roles) and "审批" in answer
                    and not any(role in answer for role in ("法务", "财务负责人")),
                    f"Packaged approval answer does not match the requested amount: {response}")
            require(re.search(r"(?<![\d.])5\s*万元\s*[（(]含[）)]\s*(?:至|到|[-—])\s*20\s*万元\s*[（(]不含[）)]", visible)
                    and all(role in visible for role in roles),
                    f"Visible approval evidence uses the wrong monetary band: {response}")
            require(all(source.get("location") for source in sources),
                    f"Approval evidence lost its source location: {response}")
        elif case["id"] == "exact-003":
            require(re.search(r"(?:不能|不可以|不得)(?:直接)?跳过审批", answer)
                    and re.search(r"(?<![\d.])(?:2\s*个?|两(?:个)?)\s*工作日", answer),
                    f"Packaged deadline answer lost the approval rule or workday unit: {response}")
            require("不能跳过审批" in visible and re.search(r"(?<![\d.])2\s*个?\s*工作日", visible),
                    f"Visible deadline evidence does not support the answer: {response}")
        elif case["id"] == "exact-011":
            require(re.search(r"(?<![\d.])10\s*%", answer) and re.search(r"(?<![\d.])24\s*小时", answer)
                    and "后再决定是否扩量" in re.sub(r"\s+", "", answer),
                    f"Packaged first-batch answer lost the ratio or observation period: {response}")
            require("第一批灰度" in visible and re.search(r"(?<![\d.])10\s*%", visible)
                    and re.search(r"(?<![\d.])24\s*小时", visible)
                    and "后再决定是否扩量" in re.sub(r"\s+", "", visible),
                    f"Visible first-batch evidence does not support the answer: {response}")
        elif case["id"] == "paraphrase-001":
            # This narrow recovery must quote the complete original low band.
            # A shared boundary number alone must never pass this inverse check.
            target = "单笔含税金额 5 万元以下，由部门负责人和预算负责人审批；"
            witness = "5 万元（含）至 20 万元（不含），由部门负责人、预算负责人及采购负责人审批；"

            def approval_text(text):
                return re.sub(r"\s+", "", text).removeprefix("【新审批规则】").strip("。；;")

            quoted = re.sub(r"(?:来源)?\[\d+\]", "", answer)
            require(approval_text(quoted) == approval_text(target),
                    f"Inverse approval answer changes the original low-band condition or roles: {response}")
            require(approval_text(visible) == approval_text(target + witness),
                    f"Inverse approval visible evidence lacks the complete adjacent rule pair: {response}")
            require(len(sources) == len(response.get("sources", [])) == 1,
                    f"Inverse approval proof is split across sources: {response}")
            source = sources[0]
            original = next(document["content"] for document in documents if document["title"] == source["title"])
            chunks = split_note_text(original)
            expected_locations = [f"正文 {i + 1}" for i, chunk in enumerate(chunks)
                                  if approval_text(target) in approval_text(chunk)
                                  and approval_text(witness) in approval_text(chunk)]
            require(len(expected_locations) == 1 and source.get("location") == expected_locations[0]
                    and source.get("evidence_locations") == expected_locations,
                    f"Inverse approval pair is not located in its actual original chunk: {response}")
            require(source.get("type") == "note" and source.get("id") == created_note_ids[source["title"]]
                    and source.get("url") == f"/api/notes/{source['id']}"
                    and {int(value) for value in re.findall(r"\[(\d+)\]", answer)} == {int(source.get("index", 1))},
                    f"Inverse approval source identity or citation is wrong: {response}")
        elif case["id"] == "paraphrase-011":
            # A source denial of immediate deletion cannot supply a new
            # retention deadline. Accept only the original supported wording.
            original_fact = "“账号停用”不等于“所有历史资料立刻删除”；"
            qualifier = "资料保存期限另按业务与合规规则执行。"

            def retention_text(text):
                return re.sub(r"\s+", "", text).removeprefix("【常见误解】").strip("。；;")

            quoted = re.sub(r"(?:来源)?\[\d+\]", "", answer)
            allowed = {retention_text(original_fact), retention_text(original_fact + qualifier)}
            require(retention_text(quoted) in allowed and retention_text(visible) in allowed,
                    f"Retention answer or evidence adds an unsupported lifetime or deletion rule: {response}")
            require(len(sources) == len(response.get("sources", [])) == 1,
                    f"Retention answer combines distinct sources: {response}")
            source = sources[0]
            original = next(document["content"] for document in documents if document["title"] == source["title"])
            expected_locations = [f"正文 {i + 1}" for i, chunk in enumerate(split_note_text(original))
                                  if retention_text(visible) in re.sub(r"\s+", "", chunk)]
            require(len(expected_locations) == 1 and source.get("location") == expected_locations[0]
                    and source.get("evidence_locations") == expected_locations,
                    f"Retention evidence lost its actual original location: {response}")
            require(source.get("type") == "note" and source.get("id") == created_note_ids[source["title"]]
                    and source.get("url") == f"/api/notes/{source['id']}"
                    and source.get("chunk_id") is None and not source.get("chunk_ids")
                    and {int(value) for value in re.findall(r"\[(\d+)\]", answer)} == {int(source.get("index", 1))},
                    f"Retention source identity or citation is wrong: {response}")
        elif case["id"] == "followup-006":
            # Keep the original elliptical question and history. Do not replace
            # the condition with an amount, a computed boundary or a permission.
            expected = "任何单渠道超预算 5% 必须暂停并向项目经理说明。"

            def budget_text(text):
                return re.sub(r"\s+", "", text).removeprefix("【预算】").strip("。；;")

            quoted = re.sub(r"(?:来源)?\[\d+\]", "", answer)
            require(budget_text(quoted) == budget_text(expected)
                    and budget_text(visible) == budget_text(expected),
                    f"Budget answer or visible passage changed the original condition or obligation: {response}")
            require(len(sources) == len(response.get("sources", [])) == 1,
                    f"Budget rule is split across sources: {response}")
            source = sources[0]
            original = next(document["content"] for document in documents if document["title"] == source["title"])
            expected_locations = [f"正文 {i + 1}" for i, chunk in enumerate(split_note_text(original))
                                  if budget_text(expected) in re.sub(r"\s+", "", chunk)]
            require(len(expected_locations) == 1 and source.get("location") == expected_locations[0]
                    and source.get("evidence_locations") == expected_locations,
                    f"Budget evidence is not in its actual original chunk: {response}")
            require(source.get("type") == "note" and source.get("id") == created_note_ids[source["title"]]
                    and source.get("url") == f"/api/notes/{source['id']}"
                    and source.get("chunk_id") is None and not source.get("chunk_ids")
                    and {int(value) for value in re.findall(r"\[(\d+)\]", answer)} == {int(source.get("index", 1))},
                    f"Budget source identity or citation is wrong: {response}")
            understanding = response.get("diagnostics", {}).get("query_understanding", {})
            require(understanding.get("history_used") is True
                    and understanding.get("mode") == "contextual_followup"
                    and response.get("conversation", {}).get("mode") == "contextual_followup",
                    f"Budget smoke did not exercise the original history resolution: {response}")
        elif case["id"] == "distractor-003":
            # Quote the mechanism and the explicit exclusion together. The
            # incident severity rationale cannot support this cause question.
            expected = ("【根因】促销峰值使消息队列积压；消费者扩容策略仅依据 CPU 使用率，"
                        "未纳入队列滞留时间。数据库不是根因，支付接口也未异常。")

            def cause_text(text):
                return re.sub(r"\s+", "", text).removeprefix("【根因】").strip("。；;")

            quoted = re.sub(r"(?:来源)?\[\d+\]", "", answer)
            require(cause_text(quoted) == cause_text(expected)
                    and cause_text(visible) == cause_text(expected),
                    f"Cause answer or visible passage lost the original mechanism or exclusion: {response}")
            require(len(sources) == len(response.get("sources", [])) == 1,
                    f"Cause proof is split across sources: {response}")
            source = sources[0]
            original = next(document["content"] for document in documents if document["title"] == source["title"])
            expected_locations = [f"正文 {i + 1}" for i, chunk in enumerate(split_note_text(original))
                                  if cause_text(expected) in re.sub(r"\s+", "", chunk)]
            require(len(expected_locations) == 1 and source.get("location") == expected_locations[0]
                    and source.get("evidence_locations") == expected_locations,
                    f"Cause evidence is not in its actual complete original chunk: {response}")
            require(source.get("type") == "note" and source.get("id") == created_note_ids[source["title"]]
                    and source.get("url") == f"/api/notes/{source['id']}"
                    and {int(value) for value in re.findall(r"\[(\d+)\]", answer)} == {int(source.get("index", 1))},
                    f"Cause source identity or citation is wrong: {response}")
        else:
            change = r"从\s*20\s*万条\s*(?:调整为|调整到|改为)\s*5\s*万条"
            require(all(re.search(change, text) and "积压" in text and "告警" in text
                        for text in (answer, visible)),
                    f"Threshold answer or evidence lost the old-to-new count relation: {response}")
            require(not re.search(r"(?:47|5)\s*分钟|拟|计划|否决|撤回|取消|待审批|建议", answer + visible),
                    f"Threshold answer mixes another quantity or an ineffective proposal: {response}")
            require(not re.search(r"(?:调整到|调整为|改为)\s*20\s*万条", answer),
                    f"Threshold answer also asserts the old count as the adjusted value: {response}")
            require(len(sources) == len(response.get("sources", [])) == 1
                    and sources[0].get("location") == "正文 1"
                    and sources[0].get("evidence_locations") == ["正文 1"],
                    f"Threshold evidence lost its actual source location: {response}")
            source = sources[0]
            require(source.get("type") == "note" and source.get("id") == created_note_ids[source["title"]]
                    and source.get("url") == f"/api/notes/{source['id']}"
                    and {int(value) for value in re.findall(r"\[(\d+)\]", answer)} == {int(source.get("index", 1))},
                    f"Threshold source identity or citation is wrong: {response}")
            # This case exercises task-scoped extractive recovery. Check the
            # complete original clause, not just the occurrence of two numbers.
            def clause_text(text):
                return re.sub(r"\s+", "", text).removeprefix("【处置】").strip("。；;")
            original = next(document["content"] for document in documents if document["title"] == source["title"])
            quoted = re.sub(r"(?:来源)?\[\d+\]", "", answer)
            require(clause_text(visible) in re.sub(r"\s+", "", original)
                    and clause_text(quoted) == clause_text(visible),
                    f"Threshold recovery changed the original subject, parameter or relation: {response}")
    return {"fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest().upper(),
            "created_note_ids": created_note_ids, "case_requests": case_requests, "cases": results}


def check_backup(data: bytes, directory: Path, note: dict, image_note: dict, document: dict | None = None) -> dict:
    restored = directory / "restored"
    restored.mkdir()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(archive.testzip() is None, "Backup ZIP CRC failed")
        names = set(archive.namelist())
        for expected in ("manifest.json", "pocket_memory.db", note["markdown_path"], image_note["attachment_path"]):
            require(expected in names, f"Backup missing {expected}")
        if document is not None:
            require(document["stored_path"] in names, "Backup missing imported XLSX file")
        for member in archive.infolist():
            require((restored / member.filename).resolve().is_relative_to(restored.resolve()), "Unsafe backup path")
        archive.extractall(restored)
        manifest = json.loads(archive.read("manifest.json"))
    with closing(sqlite3.connect((restored / "pocket_memory.db").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        restored_note = connection.execute("SELECT title, content FROM notes WHERE id = ?", (note["id"],)).fetchone()
        restored_ocr = connection.execute("SELECT ocr_text FROM notes WHERE id = ?", (image_note["id"],)).fetchone()
        require(integrity == "ok" and not foreign_keys, "Restored SQLite integrity failed")
        require(restored_note == (note["title"], note["content"]), "Restored note differs from original")
        require(restored_ocr and "2026" in restored_ocr[0], "Restored OCR evidence missing")
        if document is not None:
            restored_chunks = connection.execute("SELECT count(*) FROM document_chunks WHERE document_id = ?", (document["id"],)).fetchone()[0]
            require(restored_chunks == document["chunk_count"], "Restored XLSX chunk count differs")
    return {"zip_bytes": len(data), "entries": len(names), "manifest": manifest, "restored_integrity": integrity,
            "foreign_key_violations": len(foreign_keys), "synthetic_note_and_ocr_preserved": True,
            "imported_document_preserved": document is not None}


def run(executable: Path, output: Path, llm_model: Path | None = None, *, skip_historical_fields: bool = False, release_mechanics: bool = False) -> dict:
    executable = executable.resolve(strict=True)
    if llm_model is not None:
        llm_model = llm_model.resolve(strict=True)
        require(llm_model.is_file() and llm_model.suffix.lower() == ".gguf", "--llm-model must be an existing GGUF file")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"executable": str(executable), "llm_model": str(llm_model) if llm_model else None,
              "passed": False, "checks": {}, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    proc = None
    owned_base = None
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="pocket-memory-packaged-smoke-") as temporary:
            root = Path(temporary).resolve()
            data_dir = root / "data"
            local_appdata = root / "localappdata"
            config_path = local_appdata / "PocketMemory" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_bytes = json.dumps({"data_dir": str(data_dir), "ai_auto_apply": False,
                                       "ai_warmup_on_start": False, "llm_model_path": str(llm_model) if llm_model else None}).encode("utf-8")
            config_path.write_bytes(config_bytes)
            environment = dict(os.environ, LOCALAPPDATA=str(local_appdata))
            command = [str(executable), "--server-only", "--no-browser", "--data-dir", str(data_dir),
                       "--host", "127.0.0.1", "--port", "0"]
            report["command"] = command
            report["isolated_config"] = str(config_path)
            with output.with_suffix(".log").open("wb") as log:
                proc = subprocess.Popen(command, cwd=executable.parent, env=environment, stdout=log,
                                        stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        require(proc.poll() is None, f"Packaged process exited before readiness: {proc.returncode}")
                        lock_path = data_dir / ".pocket-memory.lock"
                        if lock_path.is_file():
                            try:
                                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                            except (ValueError, OSError):
                                time.sleep(0.25)
                                continue
                            parsed = urlparse(str(lock.get("url", "")))
                            require(lock.get("pid") == proc.pid, "Smoke lock belongs to another process")
                            require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                                    and not parsed.username and not parsed.password, "Smoke lock is not a loopback endpoint")
                            try:
                                status = request(lock["url"], "/api/status", timeout=2)
                            except OSError:
                                time.sleep(0.25)
                                continue
                            require(status.get("pid") == proc.pid, "HTTP identity differs from owned process")
                            owned_base = lock["url"]
                            report["checks"]["status"] = status
                            break
                        time.sleep(0.25)
                    require(owned_base is not None, "Packaged server readiness timed out")
                    settings = request(owned_base, "/api/settings")
                    require(Path(settings["data_dir"]).resolve() == data_dir, "App selected a non-smoke data directory")
                    require(settings.get("ai_warmup_on_start") is False and settings.get("ai_auto_apply") is False,
                            "Isolated configuration was not applied")
                    report["checks"]["settings"] = settings
                    for name in ("embedding", "ocr"):
                        runtime = request(owned_base, f"/api/{name}/status", timeout=90)
                        require(runtime.get("available") is True, f"Packaged {name} unavailable: {runtime}")
                        report["checks"][name] = runtime
                    title = "白鹭计划验收记录"
                    note = request(owned_base, "/api/notes", {"title": title, "content": "白鹭计划的负责人是林岚。白鹭计划的验收口令是松柏桥。"})
                    image_note = request(owned_base, "/api/notes", {"title": "打包 OCR 验收", "content": "仅供隔离环境验收。",
                                                                              "image_data_url": ocr_image(), "image_name": "smoke.png"})
                    image_note = wait_until(lambda: request(owned_base, f"/api/notes/{image_note['id']}"),
                                            lambda value: value.get("ocr_status") in {"done", "failed", "unavailable"})
                    require(image_note.get("ocr_status") == "done" and "2026" in image_note.get("ocr_text", ""),
                            f"Packaged OCR did not recognize synthetic text: {image_note.get('ocr_text')}")
                    report["checks"]["ocr_recognition"] = {"status": image_note["ocr_status"], "text": image_note["ocr_text"]}
                    report["checks"]["embedding_after_create"] = wait_until(
                        lambda: request(owned_base, "/api/embedding/status"),
                        lambda value: value.get("indexed", 0) >= 2 and value.get("note_chunks_indexed", 0) >= 2,
                    )
                    runtime = request(owned_base, "/api/intelligence/status")
                    if llm_model is not None:
                        models = request(owned_base, "/api/intelligence/models")
                        require(models.get("current") == str(llm_model), "External GGUF configuration was not applied")
                        require(runtime.get("available") is True, f"Configured LLM runtime unavailable: {runtime}")
                    report["checks"]["intelligence_before_ask"] = runtime
                    imported = None
                    if runtime.get("available"):
                        answer = request(owned_base, "/api/ask", {"question": "白鹭计划的验收口令是什么？", "diagnostics": True}, timeout=180)
                        require("松柏桥" in answer.get("answer", ""), f"Synthetic RAG answer is wrong: {answer}")
                        require(any(source.get("title") == title for source in answer.get("sources", [])), "Synthetic source was not cited")
                        report["checks"]["ask"] = answer
                        loaded = request(owned_base, "/api/intelligence/status")
                        require(loaded.get("model_loaded") is True, "Qwen model was not loaded by the real ask route")
                        report["checks"]["intelligence_after_ask"] = loaded
                        imported = import_workbook(owned_base, root)
                        # Preserve parsed rows and import metadata even if the
                        # following answer assertion rejects this candidate.
                        report["checks"]["xlsx_import"] = imported
                        report["checks"]["xlsx_rag"] = {}
                        report["checks"]["xlsx_rag"] = check_table_answers(owned_base, imported, report["checks"]["xlsx_rag"], release_mechanics=release_mechanics)
                        if skip_historical_fields:
                            report["checks"]["explicit_field_rag"] = {"skipped": True, "reason": "Reuse frozen RAG evidence; package mechanics smoke only"}
                        else:
                            report["checks"]["explicit_field_rag"] = check_explicit_field_answers(owned_base)
                    else:
                        report["checks"]["ask"] = {"skipped": True, "reason": "No available model; pass --llm-model for Qwen smoke"}
                    backup = request(owned_base, "/api/export/backup", raw=True, timeout=60)
                    report["checks"]["backup_restore"] = check_backup(backup, root, note, image_note,
                                                                      imported["document"] if imported else None)
                    require(config_path.read_bytes() == config_bytes, "Read-only smoke settings changed unexpectedly")
                    report["package_mechanics_passed"] = True
                    report["known_limitations"] = report["checks"].get("xlsx_rag", {}).get("known_limitations", [])
                    report["passed"] = not report["known_limitations"]
                finally:
                    # Only address the PID-owned endpoint discovered in this
                    # disposable directory. Never call the general --quit path.
                    if proc.poll() is None and owned_base is not None:
                        try:
                            if request(owned_base, "/api/status", timeout=3).get("pid") == proc.pid:
                                request(owned_base, "/api/shutdown", {}, timeout=5)
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        report["passed"] = False
                        report["shutdown_required_termination"] = True
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=10)
                    report["process_exit_code"] = proc.returncode
                    require(proc.returncode == 0, f"Packaged process exited with {proc.returncode}")
    except Exception as error:
        report["passed"] = False
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--llm-model", type=Path, help="Existing external GGUF read without copying/downloading")
    parser.add_argument("--skip-historical-fields", action="store_true", help="Reuse unchanged frozen RAG evidence; do not repeat legacy exact-layout questions")
    parser.add_argument("--release-mechanics", action="store_true", help="Continue package checks after adjacent-row evidence scope limitation; keep that quality check failed")
    args = parser.parse_args()
    report = run(args.exe, args.output, args.llm_model, skip_historical_fields=args.skip_historical_fields, release_mechanics=args.release_mechanics)
    print(json.dumps({"passed": report["passed"], "error": report.get("error"), "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
