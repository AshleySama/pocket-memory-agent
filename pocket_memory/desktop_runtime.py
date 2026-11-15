# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Configure this frozen executable's CLR before pythonnet initializes it.

Downloaded ZIP entries may retain their Internet zone. .NET LoadFrom rejects
those managed assemblies unless the host application opts into full trust.
The setting belongs beside the host EXE, not in PyInstaller's _internal folder
or a child AppDomain. No file marks or machine policies are modified here.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

CONFIG = b'''<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <runtime>
    <loadFromRemoteSources enabled="true" />
  </runtime>
</configuration>
'''


def _validate(path: Path) -> None:
    # Never rewrite an existing custom application configuration or follow a
    # configuration-file link to user data outside the installation directory.
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise RuntimeError("桌面运行配置不能指向安装目录之外。")
    if path.stat().st_size > 65536:
        raise RuntimeError("已有桌面运行配置过大，未修改该文件。")
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError as exc:
        raise RuntimeError("已有桌面运行配置损坏，未覆盖该文件。") from exc
    setting = root.findall('./runtime/loadFromRemoteSources')
    if root.tag != 'configuration' or len(setting) != 1 or setting[0].get('enabled', '').lower() != 'true':
        raise RuntimeError("已有桌面运行配置与此版本不兼容，未覆盖该文件。请将完整包解压到新文件夹。")


def ensure_native_runtime_config(executable: Path | None = None) -> Path | None:
    """First launch installs a tiny sidecar; source Python is never changed.

Creating it here also preserves compatibility with 1.0.0's updater whitelist:
the update remains ordinary executable/internal files, not a new root payload.
"""
    if sys.platform != 'win32' or (executable is None and not getattr(sys, 'frozen', False)):
        return None
    target = Path(str(Path(executable or sys.executable).absolute()) + '.config')
    if target.exists() or target.is_symlink():
        _validate(target)
        return target
    temp = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.pm-desktop-', suffix='.tmp', dir=target.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(CONFIG)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Windows rename is atomic and fails if another launcher won the
            # race. Do not replace a custom .exe.config created concurrently.
            os.rename(temp, target)
        except FileExistsError:
            pass
        _validate(target)
        return target
    except OSError as exc:
        raise RuntimeError("无法写入桌面运行配置。请将完整包解压到有写入权限的新文件夹后运行。") from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
