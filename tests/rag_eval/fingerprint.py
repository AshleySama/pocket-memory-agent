# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

from .dataset import file_sha256


DEPENDENCIES = (
    "numpy",
    "onnxruntime",
    "tokenizers",
    "llama-cpp-python",
    "rapidocr",
    "Pillow",
    "PyMuPDF",
    "openpyxl",
    "python-docx",
    "pywebview",
    "pyinstaller",
)


def environment_fingerprint(root: Path, fixture_path: Path, command: list[str] | None = None) -> dict:
    return {
        "generated_at": _iso_now(),
        "command": command or sys.argv,
        "git": _git_state(root),
        "runtime": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count(),
            "physical_memory_bytes": _physical_memory_bytes(),
            "python_hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
        },
        "dependencies": _dependency_versions(),
        "fixture": {
            "path": str(fixture_path.resolve()),
            "sha256": file_sha256(fixture_path),
        },
        "models": _model_fingerprints(root),
        "source_sha256": {
            str(path.relative_to(root)).replace("\\", "/"): file_sha256(path)
            for directory in ("pocket_memory", "scripts", "tests/rag_eval")
            for path in sorted((root / directory).rglob("*.py"))
        },
    }


def _git_state(root: Path) -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=root, text=True, encoding="utf-8", errors="replace"
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    status = run("status", "--short")
    return {
        "head": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(status),
        "status": status.splitlines(),
    }


def _dependency_versions() -> dict:
    versions = {}
    for name in DEPENDENCIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _model_fingerprints(root: Path) -> list[dict]:
    patterns = (
        "models/Qwen3-4B/*.gguf",
        "models/bge-small-zh-v1.5/onnx/model_quantized.onnx",
        "models/bge-small-zh-v1.5/onnx/model_quantized.onnx_data",
        "models/rapidocr/*.onnx",
    )
    rows = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            rows.append(
                {
                    "path": str(path.relative_to(root)).replace("\\", "/"),
                    "bytes": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
            )
    return rows


def _physical_memory_bytes() -> int | None:
    if os.name != "nt":
        return None

    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
    except (AttributeError, OSError):
        return None
    return None


def _iso_now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")
