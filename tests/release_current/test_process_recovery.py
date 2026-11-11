# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Actual child-process exit and Windows file sharing denial on disposable data."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_safe_update as fixtures
up, MODULE = fixtures.up, fixtures.MODULE


class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture_owner = fixtures.SafetyTests()
        self.fixture_owner.setUp()

    def tearDown(self):
        self.fixture_owner.tearDown()

    def test_actual_process_exits_after_runtime_rename_then_recovers(self):
        install, source, _ = self.fixture_owner.fixture('real-process-exit')
        code = '''import importlib.util,sys,os
from pathlib import Path
s=importlib.util.spec_from_file_location("update_child",sys.argv[1])
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
u=m.SafeUpdater(Path(sys.argv[2])); original=u._move
def move(a,b):
    original(a,b)
    if a.parent.name=="stage" and a.name=="_internal": os._exit(86)
u._move=move
u.apply(Path(sys.argv[3]))
'''
        child = subprocess.run([sys.executable, '-c', code, str(MODULE), str(install), str(source)],
                               capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 86, child.stderr)
        self.assertFalse((install/'PocketMemory.exe').exists())
        self.assertEqual((install/'_internal/runtime.bin').read_bytes(), b'new runtime')
        updater = up.SafeUpdater(install)
        self.assertEqual(updater.recover()['state'], 'rolled_back')
        self.assertEqual(updater.recover()['state'], 'rolled_back')
        self.fixture_owner.assert_original(install)

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing check')
    def test_actual_windows_locked_executable_preserves_install(self):
        install, source, _ = self.fixture_owner.fixture('real-file-lock')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        create.restype = ctypes.c_void_p
        close = kernel.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        handle = create(str(install/'PocketMemory.exe'), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(up.UpdateError):
                up.SafeUpdater(install).apply(source)
            self.fixture_owner.assert_original(install)
        finally:
            close(handle)
        self.assertEqual(up.SafeUpdater(install).recover()['state'], 'rolled_back')


if __name__ == '__main__':
    unittest.main(verbosity=2)
