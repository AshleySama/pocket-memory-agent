# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Read-only local folder navigation for browser and desktop settings.

No native modal window, file-content reads, directory creation, or library
switching occurs here. The existing explicit switch/migrate actions own writes.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


class DirectoryBrowseError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _local_roots() -> list[dict[str, str]]:
    if sys.platform != "win32":
        return [{"name": "/", "path": "/"}]
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetLogicalDrives.argtypes = []
    kernel.GetLogicalDrives.restype = wintypes.DWORD
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise DirectoryBrowseError("无法读取本机磁盘列表，请在目录栏输入完整路径", 503)
    roots = []
    for index in range(26):
        if mask & (1 << index):
            name = chr(ord("A") + index) + ":"
            path = name + "\\"
            # Local/removable/CD/RAM disks only. Do not probe network mounts.
            if kernel.GetDriveTypeW(path) in (2, 3, 5, 6):
                roots.append({"name": name, "path": path})
    return roots


def browse_directory(value: str | None, initial_dir: Path) -> dict:
    """None opens the current library; an empty string lists local drives."""
    if value is not None and not isinstance(value, str):
        raise DirectoryBrowseError("目录路径必须是文字")
    roots = _local_roots()
    if value == "":
        return {"path": "", "parent": None, "directories": roots,
                "roots": roots, "truncated": False}
    value = str(initial_dir) if value is None else value.strip()
    if not value or "\x00" in value:
        raise DirectoryBrowseError("请输入完整目录路径")
    if sys.platform == "win32" and value.startswith(("\\\\", "//")):
        raise DirectoryBrowseError("请选择本机磁盘上的目录，不支持网络或设备路径")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DirectoryBrowseError("请输入完整的绝对目录路径")
    if sys.platform == "win32" and path.anchor.casefold() not in {
            root["path"].casefold() for root in roots}:
        raise DirectoryBrowseError("请选择可访问的本机磁盘，不支持网络映射目录")
    try:
        path = path.resolve(strict=True)
        if sys.platform == "win32" and path.anchor.casefold() not in {
                root["path"].casefold() for root in roots}:
            raise DirectoryBrowseError("此路径指向网络或设备目录，请选择本机文件夹")
        if not path.is_dir():
            raise DirectoryBrowseError("所选位置不是文件夹")
        directories = []
        truncated = False
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_directory:
                    continue
                if len(directories) >= 1000:
                    truncated = True
                    break
                directories.append({"name": entry.name, "path": str(path / entry.name)})
        directories.sort(key=lambda entry: (entry["name"].casefold(), entry["name"]))
    except PermissionError as exc:
        raise DirectoryBrowseError("没有权限打开此文件夹，请选择其他目录", 403) from exc
    except FileNotFoundError as exc:
        raise DirectoryBrowseError("此文件夹不存在，请检查路径", 404) from exc
    except NotADirectoryError as exc:
        raise DirectoryBrowseError("所选位置不是文件夹") from exc
    except OSError as exc:
        raise DirectoryBrowseError("无法读取此文件夹，请检查磁盘是否可用", 503) from exc
    parent = None if path.parent == path else str(path.parent)
    return {"path": str(path), "parent": parent, "directories": directories,
            "roots": roots, "truncated": truncated}
