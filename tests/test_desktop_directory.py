# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from pocket_memory.desktop_directory import DesktopDirectoryApi
from pocket_memory import directory_picker


class DesktopDirectoryTests(TestCase):
    def test_ui_owner_is_passed_and_selection_only_returns_path(self):
        api = DesktopDirectoryApi()
        native = SimpleNamespace(IsDisposed=False, InvokeRequired=True,
                                 Handle=SimpleNamespace(ToInt64=lambda: 1234))
        invoked = []
        native.Invoke = lambda callback: (invoked.append(True), callback())
        api._window = SimpleNamespace(native=native)
        target = Path.cwd()
        with patch.dict(sys.modules, {"System": SimpleNamespace(Action=lambda callback: callback)}), \
             patch("pocket_memory.desktop_directory.sys.platform", "win32"), \
             patch("pocket_memory.desktop_directory.choose_directory", return_value=target) as choose:
            self.assertEqual(api.choose_data_directory(str(target)), {"data_dir": str(target)})
        choose.assert_called_once_with(target, owner_hwnd=1234)
        self.assertEqual(invoked, [True])

    def test_cancel_error_and_missing_owner_are_distinct(self):
        api = DesktopDirectoryApi()
        with patch.object(api, "_owner_handle", return_value=1234), \
             patch("pocket_memory.desktop_directory.sys.platform", "win32"), \
             patch("pocket_memory.desktop_directory.choose_directory", return_value=None) as choose:
            self.assertEqual(api.choose_data_directory(), {"data_dir": None})
            choose.side_effect = OSError("dialog unavailable")
            self.assertEqual(api.choose_data_directory(), {"error": "dialog unavailable"})
        with patch("pocket_memory.desktop_directory.sys.platform", "win32"), \
             patch("pocket_memory.desktop_directory.choose_directory") as choose:
            self.assertIn("error", api.choose_data_directory())
            choose.assert_not_called()

    def test_native_worker_preserves_owner(self):
        with patch.object(directory_picker.sys, "platform", "win32"), \
             patch.object(directory_picker, "_windows_directory", return_value=None) as native:
            self.assertIsNone(directory_picker.choose_directory(Path.cwd(), owner_hwnd=4321))
        native.assert_called_once_with(Path.cwd(), 4321)
