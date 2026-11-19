# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Receipt-lock regression: no network, real models or user installations."""
import ctypes
import errno
import io
import json
import os
from pathlib import Path
import threading
import time
import types
import unittest
from contextlib import ExitStack
from unittest import mock

import test_app_updates as fixtures

updates = fixtures.updates
UpdateError = fixtures.UpdateError


class ReadyAccessTests(unittest.TestCase):
    setUp = fixtures.AppUpdateTests.setUp
    tearDown = fixtures.AppUpdateTests.tearDown
    installed_fixture = fixtures.AppUpdateTests.installed_fixture
    manifest = fixtures.AppUpdateTests.manifest
    zip_fixture = fixtures.AppUpdateTests.zip_fixture
    feed = fixtures.AppUpdateTests.feed

    def clock(self):
        elapsed = [0.0]
        return types.SimpleNamespace(monotonic=lambda: elapsed[0],
                                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    def receipt(self):
        path = self.base / 'ready.json'
        fixtures.put(path, json.dumps({'token': 'test-token', 'pid': 123}).encode())
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        return path, process

    def test_disappearing_then_locked_receipt_recovers(self):
        path, process = self.receipt()
        data = path.read_bytes()
        with mock.patch.object(Path, 'open', side_effect=[FileNotFoundError(), PermissionError(13, 'locked'), io.BytesIO(data)]) as opened, \
             mock.patch.object(updates, 'time', self.clock()):
            updates.wait_for_worker_ready(process, path, 'test-token', self.base / 'helper.exe', timeout=1)
        self.assertEqual(opened.call_count, 3)

    def test_invalid_receipts_and_unexpected_io_fail_closed(self):
        path, process = self.receipt()
        for raw in (b'{', b'[]', b'x' * 4097):
            with self.subTest(raw=raw[:20]):
                path.write_bytes(raw)
                with self.assertRaises((UpdateError, ValueError)):
                    updates.wait_for_worker_ready(process, path, 'test-token', self.base / 'helper.exe')
        with mock.patch.object(Path, 'open', side_effect=OSError(errno.EIO, 'device error')) as opened:
            with self.assertRaises(OSError):
                updates.wait_for_worker_ready(process, path, 'test-token', self.base / 'helper.exe')
            self.assertEqual(opened.call_count, 1)

    def test_token_pid_link_and_worker_exit_protection(self):
        path, process = self.receipt()
        for token, pid in (('wrong', 123), ('test-token', 0)):
            path.write_text(json.dumps({'token': token, 'pid': pid}))
            with mock.patch.object(updates, 'time', self.clock()), self.assertRaisesRegex(UpdateError, '准备超时'):
                updates.wait_for_worker_ready(process, path, 'test-token', self.base / 'helper.exe', timeout=.3)
        with mock.patch.object(updates, '_no_links', side_effect=UpdateError('link blocked')):
            with self.assertRaisesRegex(UpdateError, 'link blocked'):
                updates.wait_for_worker_ready(process, path, 'test-token', self.base / 'helper.exe')
        process.poll.return_value = 1
        with self.assertRaisesRegex(UpdateError, '未能启动'):
            updates.wait_for_worker_ready(process, path, 'test-token', self.base / 'helper.exe')

    def run_handoff(self, module, *, deny_reads=0, deny_go=False, expect_failure=False):
        install, _, _, request = self.installed_fixture()
        fixtures.put(install / module.WORKER_RELATIVE_PATH, fixtures.pe_bytes())
        archive = self.zip_fixture()
        offered = self.feed(payload=archive.read_bytes())
        work = self.base / 'receipt-handoff'
        work.mkdir()
        original = {str(p.relative_to(install)): p.read_bytes() for p in install.rglob('*') if p.is_file()}
        config = types.SimpleNamespace(path=Path(request['config_path']), data_dir=Path(request['data_dir']))
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        real_open = Path.open
        reads = [0]

        def start_worker(*args, **kwargs):
            req = json.loads((work / 'request.json').read_text())
            fixtures.put(work / 'ready.json', json.dumps({'token': req['health_token'], 'pid': process.pid}).encode())
            return process

        def open_file(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            if path == work / 'ready.json' and mode == 'rb':
                reads[0] += 1
                if reads[0] <= deny_reads:
                    raise PermissionError(13, 'fixture locked', str(path))
            return real_open(path, *args, **kwargs)

        real_atomic = module._atomic_json

        def publish(path, payload):
            if deny_go and path.name == 'go.json':
                raise PermissionError(13, 'go write refused', str(path))
            return real_atomic(path, payload)

        with ExitStack() as stack:
            for patch in (
                mock.patch.object(module.sys, 'frozen', True, create=True),
                mock.patch.object(module.sys, 'executable', str(install / 'PocketMemory.exe')),
                mock.patch('pocket_memory.safe_update.running_install_processes', return_value=[os.getpid()]),
                mock.patch.object(module, 'fetch_feed', return_value=offered),
                mock.patch.object(module.tempfile, 'mkdtemp', return_value=str(work)),
                mock.patch.object(module, 'download_package', side_effect=lambda p, dest, cb: dest.write_bytes(archive.read_bytes())),
                mock.patch.object(module.subprocess, 'Popen', side_effect=start_worker),
                mock.patch.object(module, 'time', self.clock()),
                mock.patch.object(module, '_atomic_json', side_effect=publish),
                mock.patch.object(Path, 'open', open_file),
            ):
                stack.enter_context(patch)
            service = module.AppUpdateService(config, config.data_dir, server_port=1234,
                                             exit_callback=mock.Mock(), current_version='1.2.0')
            caught = None
            try:
                service._download_and_handoff(offered, (config.data_dir, config.path, config.data_dir))
            except Exception as exc:
                caught = exc
        if expect_failure:
            self.assertIsNotNone(caught)
            self.assertFalse((work / 'go.json').exists())
            service.exit_callback.assert_not_called()
            self.assertFalse(service.status().get('shutdown_requested', False))
        else:
            if caught:
                raise caught
            self.assertTrue((work / 'go.json').is_file())
            service.exit_callback.assert_called_once()
            self.assertTrue(service.status()['shutdown_requested'])
            process.terminate.assert_not_called()
        for relative, content in original.items():
            self.assertEqual((install / relative).read_bytes(), content)
        self.assertFalse((install / '.pocket-memory-update/journal.json').exists())
        return caught, reads[0], process

    def test_actual_handoff_recovers_from_transient_lock(self):
        _, reads, _ = self.run_handoff(updates, deny_reads=2)
        self.assertEqual(reads, 3)

    def test_persistent_denial_never_signals_go_or_exit(self):
        with self.assertLogs(updates.logger, level='WARNING'):
            caught, reads, process = self.run_handoff(updates, deny_reads=9999, expect_failure=True)
        self.assertIsInstance(caught, UpdateError)
        self.assertIn('持续无法读取', str(caught))
        self.assertLess(reads, 160)
        self.assertIsInstance(caught.__cause__, PermissionError)
        process.terminate.assert_called_once()

    def test_go_write_failure_does_not_mark_app_for_shutdown(self):
        caught, _, process = self.run_handoff(updates, deny_go=True, expect_failure=True)
        self.assertIsInstance(caught, PermissionError)
        process.terminate.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_actual_windows_exclusive_handle_then_release(self):
        from ctypes import wintypes
        path, process = self.receipt()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # Read-only handle, but deny sharing; no ACL or file content changes.
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        release = None
        try:
            # This is the old reader's failure, with an actual Win32 handle.
            with self.assertRaises(PermissionError) as failure:
                path.read_bytes()
            self.assertEqual(failure.exception.errno, errno.EACCES)
            release = threading.Timer(.25, lambda: kernel.CloseHandle(handle))
            release.start()
            started = time.monotonic()
            updates.wait_for_worker_ready(process, path, 'test-token', self.base / 'helper.exe', timeout=3)
            self.assertGreaterEqual(time.monotonic() - started, .2)
        finally:
            if release:
                release.join()
            else:
                kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
