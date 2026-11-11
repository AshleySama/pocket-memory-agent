# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pocket_memory.directory_browser import browse_directory, DirectoryBrowseError
import test_server as server_tests


class DirectoryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.roots = [{"name": self.root.anchor.rstrip("\\/"), "path": self.root.anchor}]
        self.root_patch = patch("pocket_memory.directory_browser._local_roots", return_value=self.roots)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def test_current_directory_lists_folders_only_without_reading_or_writing_files(self):
        (self.root / "中文目录").mkdir()
        (self.root / "another").mkdir()
        (self.root / "private.txt").write_text("not returned", encoding="utf-8")
        before = sorted(item.name for item in self.root.iterdir())
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read file content")):
            result = browse_directory(None, self.root)
        self.assertEqual(result["path"], str(self.root))
        self.assertEqual(result["parent"], str(self.root.parent))
        self.assertEqual([item["name"] for item in result["directories"]], ["another", "中文目录"])
        self.assertNotIn("private", json.dumps(result))
        self.assertFalse(result["truncated"])
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), before)

    def test_empty_path_lists_drives_and_root_has_no_parent(self):
        result = browse_directory("", self.root)
        self.assertEqual(result["directories"], self.roots)
        self.assertEqual(result["path"], "")
        self.assertIsNone(result["parent"])
        result = browse_directory(self.root.anchor, self.root)
        self.assertIsNone(result["parent"])

    def test_missing_non_directory_relative_and_non_text_paths_are_errors(self):
        file = self.root / "file.txt"
        file.write_text("secret", encoding="utf-8")
        for value, status in ((str(self.root / "missing"), 404), (str(file), 400),
                              ("relative", 400), ([], 400)):
            with self.subTest(value=value):
                with self.assertRaises(DirectoryBrowseError) as raised:
                    browse_directory(value, self.root)
                self.assertEqual(raised.exception.status, status)
        with patch("pocket_memory.directory_browser.sys.platform", "win32"):
            with self.assertRaisesRegex(DirectoryBrowseError, "网络或设备"):
                browse_directory("\\\\server\\share", self.root)

    def test_access_denied_is_explained(self):
        with patch("pocket_memory.directory_browser.os.scandir", side_effect=PermissionError):
            with self.assertRaises(DirectoryBrowseError) as raised:
                browse_directory(str(self.root), self.root)
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("权限", str(raised.exception))


class DirectoryBrowserApiTests(unittest.TestCase):
    setUp = server_tests.PocketMemoryServerTests.setUp
    tearDown = server_tests.PocketMemoryServerTests.tearDown
    request = server_tests.PocketMemoryServerTests.request
    request_error = server_tests.PocketMemoryServerTests.request_error

    def test_browse_never_calls_native_picker_or_changes_library(self):
        original = self.store.data_dir
        with patch.object(self.server, "choose_directory", side_effect=AssertionError("no native modal")), \
                patch.object(self.server, "switch_data_dir", side_effect=AssertionError("no switch")), \
                patch.object(self.server, "migrate_data_dir", side_effect=AssertionError("no migrate")):
            status, result = self.request("/api/settings/browse-data-dir", "POST", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["path"], str(original.resolve()))
            status, drives = self.request("/api/settings/browse-data-dir", "POST", {"path": ""})
            self.assertEqual(status, 200)
            self.assertEqual(drives["directories"], drives["roots"])
        self.assertEqual(self.store.data_dir, original)
        self.assertEqual(self.config.data_dir, original)

    def test_cross_origin_request_cannot_list_local_folders(self):
        request = Request(self.base_url + "/api/settings/browse-data-dir", data=b"{}", method="POST",
                          headers={"Content-Type": "application/json", "Origin": "https://example.com"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)

    def test_invalid_path_returns_clear_http_error(self):
        status, result = self.request_error("/api/settings/browse-data-dir", "POST", {"path": "relative"})
        self.assertEqual(status, 400)
        self.assertIn("绝对", result["error"])


if __name__ == "__main__":
    unittest.main()
