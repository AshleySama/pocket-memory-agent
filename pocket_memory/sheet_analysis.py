# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Local, row-grained XLSX analysis. Arithmetic never calls a language model.

Imported workbooks and calculated reports are immutable snapshots; title/notes
are the only editable report fields. Formula caches are not trusted as values.
"""
from __future__ import annotations

import base64
import binascii
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
from html import escape
import io
import json
from pathlib import Path
import re
import threading
import time
import uuid
import zipfile

LIMITS = {"upload_bytes": 8 * 1024 * 1024, "expanded_bytes": 40 * 1024 * 1024,
          "sheets": 20, "rows": 10000, "columns": 64, "cells": 200000,
          "text_chars": 8000000}


def now():
    return datetime.now().isoformat(timespec="seconds")


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
    temp.replace(path)


def safe_id(value):
    value = str(value)
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("无效的本地记录编号")
    return value


def text(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def parse_date(cell):
    raw = cell["value"].strip()
    if cell.get("formula"):
        return {"raw": raw, "parsed": None, "issue": "公式未核算"}
    if not raw:
        return {"raw": raw, "parsed": None, "issue": "未填写"}
    value = raw[:10] if cell["type"] == "date" else raw
    match = re.fullmatch(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", value)
    if match:
        try:
            parsed = date(*map(int, match.groups())).isoformat()
            return {"raw": raw, "parsed": parsed, "issue": ""}
        except ValueError:
            pass
    return {"raw": raw, "parsed": None, "issue": "不是可确认的绝对日期"}


def decimal_value(cell):
    if cell.get("formula") or cell["type"] in ("error", "bool", "date"):
        return None
    value = cell["value"].strip()
    # Reject thousands/decimal locale ambiguity, units and percent strings.
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        return None
    try:
        result = Decimal(value)
        if result.is_finite() and abs(result.adjusted()) <= 30:
            return result
    except InvalidOperation:
        pass
    return None


def grouped(rows, field):
    groups = defaultdict(list)
    for row in rows:
        groups[row[field] or "未填写"].append(row["row"])
    return [{"label": label, "count": len(numbers), "rows": numbers}
            for label, numbers in sorted(groups.items(), key=lambda p: (-len(p[1]), p[0]))]


class SheetAnalysis:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "table-analysis"
        self.datasets = self.root / "datasets"
        self.reports = self.root / "reports"
        self.datasets.mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def list(self):
        with self.lock:
            datasets = [json.loads(p.read_text("utf-8")) for p in self.datasets.glob("*.json")]
            reports = []
            for path in self.reports.glob("*.json"):
                if path.name.endswith('.meta.json'):
                    continue
                meta = path.with_suffix('.meta.json')
                if not meta.exists():
                    report = json.loads(path.read_text("utf-8"))
                    write_json(meta, {k: report[k] for k in ("id", "title", "created", "version")})
                reports.append(json.loads(meta.read_text('utf-8')))
            return {"datasets": sorted(datasets, key=lambda d: d["created"], reverse=True),
                    "reports": sorted(reports, key=lambda d: d["created"], reverse=True), "limits": LIMITS}

    def dataset(self, dataset_id):
        path = self.datasets / (safe_id(dataset_id) + ".json")
        if not path.exists():
            raise KeyError(dataset_id)
        return json.loads(path.read_text("utf-8"))

    def file(self, dataset_id):
        dataset = self.dataset(dataset_id)
        path = self.datasets / (dataset["id"] + ".xlsx")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != dataset["sha256"]:
            raise ValueError("原始工作簿副本已改变或丢失，请重新导入；不会使用失效来源计算")
        return dataset, path

    def import_file(self, body):
        from openpyxl import load_workbook
        filename = str(body.get("filename", "")).replace("\\", "/").split("/")[-1]
        if not filename.lower().endswith(".xlsx") or len(filename) > 200:
            raise ValueError("本轮支持 .xlsx 工作簿，请先在 Excel 另存为该格式")
        data_url = str(body.get("data_url", ""))
        if len(data_url) > LIMITS["upload_bytes"] * 4 // 3 + 300 or ";base64," not in data_url:
            raise ValueError("请选择不超过 8 MiB 的 XLSX 工作簿")
        try:
            payload = base64.b64decode(data_url.split(";base64,", 1)[1], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("文件编码无效") from None
        if len(payload) > LIMITS["upload_bytes"]:
            raise ValueError("工作簿超过 8 MiB")
        with self.lock:
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    entries = archive.infolist()
                    if len(entries) > 5000 or sum(i.file_size for i in entries) > LIMITS["expanded_bytes"]:
                        raise ValueError("解压后的工作簿过大，请拆分工作表后导入")
                    if any(i.flag_bits & 1 for i in entries):
                        raise ValueError("不支持加密工作簿")
                    if any(i.filename.lower().endswith("vbaproject.bin") for i in entries):
                        raise ValueError("不支持包含宏的工作簿")
                book = load_workbook(io.BytesIO(payload), read_only=True, data_only=False, keep_links=False)
                try:
                    if len(book.worksheets) > LIMITS["sheets"]:
                        raise ValueError("工作表超过 20 个，请拆分后导入")
                    sheets = [{"name": sheet.title, "max_row": sheet.max_row,
                               "max_column": sheet.max_column} for sheet in book.worksheets]
                finally:
                    book.close()
            except (zipfile.BadZipFile, KeyError, OSError):
                raise ValueError("无法读取 XLSX 工作簿，请检查格式或是否加密") from None
            dataset = {"id": uuid.uuid4().hex, "filename": filename,
                       "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
                       "sheets": sheets, "created": now()}
            path = self.datasets / (dataset["id"] + ".xlsx")
            path.write_bytes(payload)
            write_json(path.with_suffix(".json"), dataset)
            return dataset

    def table(self, body):
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
        dataset, path = self.file(body.get("dataset_id", ""))
        sheet_name = str(body.get("sheet", ""))
        try:
            header_row = int(body.get("header_row", 1))
        except (TypeError, ValueError):
            raise ValueError("表头行须为 1–100 的整数") from None
        if not 1 <= header_row <= 100:
            raise ValueError("表头行须为 1–100 的整数")
        book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        cached = None
        try:
            if sheet_name not in book.sheetnames:
                raise ValueError("工作表不存在")
            sheet = book[sheet_name]
            if (sheet.max_column or 0) > LIMITS["columns"] or (sheet.max_row or 0) > LIMITS["rows"] + header_row:
                raise ValueError("选中工作表超过 64 列或 10,000 数据行，请拆分；不会截掉尾部再计算")
            # Read all rows in the bounded worksheet, including hidden/filtered rows.
            # Formula caches remain diagnostic only, even when Excel saved one.
            cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
            sheet.reset_dimensions()
            cached[sheet_name].reset_dimensions()
            values_iter = cached[sheet_name].iter_rows(min_row=header_row)
            headers, rows = [], []
            chars = cells_count = 0
            blank_rows = []
            for offset, (cells, cache_cells) in enumerate(zip(sheet.iter_rows(min_row=header_row), values_iter)):
                row_number = header_row + offset
                if offset > LIMITS["rows"]:
                    raise ValueError("数据超过 10,000 行")
                if len(cells) > LIMITS['columns']:
                    raise ValueError("实际数据超过 64 列")
                raw = []
                for index, cell in enumerate(cells):
                    value = text(cell.value)
                    chars += len(value)
                    cells_count += 1
                    if chars > LIMITS["text_chars"] or cells_count > LIMITS["cells"]:
                        raise ValueError("工作表内容过大，请按业务范围拆分后导入")
                    kind = ("formula" if cell.data_type == "f" else "error" if cell.data_type == "e"
                            else "date" if isinstance(cell.value, (date, datetime))
                            else "bool" if isinstance(cell.value, bool)
                            else "number" if isinstance(cell.value, (int, float)) else "text")
                    raw.append({"column": get_column_letter(index + 1), "value": value, "type": kind,
                                "formula": value if kind == "formula" else "",
                                "cached": text(cache_cells[index].value) if kind == "formula" and index < len(cache_cells) else ""})
                if offset == 0:
                    headers = [{"key": c["column"], "label": c["value"].strip() or "（空表头）"} for c in raw]
                    if not any(c["value"].strip() for c in raw):
                        raise ValueError("所选表头行为空，请选择正确的表头行")
                    if any(c["formula"] for c in raw):
                        raise ValueError("表头含公式，请使用已确认的文字表头")
                elif any(c["value"].strip() for c in raw):
                    rows.append({"row": row_number, "cells": raw})
                else:
                    blank_rows.append(row_number)
            if not headers:
                raise ValueError("未读取到表头")
            # Preserve cell-level identity independently of duplicate header labels.
            labels = {h["key"]: h["label"] for h in headers}
            for row in rows:
                for cell in row["cells"]:
                    if cell['column'] not in labels:
                        raise ValueError("数据超出表头列范围，请补齐表头后导入")
                    cell["header"] = labels[cell["column"]]
            return {"dataset": dataset, "sheet": sheet_name, "header_row": header_row,
                    "headers": headers, "rows": rows, "blank_rows": blank_rows,
                    "data_end_row": rows[-1]["row"] if rows else header_row}
        finally:
            book.close()
            if cached:
                cached.close()

    def preview(self, body):
        with self.lock:
            table = self.table(body)
            values = {h["key"]: [] for h in table["headers"]}
            seen = {k: set() for k in values}
            for row in table["rows"]:
                for cell in row["cells"]:
                    key, value = cell["column"], cell["value"].strip()
                    if value and not cell["formula"] and value not in seen[key]:
                        if len(values[key]) < 100:
                            values[key].append(value)
                        seen[key].add(value)
            issues = []
            duplicates = [label for label, n in Counter(h["label"] for h in table["headers"]).items() if n > 1]
            if duplicates:
                issues.append("存在同名表头，请按列字母区分：" + "、".join(duplicates))
            if table["blank_rows"]:
                issues.append(f"{len(table['blank_rows'])} 个空白行不计入记录")
            if any(len(s) > 100 for s in seen.values()):
                issues.append("列值选项最多展示 100 种；记录本身未截断")
            return {"headers": table["headers"], "rows": [
                {"row": r["row"], "values": {c["column"]: c["value"] for c in r["cells"]},
                 "formula_columns": [c["column"] for c in r["cells"] if c["formula"]]} for r in table["rows"][:20]],
                "row_count": len(table["rows"]), "data_end_row": table["data_end_row"],
                "values": values, "issues": issues}

    def analyze(self, body):
        start = time.perf_counter()
        with self.lock:
            table = self.table(body)
            try:
                first = int(body.get("start_row") or table["header_row"] + 1)
                last = int(body.get("end_row") or table["data_end_row"])
            except (TypeError, ValueError):
                raise ValueError("数据起止行须为整数") from None
            if not table['rows'] or first <= table['header_row'] or last < first or last > table['data_end_row']:
                raise ValueError("请选择表头以下、实际数据范围以内的起止行")
            table['rows'] = [row for row in table['rows'] if first <= row['row'] <= last]
            table['blank_rows'] = [n for n in table['blank_rows'] if first <= n <= last]
            keys = {h["key"] for h in table["headers"]}
            columns = {k: str(body.get("columns", {}).get(k) or "") for k in ("title", "action", "status", "owner", "due", "risk", "number")}
            if not columns["title"] or any(v and v not in keys for v in columns.values()):
                raise ValueError("请选择有效的记录名称列及其他计算列")
            if len([v for v in columns.values() if v]) != len(set(v for v in columns.values() if v)):
                raise ValueError("不同用途须映射到不同列，请检查列对应")
            try:
                as_of = date.fromisoformat(str(body.get("as_of", "")))
            except ValueError:
                raise ValueError("请选择统计日期（YYYY-MM-DD）") from None
            completed_values = body.get("completed_values", [])
            if not isinstance(completed_values, list) or len(completed_values) > 100 or any(not isinstance(v, str) for v in completed_values):
                raise ValueError("完成状态选择无效")
            completed_values = sorted(set(v.strip() for v in completed_values if v.strip()))
            if completed_values and not columns["status"]:
                raise ValueError("未指定状态列，不能定义完成状态")
            unit = str(body.get("numeric_unit", "")).strip()[:40]
            if columns["number"] and not unit:
                raise ValueError("数值汇总须填写单位，统一单位后再计算；无单位可写“无单位”")
            title = str(body.get("title", "")).strip()[:160] or f"{table['sheet']} · 统计报告"
            notes = str(body.get("notes", ""))[:10000]
            rows, issue_rows = [], defaultdict(list)
            numeric = {"sum": None, "valid": 0, "invalid": 0, "missing": 0, "unit": unit}
            total = Decimal(0)
            completed = open_count = overdue_count = unknown_due = unknown_status = valid_due = 0
            duplicate_keys = defaultdict(list)
            for source in table["rows"]:
                cells = {c["column"]: c for c in source["cells"]}
                def cell(field):
                    return cells.get(columns[field], {"value": "", "type": "text", "formula": "", "cached": ""})
                def value(field):
                    c = cell(field)
                    return "公式未核算" if c["formula"] else "错误值未确认" if c['type'] == 'error' else c["value"].strip()
                label = value("title")
                row_issues = []
                eligible = bool(label) and not cell("title")["formula"] and cell("title")["type"] != "error"
                if not eligible:
                    row_issues.append("记录名称空白、错误或公式，未纳入统计")
                    issue_rows["excluded"].append(source["row"])
                for field in columns:
                    if columns[field] and cell(field)['type'] == 'error' and '映射字段含 Excel 错误值' not in row_issues:
                        row_issues.append('映射字段含 Excel 错误值')
                        issue_rows['cell_error'].append(source['row'])
                    if columns[field] and cell(field)["formula"]:
                        if "映射字段含公式（未核算）" not in row_issues:
                            row_issues.append("映射字段含公式（未核算）")
                            issue_rows["formula"].append(source["row"])
                due = parse_date(cell("due")) if columns["due"] else {"raw": "", "parsed": None, "issue": "未映射期限列"}
                number = value("number")
                row = {"row": source["row"], "label": label or "（名称未确认）", "eligible": eligible,
                       "action": value("action"),
                       "status": value("status"), "owner": value("owner"), "risk": value("risk"),
                       "due": due, "number": number, "issues": row_issues}
                if eligible:
                    duplicate_keys[tuple(c["value"] for c in source["cells"])].append(source["row"])
                    if columns["status"] and completed_values:
                        status = cell("status")
                        if status["formula"] or status["type"] == "error" or not status["value"].strip():
                            unknown_status += 1
                            issue_rows["status_unknown"].append(source["row"])
                        elif status["value"].strip() in completed_values:
                            completed += 1
                        else:
                            open_count += 1
                            if columns["due"]:
                                if due["parsed"]:
                                    valid_due += 1
                                    if due["parsed"] < as_of.isoformat():
                                        overdue_count += 1
                                else:
                                    unknown_due += 1
                    if columns["due"] and due["issue"]:
                        issue_rows["due_unknown"].append(source["row"])
                        row_issues.append("期限：" + due["issue"])
                    if columns["number"]:
                        number_cell = cell("number")
                        amount = decimal_value(number_cell)
                        if not number_cell["value"].strip():
                            numeric["missing"] += 1
                            issue_rows["number_missing"].append(source["row"])
                        elif amount is None:
                            numeric["invalid"] += 1
                            issue_rows["number_invalid"].append(source["row"])
                            row_issues.append("数值未纳入合计：不是已确认的统一单位数值")
                        else:
                            numeric["valid"] += 1
                            with localcontext() as context:
                                context.prec = 80
                                total += amount
                rows.append(row)
            duplicates = sorted(n for values in duplicate_keys.values() if len(values) > 1 for n in values)
            if duplicates:
                issue_rows["duplicate"].extend(duplicates)
            numeric["sum"] = format(total, "f") if numeric["valid"] else None
            eligible_rows = [r for r in rows if r["eligible"]]
            summary = {"total_rows": len(rows), "eligible_rows": len(eligible_rows),
                       "excluded_rows": len(rows) - len(eligible_rows), "blank_rows": len(table["blank_rows"]),
                       "completed": completed if completed_values else None,
                       "open": open_count if completed_values else None,
                       "unknown_status": unknown_status if completed_values else None,
                       "overdue": overdue_count if completed_values and columns["due"] and (valid_due or (open_count == 0 and unknown_status == 0)) else None,
                       "known_due": valid_due, "unknown_due": unknown_due if completed_values and columns["due"] else None,
                       "numeric": numeric}
            labels = {"excluded": "未纳入统计的行", "formula": "映射字段含公式，缓存值未用于计算", "cell_error":"映射字段含 Excel 错误值，未当作有效值",
                      "status_unknown": "状态未确认，未判断完成或逾期", "due_unknown": "期限空白或不能解析为绝对日期",
                      "number_invalid": "非数值、公式或带单位文本未纳入合计", "number_missing": "数值未填写",
                      "duplicate": "内容完全相同的行，已保留并计数，需人工确认是否重复"}
            config = {"dataset_id": table["dataset"]["id"], "sheet": table["sheet"],
                      "header_row": table["header_row"], "columns": columns, "completed_values": completed_values,
                      "numeric_unit": unit, "as_of": as_of.isoformat(), "start_row": first, "end_row": last}
            report = {"id": uuid.uuid4().hex, "title": title, "notes": notes, "created": now(), "updated": now(), "version": 1,
                      "dataset": table["dataset"], "config": config, "summary": summary,
                      "groups": {field: grouped(eligible_rows, field) if columns[field] else [] for field in ("status", "owner", "risk")},
                      "rows": rows, "issues": [{"code": k, "label": labels[k], "rows": v} for k, v in issue_rows.items()],
                      "lineage": {"sha256": table["dataset"]["sha256"], "sheet": table["sheet"], "header_row": table["header_row"],
                                  "range": f"A{first}:{table['headers'][-1]['key']}{last}",
                                  "columns": [{"role": k, "column": v, "label": next(h['label'] for h in table['headers'] if h['key'] == v)}
                                              for k, v in columns.items() if v]},
                      "timing": {"calculation_seconds": round(time.perf_counter() - start, 4), "model_calls": 0},
                      "_source_rows": table["rows"], "_headers": table["headers"]}
            if columns['due']:
                report['groups']['deadline'] = []
                for parsed, label in ((True, '可确认绝对日期'), (False, '未填写或未确认')):
                    members = [r['row'] for r in eligible_rows if bool(r['due']['parsed']) == parsed]
                    if members:
                        report['groups']['deadline'].append({'label':label,'count':len(members),'rows':members})
            write_json(self.reports / (report["id"] + ".json"), report)
            write_json(self.reports / (report["id"] + ".meta.json"), {k: report[k] for k in ('id','title','created','version')})
            return report

    def read(self, report_id):
        path = self.reports / (safe_id(report_id) + ".json")
        if not path.exists():
            raise KeyError(report_id)
        with self.lock:
            return json.loads(path.read_text("utf-8"))

    def save(self, report_id, body):
        with self.lock:
            report = self.read(report_id)
            if body.get("version") != report["version"]:
                raise ValueError("报告已更新，请重新打开后编辑")
            title = str(body.get("title", report["title"])).strip()
            if not title or len(title) > 160:
                raise ValueError("标题须为 1–160 字")
            report.update(title=title, notes=str(body.get("notes", report["notes"]))[:10000],
                          version=report["version"] + 1, updated=now())
            write_json(self.reports / (report["id"] + ".json"), report)
            write_json(self.reports / (report["id"] + ".meta.json"), {k: report[k] for k in ('id','title','created','version')})
            return report

    def source(self, report_id, number):
        report = self.read(report_id)
        self.file(report["dataset"]["id"])
        row = next((r for r in report["_source_rows"] if r["row"] == number), None)
        if row is None:
            raise KeyError(number)
        return {"filename": report["dataset"]["filename"], "sheet": report["config"]["sheet"], "row": number,
                "range": f"A{number}:{report['_headers'][-1]['key']}{number}",
                "sha256": report["dataset"]["sha256"], "cells": row["cells"]}


def public_report(report):
    return {k: v for k, v in report.items() if not k.startswith("_")}


def h(value):
    return escape(str(value), quote=True)


def detail_table(report, rows=None, offline=False):
    rows = report["rows"] if rows is None else rows
    cols = report["config"]["columns"]
    fields = [("label", "记录名称")] + [(k, label) for k, label in (("action", "处理动作"), ("status", "状态"), ("owner", "负责人"),
              ("due", "期限原值"), ("risk", "风险等级"), ("number", "数值")) if cols[k]]
    out = ['<div class="analysis-table-scroll"><table><thead><tr><th>原始行</th>' + ''.join('<th>'+label+'</th>' for _, label in fields) + '<th>核对提示</th></tr></thead><tbody>']
    for row in rows:
        n = row["row"]
        ref = f'<a href="#source-{n}">第 {n} 行</a>' if offline else f'<button type="button" data-source-row="{n}">第 {n} 行</button>'
        out.append(f'<tr><td>{ref}</td>' + ''.join('<td>'+h(row[k]["raw"] if k == "due" else row[k] or '—')+'</td>' for k, _ in fields)
                   + '<td>'+h('；'.join(row["issues"]) or '—')+'</td></tr>')
    return ''.join(out) + '</tbody></table></div>'


def chart(report, kind, title, offline=False):
    groups = report["groups"].get(kind, [])
    if not groups:
        return ''
    total = report["summary"]["eligible_rows"]
    peak = max(g["count"] for g in groups)
    # Charts show the largest 20 groups; remainder explicitly identified, never hidden from data.
    out = [f'<section class="analysis-chart"><h3>{title}</h3><p class="analysis-muted">按记录行计数 · 分母 {total}；条长从 0 起算。'+('负责人按原值分组，不拆分多人单元格。' if kind=='owner' else '')+('显示前 20 类；全部类别保留在下表。' if len(groups)>20 else '')+'</p>']
    for index, group in enumerate(groups[:20]):
        label, count = h(group["label"]), group["count"]
        if offline:
            opening, closing = '<div class="analysis-bar-row">', '</div>'
        else:
            opening, closing = f'<button class="analysis-bar-row" type="button" data-drill-kind="{kind}" data-drill-index="{index}" aria-label="查看{label}的{count}条记录">', '</button>'
        out.append(opening + f'<span>{label}</span><svg viewBox="0 0 300 16" preserveAspectRatio="none" role="img" aria-label="{count}条"><rect width="300" height="16" fill="#edf1f3"/><rect width="{300*count/peak:.2f}" height="16" fill="#486b7d"/></svg><strong>{count}</strong>' + closing)
    out.append('<details><summary>分类数量与原始行</summary><table><thead><tr><th>类别</th><th>数量</th><th>原始行</th></tr></thead><tbody>')
    for group in groups:
        out.append('<tr><td>'+h(group['label'])+'</td><td>'+str(group['count'])+'</td><td>'+', '.join(str(n) for n in group['rows'])+'</td></tr>')
    return ''.join(out) + '</tbody></table></details></section>'


REPORT_CSS = '''
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#edf0f2;color:#253441;font:15px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}main{max-width:1180px;margin:28px auto;background:white;padding:40px 48px}h1{font-size:26px;line-height:1.4;margin:8px 0 12px}h2{font-size:18px;border-bottom:1px solid #c7d0d5;padding-bottom:7px;margin:32px 0 14px}h3{font-size:16px;margin:0 0 5px}p{margin:8px 0 14px}.analysis-kicker{font-size:12px;letter-spacing:.12em;color:#566b78}.analysis-muted{color:#64747f;font-size:13px}.analysis-metrics{display:flex;gap:28px;flex-wrap:wrap;border-top:2px solid #3e5969;border-bottom:1px solid #ccd5da;padding:16px 0}.analysis-metrics div{min-width:110px}.analysis-metrics strong{font-size:25px;display:block;font-weight:600}.analysis-metrics span{font-size:13px;color:#5c6b74}.analysis-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:26px}.analysis-chart{min-width:0}.analysis-bar-row{display:grid;grid-template-columns:120px 1fr 35px;align-items:center;gap:10px;width:100%;padding:7px 0;border:0;background:transparent;text-align:left;color:inherit;font:inherit}.analysis-bar-row span{overflow-wrap:anywhere}.analysis-bar-row svg{width:100%;height:16px}.analysis-bar-row strong{text-align:right;font-size:14px}.analysis-table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 15px}th,td{border-bottom:1px solid #dbe1e5;padding:9px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#f3f5f6;font-weight:600}a{color:#315f79}button{font:inherit;cursor:pointer;color:#315f79}summary{cursor:pointer;color:#315f79}details{margin:12px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 Consolas,monospace}.analysis-notes{white-space:pre-wrap;border-left:2px solid #8c9ca6;padding-left:14px}.analysis-method{padding:14px 18px;background:#f5f6f7;font-size:13px}.analysis-source{break-inside:avoid;margin:20px 0}.analysis-actions{margin:12px 0}@media(max-width:700px){main{margin:0;padding:20px}.analysis-charts{grid-template-columns:1fr}.analysis-bar-row{grid-template-columns:100px 1fr 30px}}@media print{body{background:white}main{margin:0;max-width:none;padding:0}h2{break-after:avoid}.analysis-chart{break-inside:avoid}table{font-size:10px}thead{display:table-header-group}details> *{display:block!important}summary{display:none}.analysis-metrics{gap:18px}a{color:inherit;text-decoration:none}}
'''


def render_report(report, offline=False):
    s, config = report["summary"], report["config"]
    out = ['<article class="analysis-paper">', '<div class="analysis-kicker">工作表分析</div>',
           '<h1>'+h(report["title"])+'</h1>', '<p class="analysis-muted">统计日期 '+h(config["as_of"])+
           ' · '+h(report["dataset"]["filename"])+ ' / '+h(config["sheet"])+ ' · '+h(report["lineage"]["range"])+ '</p>']
    metrics = [(s["eligible_rows"], "纳入记录 / 行")]
    if config['columns']['status']:
        metrics.append((s['completed'], '完成 / 行') if config['completed_values'] else (len(report['groups']['status']), '原始状态 / 类'))
    if config['columns']['owner']:
        owners = {r['owner'] for r in report['rows'] if r['eligible'] and r['owner'] and r['owner'] not in ('公式未核算','错误值未确认')}
        metrics.append((len(owners), '责任分组 / 组'))
    if config['columns']['due']:
        metrics.append((s['overdue'], '已确认逾期 / 行') if config['completed_values'] else
                       (sum(1 for r in report['rows'] if r['eligible'] and not r['due']['parsed']), '期限待确认 / 行'))
    if config["columns"]["number"]:
        metrics.append((s["numeric"]["sum"], '合计 / '+s["numeric"]["unit"]))
    out.append('<div class="analysis-metrics">'+''.join('<div><strong>'+h(value if value is not None else '未判定')+'</strong><span>'+h(label)+'</span></div>' for value, label in metrics)+'</div>')
    out.append('<h2>统计结论</h2><p>本次读取 '+str(s['total_rows'])+' 个非空数据行，按记录名称列纳入 '+str(s['eligible_rows'])+' 行；'+str(s['excluded_rows'])+' 行未纳入，'+str(s['blank_rows'])+' 个空白行不计数。每个数据行计为一条，重复内容不自动合并。</p>')
    if not config['completed_values']:
        out.append('<p>未指定完成状态，因此不推断完成率或逾期数；原始状态仅做分类统计。</p>' if config['columns']['status'] else '<p>本次未映射状态列，因此不推断完成率或逾期数。</p>')
    else:
        out.append('<p>完成状态按“'+h('、'.join(config['completed_values']))+'”精确匹配：完成 '+str(s['completed'])+' 行，其他已知状态 '+str(s['open'])+' 行，状态未确认 '+str(s['unknown_status'])+' 行。</p>')
    if config['columns']['due']:
        out.append('<p>逾期定义：已确认未完成且期限早于统计日期；当天到期不算逾期。相对时间、空白与公式日期不推算。'+('待判定期限 '+str(s['unknown_due'])+' 行。' if s['unknown_due'] is not None else '未定义完成状态，暂不判断逾期。')+'</p>')
    if config['columns']['number']:
        n = s['numeric']
        out.append('<p>数值合计来自 '+str(n['valid'])+' 个有效数值；'+str(n['invalid'])+' 个未确认值、'+str(n['missing'])+' 个空值未纳入。未执行公式，也未换算单位。</p>')
    out.append('<h2>分布与明细</h2><p class="analysis-muted">'+('图表和以下明细均为此次计算的固定快照。' if offline else '点击图表条目筛选明细，再点原始行核对工作簿单元格。')+'</p><div class="analysis-charts">')
    for kind, title in (("status", "状态分布"), ("risk", "风险等级分布"), ("deadline", "期限是否可确认"), ("owner", "负责人分布")):
        out.append(chart(report, kind, title, offline))
    out.append('</div>')
    if report['issues']:
        out.append('<h2>待核对事项</h2><table><thead><tr><th>事项</th><th>涉及行数</th><th>原始行</th></tr></thead><tbody>')
        for item in report['issues']:
            out.append('<tr><td>'+h(item['label'])+'</td><td>'+str(len(item['rows']))+'</td><td>'+h(', '.join(map(str,item['rows'])))+'</td></tr>')
        out.append('</tbody></table>')
    out.append('<h2>记录明细</h2>')
    if not offline:
        out.append('<div class="analysis-actions"><button type="button" data-all-rows>显示全部记录</button></div>')
    out.append('<div id="analysis-details">'+detail_table(report, None if offline else report['rows'][:100], offline)+'</div>')
    if report['notes']:
        out.append('<h2>工作说明</h2><div class="analysis-notes">'+h(report['notes'])+'</div>')
    roles = {'title':'记录名称','action':'处理动作','status':'状态','owner':'负责人','due':'期限','risk':'风险等级','number':'数值'}
    out.append('<h2>口径与来源</h2><div class="analysis-method">读取所选工作表第 '+str(config['start_row'])+'–'+str(config['end_row'])+' 行，表头为第 '+str(config['header_row'])+' 行，包括此范围内的 Excel 隐藏行与筛选隐藏行；不按 Excel 当前筛选状态统计。表内小计或合计行如有名称也会作为记录，应在设置起止行时排除。计算使用导入副本，不会修改原文件。<br>列对应：'+h('；'.join(roles[x['role']]+' = '+x['column']+'列「'+x['label']+'」' for x in report['lineage']['columns']))+'<br>原文件 SHA-256：'+h(report['lineage']['sha256'])+'<br>本地计算用时 '+h(report['timing']['calculation_seconds'])+' 秒。统计日期由使用者确认，文字说明与计算指标分开保存。</div>')
    if offline:
        out.append('<h2>原始行追溯</h2><p class="analysis-muted">以下为导入时原始单元格快照；公式仅记录表达式与缓存，未执行。可用上方文件指纹核对工作簿版本。</p>')
        for row in report['_source_rows']:
            out.append(f'<section class="analysis-source" id="source-{row["row"]}"><h3>'+h(config['sheet'])+f' · 第 {row["row"]} 行</h3><table><thead><tr><th>单元格</th><th>表头</th><th>原值</th><th>类型 / 公式缓存</th></tr></thead><tbody>')
            for cell in row['cells']:
                out.append('<tr><td>'+cell['column']+str(row['row'])+'</td><td>'+h(cell['header'])+'</td><td>'+h(cell['value'])+'</td><td>'+h(cell['type']+(' / '+cell['cached'] if cell['formula'] else ''))+'</td></tr>')
            out.append('</tbody></table></section>')
    return ''.join(out) + '</article>'


def export_html(report):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+h(report['title'])+'</title><style>'+REPORT_CSS+'</style><body><main>'+render_report(report, True)+'</main></body></html>'
