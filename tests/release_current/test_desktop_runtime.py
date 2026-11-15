# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory import desktop_runtime as runtime


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.exe = self.folder / 'PocketMemory.exe'
        self.config = self.folder / 'PocketMemory.exe.config'
        self.patch = mock.patch.multiple(runtime.sys, frozen=True, executable=str(self.exe), platform='win32', create=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_first_run_and_repeat_preserve_exact_config(self):
        self.assertEqual(runtime.ensure_native_runtime_config(), self.config)
        stamp = self.config.stat().st_mtime_ns
        self.assertEqual(self.config.read_bytes(), runtime.CONFIG)
        runtime.ensure_native_runtime_config()
        self.assertEqual(stamp, self.config.stat().st_mtime_ns)
        self.assertEqual(list(self.folder.glob('*.tmp')), [])

    def test_source_python_is_never_modified(self):
        with mock.patch.object(runtime.sys, 'frozen', False):
            self.assertIsNone(runtime.ensure_native_runtime_config())
        self.assertFalse(self.config.exists())

    def test_existing_custom_compatible_config_is_preserved(self):
        data = b'<configuration><appSettings><add key="x" value="y"/></appSettings><runtime><loadFromRemoteSources enabled="true"/></runtime></configuration>'
        self.config.write_bytes(data)
        runtime.ensure_native_runtime_config()
        self.assertEqual(self.config.read_bytes(), data)

    def test_corruption_and_conflict_are_not_overwritten(self):
        for data in (b'not xml', b'<configuration/>', runtime.CONFIG.replace(b'true', b'false')):
            with self.subTest(data=data):
                self.config.write_bytes(data)
                with self.assertRaises(RuntimeError):runtime.ensure_native_runtime_config()
                self.assertEqual(data, self.config.read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'Windows atomic no-overwrite rename semantics')
    def test_simultaneous_first_launches_produce_one_complete_config(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            values=list(pool.map(lambda _:runtime.ensure_native_runtime_config(),range(12)))
        self.assertEqual(set(values),{self.config})
        self.assertEqual(self.config.read_bytes(),runtime.CONFIG)
        self.assertFalse(list(self.folder.glob('*.tmp')))

    def test_user_data_and_download_mark_are_untouched(self):
        user=self.folder/'config.json'; user.write_bytes(b'private settings')
        dll=self.folder/'Python.Runtime.dll'; dll.write_bytes(b'fixture')
        marker=Path(str(dll)+':Zone.Identifier')
        marker.write_bytes(b'[ZoneTransfer]\nZoneId=3\n')
        runtime.ensure_native_runtime_config()
        self.assertEqual(user.read_bytes(),b'private settings')
        self.assertEqual(marker.read_bytes(),b'[ZoneTransfer]\nZoneId=3\n')

    def test_read_only_directory_reports_error_without_partial_config(self):
        with mock.patch.object(runtime.tempfile,'NamedTemporaryFile',side_effect=PermissionError('read only')):
            with self.assertRaisesRegex(RuntimeError,'写入权限'):runtime.ensure_native_runtime_config()
        self.assertFalse(self.config.exists())


if __name__=='__main__':unittest.main()
