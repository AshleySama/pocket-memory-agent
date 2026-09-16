#!/usr/bin/env python3
"""下载 WebView2 Bootstrapper，以便 PyInstaller 打包时附带给最终用户。

首次启动 exe 时，app.py 会检测 WebView2 Runtime 是否已安装，
若缺失则使用此 bootstrapper 静默安装，用户无需手动配置环境。

运行方式:
    python download_bootstrapper.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
DEST = Path(__file__).resolve().parent / "MicrosoftEdgeWebview2Setup.exe"


def main() -> int:
    if DEST.is_file():
        size_mb = DEST.stat().st_size / 1024 / 1024
        print(f"已存在: {DEST} ({size_mb:.1f} MB)，跳过下载。")
        return 0

    print(f"正在从微软官网下载 WebView2 Bootstrapper ...")
    try:
        urllib.request.urlretrieve(BOOTSTRAPPER_URL, str(DEST))
    except Exception as exc:
        print(f"下载失败: {exc}")
        print(f"请手动下载并放到项目根目录: {DEST.name}")
        print(f"下载地址: {BOOTSTRAPPER_URL}")
        return 1

    size_mb = DEST.stat().st_size / 1024 / 1024
    print(f"下载完成: {DEST} ({size_mb:.1f} MB)")
    print("PyInstaller 打包时会自动包含此文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
