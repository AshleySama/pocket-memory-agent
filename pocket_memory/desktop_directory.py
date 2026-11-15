# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Folder selection belongs to the desktop window, not the background server."""
from pathlib import Path
import sys

from .directory_picker import DirectoryPickerError, choose_directory


class DesktopDirectoryApi:
    def __init__(self):
        # Private to avoid exposing the window object through pywebview's bridge.
        self._window = None

    def _owner_handle(self):
        if self._window is None:
            raise DirectoryPickerError("桌面窗口尚未就绪，请稍后重试")
        from System import Action
        native = self._window.native
        if native is None or native.IsDisposed:
            raise DirectoryPickerError("桌面窗口尚未就绪，请稍后重试")
        handles = []

        def read_handle():
            if not native.IsDisposed:
                handles.append(int(native.Handle.ToInt64()))

        if native.InvokeRequired:
            native.Invoke(Action(read_handle))
        else:
            read_handle()
        if not handles or not handles[0]:
            raise DirectoryPickerError("无法定位桌面窗口，请关闭设置后重试")
        return handles[0]

    def choose_data_directory(self, initial_path=None):
        """Return a selected path only; never modify the library or config."""
        try:
            initial = Path(initial_path) if isinstance(initial_path, str) and initial_path else None
            owner = self._owner_handle() if sys.platform == "win32" else None
            selected = choose_directory(initial, owner_hwnd=owner)
            return {"data_dir": str(selected) if selected is not None else None}
        except Exception as exc:
            return {"error": str(exc) or "无法打开目录选择窗口，请重试或粘贴完整路径"}
