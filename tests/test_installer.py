from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.installer import INSTALLER_HTML, InstallerApi, copy_payload, create_desktop_shortcut


class InstallerPayloadTests(unittest.TestCase):
    def test_installer_page_matches_the_guided_install_flow_and_binds_controls_immediately(self) -> None:
        self.assertIn("安装应用与本地检索组件", INSTALLER_HTML)
        self.assertIn("下载智能模型 Qwen3-4B", INSTALLER_HTML)
        self.assertIn("创建桌面快捷方式", INSTALLER_HTML)
        self.assertIn("安装并下载模型", INSTALLER_HTML)
        self.assertIn("笔记与文件不会上传到网络", INSTALLER_HTML)
        self.assertIn("addEventListener('click',chooseDestination)", INSTALLER_HTML)
        self.assertIn("waitForBridge", INSTALLER_HTML)

    def test_choose_destination_returns_the_folder_selected_in_native_dialog(self) -> None:
        api = InstallerApi()
        selected = Path("C:/Users/Tester/AppData/Local/PocketMemory")
        api.window = SimpleNamespace()

        with patch("scripts.installer.choose_windows_folder", return_value=str(selected)):
            self.assertEqual(api.choose_destination(), str(selected.resolve()))

    def test_update_keeps_user_data_and_downloaded_llm_but_refreshes_app_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "payload"
            target = root / "installed"
            (payload / "_internal").mkdir(parents=True)
            (payload / "_internal" / "runtime.txt").write_text("new", encoding="utf-8")
            (payload / "models" / "bge-small-zh-v1.5").mkdir(parents=True)
            (payload / "models" / "bge-small-zh-v1.5" / "model.onnx").write_bytes(b"new-bge")
            (target / "data").mkdir(parents=True)
            (target / "data" / "notes.db").write_bytes(b"user-data")
            (target / "models" / "Qwen3-4B").mkdir(parents=True)
            (target / "models" / "Qwen3-4B" / "model.gguf").write_bytes(b"user-model")
            (target / "_internal").mkdir(parents=True)
            (target / "_internal" / "runtime.txt").write_text("old", encoding="utf-8")

            copy_payload(payload, target)

            self.assertEqual((target / "data" / "notes.db").read_bytes(), b"user-data")
            self.assertEqual((target / "models" / "Qwen3-4B" / "model.gguf").read_bytes(), b"user-model")
            self.assertEqual((target / "_internal" / "runtime.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual((target / "models" / "bge-small-zh-v1.5" / "model.onnx").read_bytes(), b"new-bge")

    def test_desktop_shortcut_targets_the_desktop_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "PocketMemory" / "PocketMemory.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"exe")
            with patch.dict("os.environ", {"USERPROFILE": str(root)}, clear=False), patch("scripts.installer.subprocess.run") as run:
                shortcut = create_desktop_shortcut(executable, executable.parent)

            self.assertEqual(shortcut, root / "Desktop" / "Pocket Memory.lnk")
            command = run.call_args.args[0]
            self.assertEqual(command[0], "powershell.exe")
            self.assertIn(str(shortcut), command)
            self.assertIn(str(executable), command)
