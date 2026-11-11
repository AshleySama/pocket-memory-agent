# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pocket_memory import directory_picker as picker
import test_server as server_tests


class DirectoryPickerTests(unittest.TestCase):
    def test_windows_selection_uses_native_worker_and_returns_path(self):
        original_thread = threading.get_ident()
        observed = []
        target = Path.cwd()

        def choose(initial):
            observed.append((threading.get_ident(), initial))
            return target

        with patch.object(picker.sys, "platform", "win32"), \
                patch.object(picker, "_windows_directory", side_effect=choose), \
                patch.object(picker, "_other_directory", side_effect=AssertionError("Tk must not be used")):
            self.assertEqual(picker.choose_directory(target), target)
        self.assertNotEqual(observed[0][0], original_thread)
        self.assertEqual(observed[0][1], target)

    def test_cancel_is_none_and_failure_is_not_cancel(self):
        with patch.object(picker.sys, "platform", "win32"), \
                patch.object(picker, "_windows_directory", return_value=None):
            self.assertIsNone(picker.choose_directory())
        with patch.object(picker.sys, "platform", "win32"), \
                patch.object(picker, "_windows_directory", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(picker.DirectoryPickerError, "unavailable"):
                picker.choose_directory()
        # An error must not leave the process-wide picker locked.
        self.assertTrue(picker._picker_lock.acquire(blocking=False))
        picker._picker_lock.release()

    def test_second_window_is_not_opened_while_one_is_pending(self):
        picker._picker_lock.acquire()
        try:
            with patch.object(picker, "_windows_directory") as native:
                with self.assertRaisesRegex(picker.DirectoryPickerError, "已打开"):
                    picker.choose_directory()
                native.assert_not_called()
        finally:
            picker._picker_lock.release()


class DirectoryPickerApiTests(unittest.TestCase):
    # Reuse only the isolated HTTP fixture, not its unrelated full-suite tests.
    setUp = server_tests.PocketMemoryServerTests.setUp
    tearDown = server_tests.PocketMemoryServerTests.tearDown
    request = server_tests.PocketMemoryServerTests.request
    request_error = server_tests.PocketMemoryServerTests.request_error

    def test_selection_and_cancel_do_not_switch_or_migrate_library(self):
        original = self.store.data_dir
        other = original.parent / "other library"
        other.mkdir()
        self.server.choose_directory = lambda _: other
        status, result = self.request("/api/settings/choose-data-dir", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"data_dir": str(other)})
        self.assertEqual(self.store.data_dir, original)
        self.assertEqual(self.config.data_dir, original)
        self.assertEqual(list(other.iterdir()), [])
        self.server.choose_directory = lambda _: None
        self.assertEqual(self.request("/api/settings/choose-data-dir", "POST", {}),
                         (200, {"data_dir": None}))
        self.assertEqual(self.store.data_dir, original)

    def test_unavailable_picker_is_visible_error_not_false_cancel(self):
        def fail(_):
            raise picker.DirectoryPickerError("unavailable")
        original = self.store.data_dir
        self.server.choose_directory = fail
        status, result = self.request_error("/api/settings/choose-data-dir", "POST", {})
        self.assertEqual(status, 503)
        self.assertIn("粘贴完整路径", result["error"])
        self.assertEqual(self.store.data_dir, original)

    def test_open_dialog_does_not_block_other_saves(self):
        opened = threading.Event()
        release = threading.Event()
        saved = threading.Event()
        results = []
        failures = []

        def choose(_):
            opened.set()
            release.wait(5)
            return None

        def pick_request():
            try:
                results.append(self.request("/api/settings/choose-data-dir", "POST", {}))
            except Exception as exc:
                failures.append(exc)

        def save_request():
            try:
                results.append(self.request("/api/settings/theme", "POST", {"theme": "aqua-memory"}))
                saved.set()
            except Exception as exc:
                failures.append(exc)

        self.server.choose_directory = choose
        pick_thread = threading.Thread(target=pick_request, daemon=True)
        save_thread = threading.Thread(target=save_request, daemon=True)
        pick_thread.start()
        try:
            self.assertTrue(opened.wait(2), "picker did not open")
            save_thread.start()
            self.assertTrue(saved.wait(2), "native dialog must not hold the application write lock")
        finally:
            release.set()
            pick_thread.join(5)
            if save_thread.ident is not None:
                save_thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertIn((200, {"data_dir": None}), results)


if __name__ == "__main__":
    unittest.main()
