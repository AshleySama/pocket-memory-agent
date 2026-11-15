# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Local model diagnostics; never downloads, changes or guesses a replacement model."""
from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sys
from pathlib import Path


class ModelFileError(RuntimeError):
    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def memory_snapshot() -> dict:
    """Best-effort Windows memory/commit availability, not a diagnosis of failure."""
    if sys.platform != "win32":
        return {"available": False}
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total_phys", "available_phys", "total_pagefile", "available_pagefile",
             "total_virtual", "available_virtual", "available_extended_virtual")]
    try:
        value = MemoryStatus()
        value.length = ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            return {"available": False}
        return {"available": True, "load_percent": value.load,
                "physical_available_bytes": value.available_phys,
                "commit_available_bytes": value.available_pagefile,
                "commit_limit_bytes": value.total_pagefile}
    except (AttributeError, OSError):
        return {"available": False}


def _manifest_entry(path: Path) -> dict | None:
    # Use the release's inventory, NOT the downloader's independently pinned
    # mirror artifact. The two valid GGUF exports have different size and hash.
    for root in path.parents:
        if root.name.casefold() != "models":
            continue
        install = root.parent.parent if root.parent.name == "_internal" else root.parent
        manifest = install / "model-resources.json"
        try:
            if not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
                continue
            entries = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if not isinstance(entries, list):
                continue
            relative = path.relative_to(root).as_posix()
            for entry in entries:
                if (isinstance(entry, dict) and entry.get("path") == relative
                        and type(entry.get("size")) is int and entry["size"] >= 24
                        and isinstance(entry.get("sha256"), str)
                        and re.fullmatch(r"[0-9a-fA-F]{64}", entry["sha256"])):
                    return {"manifest": str(manifest), "size": entry["size"],
                            "sha256": entry["sha256"].lower()}
        except (OSError, ValueError, UnicodeError):
            continue
    return None


def inspect_model_file(path: Path) -> dict:
    """Cheap preflight. Custom GGUFs without a release inventory stay supported."""
    details = {"path": str(path), "manifest": _manifest_entry(path)}
    try:
        with path.open("rb") as source:
            stat = path.stat()
            magic = source.read(4)
        details.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns, magic=magic.hex())
    except FileNotFoundError as exc:
        raise ModelFileError("模型文件未找到。请确认已完整解压安装包，再重新加载。", details) from exc
    except (PermissionError, OSError) as exc:
        details["read_error"] = f"{type(exc).__name__}: {exc}"
        raise ModelFileError("暂时无法读取模型文件。请检查文件访问权限，再重新加载。", details) from exc
    if magic != b"GGUF" or stat.st_size < 24:
        raise ModelFileError("模型文件格式不完整。请从完整安装包恢复模型文件后重新加载。", details)
    expected = details["manifest"]
    if expected and stat.st_size != expected["size"]:
        raise ModelFileError("模型文件大小与安装包不一致，可能未完整解压。请恢复模型文件后重新加载。", details)
    return details


def diagnose_load_failure(path: Path, preflight: dict, errors: list[str]) -> tuple[str, dict]:
    """Hash only after native load failure. Generic native errors remain unknown."""
    details = {"file": preflight, "attempt_errors": errors, "memory_after": memory_snapshot(),
               "integrity": "not_verified", "cause": "unknown"}
    expected = preflight.get("manifest")
    if expected:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
            stat = path.stat()
            details["sha256"] = digest.hexdigest()
            if (stat.st_size, stat.st_mtime_ns) != (preflight["size"], preflight["mtime_ns"]):
                details.update(integrity="changed_during_check", cause="file_changed")
                return "模型文件在加载时发生变化。请等待文件操作完成，再重新加载。", details
            if digest.hexdigest() != expected["sha256"]:
                details.update(integrity="mismatch", cause="file_mismatch")
                return "模型文件校验未通过。请从完整安装包恢复模型文件后重新加载。", details
            details["integrity"] = "matches_release_inventory"
        except OSError as exc:
            details.update(integrity="read_failed", cause="file_unreadable", read_error=str(exc))
            return "暂时无法读取模型文件。请检查文件访问权限，再重新加载。", details
    # Low memory is context, not proof. Do not convert a generic native failure
    # into a confident assertion about memory, endpoint software or corruption.
    return ("模型暂未加载成功。可关闭部分占用内存的程序后重新加载；详细诊断已保存在本机日志。", details)
