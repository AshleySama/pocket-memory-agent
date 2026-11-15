# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Exercise real enumeration code with deterministic Win32 lifetime boundaries."""
import ctypes
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import test_safe_update as fixtures

up = fixtures.up

class KernelFixture:
    def __init__(self, entries, next_error=18):
        self.entries, self.index, self.next_error = entries, -1, next_error
        self.error, self.closed = 0, []
        self.CreateToolhelp32Snapshot = Mock(return_value=999)
        self.Process32FirstW = Mock(side_effect=self.advance)
        self.Process32NextW = Mock(side_effect=self.advance)
        self.OpenProcess = Mock(side_effect=self.open)
        self.QueryFullProcessImageNameW = Mock(side_effect=self.path)
        self.GetExitCodeProcess = Mock(side_effect=self.exit_code)
        self.CloseHandle = Mock(side_effect=lambda h: self.closed.append(h) or 1)

    def advance(self, snapshot, pointer):
        self.index += 1
        if self.index >= len(self.entries):
            self.error = self.next_error
            return 0
        row = self.entries[self.index]
        pointer._obj.szExeFile = row.get('name', 'PocketMemory.exe')
        pointer._obj.th32ProcessID = row['pid']
        return 1

    def row(self, pid):
        return next(x for x in self.entries if x['pid'] == pid)

    def open(self, flags, inherit, pid):
        self.error = self.row(pid).get('open_error', 0)
        return 0 if self.error else pid

    def path(self, handle, flags, buffer, size):
        row = self.row(handle)
        self.error = row.get('query_error', 0)
        if self.error: return 0
        buffer.value = row['path']
        size._obj.value = len(buffer.value)
        return 1

    def exit_code(self, handle, code):
        row = self.row(handle)
        self.error = row.get('exit_error', 0)
        if self.error: return 0
        code._obj.value = row.get('exit_code', 259)
        return 1

@unittest.skipUnless(os.name == 'nt', 'Windows update process enumeration')
class ExitRaceTests(unittest.TestCase):
    def scan(self, rows, *, next_error=18):
        kernel = KernelFixture(rows, next_error)
        self.kernel = kernel
        with patch.object(up.ctypes, 'WinDLL', return_value=kernel), \
             patch.object(up.ctypes, 'get_last_error', side_effect=lambda: kernel.error), \
             patch.object(up, '_absolute', side_effect=lambda p: Path(p).absolute()):
            return up.running_install_processes(Path('C:/isolated/installation'))

    def test_exited_handle_path_failure_does_not_abort_update(self):
        self.assertEqual(self.scan([{'pid': 71, 'query_error': 87, 'exit_code': 0}]), [])
        self.assertEqual(self.kernel.closed, [71, 999])

    def test_active_path_failure_still_blocks_and_records_diagnostics(self):
        with self.assertRaisesRegex(up.UpdateError, r'PID=72.*WinError=5.*259'):
            self.scan([{'pid': 72, 'query_error': 5, 'exit_code': 259}])
        self.assertEqual(self.kernel.closed, [72, 999])

    def test_unreadable_exit_status_still_blocks(self):
        with self.assertRaisesRegex(up.UpdateError, r'PID=73.*exit_error=5'):
            self.scan([{'pid': 73, 'query_error': 87, 'exit_error': 5}])
        self.assertEqual(self.kernel.closed, [73, 999])

    def test_missing_snapshot_pid_can_be_skipped(self):
        self.assertEqual(self.scan([{'pid': 74, 'open_error': 87}]), [])
        self.assertEqual(self.kernel.closed, [999])

    def test_access_denied_or_unknown_open_failure_is_not_ignored(self):
        for error in (5, 8, 6):
            with self.subTest(error=error), self.assertRaises(up.UpdateError):
                self.scan([{'pid': 75, 'open_error': error}])
            self.assertEqual(self.kernel.closed, [999])

    def test_live_same_install_and_backup_block_but_other_install_does_not(self):
        rows = [
            {'pid': 81, 'path': 'C:/isolated/installation/PocketMemory.exe'},
            {'pid': 82, 'path': 'C:/elsewhere/PocketMemory.exe'},
            {'pid': 83, 'path': 'C:/isolated/installation/.pocket-memory-update/abc/backup/PocketMemory.exe'},
        ]
        self.assertEqual(self.scan(rows), [81, 83])
        self.assertEqual(self.kernel.closed, [81, 82, 83, 999])

    def test_dead_snapshot_does_not_hide_later_live_process(self):
        self.assertEqual(self.scan([
            {'pid': 91, 'query_error': 87, 'exit_code': 0},
            {'pid': 92, 'path': 'C:/isolated/installation/PocketMemory.exe'},
        ]), [92])

    def test_incomplete_snapshot_is_not_accepted_as_no_running_process(self):
        with self.assertRaisesRegex(up.UpdateError, 'process list'):
            self.scan([{'pid': 1, 'name': 'Unrelated.exe'}], next_error=5)

if __name__ == '__main__': unittest.main()
