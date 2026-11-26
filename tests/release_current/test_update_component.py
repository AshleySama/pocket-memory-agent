# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Installed-component diagnosis and real HTTP checks; no model or real updater."""
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pocket_memory import app_updates as updates
from pocket_memory.update_component import inspect_worker, require_worker, WORKER_RELATIVE_PATH
from update_component_fixture import pe_bytes


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pm-component-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / WORKER_RELATIVE_PATH
        self.path.parent.mkdir()

    def test_missing_directory_and_valid_component_are_distinct(self):
        self.assertEqual(inspect_worker(self.path)['status'], 'missing')
        self.path.mkdir()
        self.assertEqual(inspect_worker(self.path)['status'], 'not_file')
        self.path.rmdir()
        self.path.write_bytes(pe_bytes())
        result = require_worker(self.path)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['sha256'], hashlib.sha256(pe_bytes()).hexdigest())
        self.assertEqual(result['size'], len(pe_bytes()))

    def test_empty_non_pe_truncated_and_wrong_architecture_fail(self):
        wrong_arch = bytearray(pe_bytes()); wrong_arch[68:70] = b'\x4c\x01'
        wrong_optional = bytearray(pe_bytes()); wrong_optional[88:90] = b'\x0b\x01'
        outside = bytearray(pe_bytes()); outside[0x3c:0x40] = (10000).to_bytes(4, 'little')
        for value in (b'', b'damaged component', pe_bytes()[:90], wrong_arch, wrong_optional, outside):
            with self.subTest(value=bytes(value[:8])):
                self.path.write_bytes(value)
                self.assertEqual(inspect_worker(self.path)['status'], 'invalid_executable')
                with self.assertRaisesRegex(ValueError, '不能交付缺件包'):
                    require_worker(self.path)

    def test_permission_error_is_not_misreported_as_missing(self):
        self.path.write_bytes(pe_bytes())
        with patch.object(Path, 'open', side_effect=PermissionError(13, 'denied')):
            result = inspect_worker(self.path)
        self.assertEqual(result['status'], 'unreadable')
        self.assertEqual(result['errno'], 13)

    def test_parent_reparse_point_is_rejected_before_file_open(self):
        real_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == self.path.parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_nlink=1)
            return real_lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', lstat), patch.object(Path, 'open') as opened:
            self.assertEqual(inspect_worker(self.path)['status'], 'unsafe_path')
        opened.assert_not_called()

    def test_hardlinked_component_rejected(self):
        self.path.write_bytes(pe_bytes())
        os.link(self.path, self.root / 'second.exe')
        self.assertEqual(inspect_worker(self.path)['status'], 'unsafe_path')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pm-component-service-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / 'installed-A'; self.install.mkdir()
        self.helper = self.install / WORKER_RELATIVE_PATH
        self.helper.parent.mkdir()
        self.helper.write_bytes(pe_bytes())
        self.data = self.root/'notes';self.data.mkdir()
        (self.data/'keep.txt').write_text('user notes unchanged', encoding='utf8')
        self.config = SimpleNamespace(path=self.root/'config.json', data_dir=self.data)
        self.config.path.write_text('{}', encoding='utf8')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(self.install/'PocketMemory.exe')):
            self.service = updates.AppUpdateService(self.config, self.data, server_port=1234,
                                exit_callback=Mock(), current_version='1.0.10')
        self.feed = {'published':True,'version':'1.0.11','notes':'unchanged release notes',
                     'package':{'size':150360501,'url':'https://'+updates.TRUSTED_HOST+'/PocketMemory-1.0.11-Program.zip','sha256':'a'*64}}

    def wait(self):
        deadline = time.monotonic()+3
        while self.service._busy and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.service._busy)
        return self.service.status()

    def check(self):
        with patch.object(updates, 'fetch_feed', return_value=self.feed):
            self.service.check_async();return self.wait()

    def assert_unchanged(self):
        self.assertEqual((self.data/'keep.txt').read_text('utf8'), 'user notes unchanged')
        self.assertEqual(self.config.path.read_text('utf8'), '{}')
        self.service.exit_callback.assert_not_called()

    def test_check_reports_missing_before_offering_install_without_exposing_path(self):
        self.helper.unlink()
        with self.assertLogs(updates.logger, level='ERROR') as logs, patch.object(updates,'download_package') as dl:
            result = self.check()
        self.assertEqual(result['state'], 'error');self.assertFalse(result['can_update'])
        # Local missing-component diagnosis precedes even a release-feed fetch.
        self.assertFalse(result['has_update']);self.assertNotIn('latest_version',result)
        self.assertIn('缺失', result['message']);self.assertNotIn('候选尚未打包', result['message'])
        self.assertNotIn(str(self.helper), result['message'])
        self.assertEqual(result['update_component']['status'],'missing')
        self.assertEqual(result['update_component']['server_pid'],os.getpid())
        self.assertNotIn(str(self.install), '\n'.join(logs.output));dl.assert_not_called()
        self.assert_unchanged()

    def test_recheck_after_restoring_file_does_not_clear_failure_pause(self):
        self.helper.unlink()
        self.assertEqual(self.check()['state'],'error')
        self.helper.write_bytes(pe_bytes())
        result=self.check()
        self.assertEqual(result['state'],'error');self.assertFalse(result['can_update'])
        self.assertEqual(result['update_component']['status'],'ok')
        self.assertTrue(result['retry_blocked']);self.assert_unchanged()

    def test_disappears_after_check_blocks_download_and_shutdown(self):
        self.helper.write_bytes(pe_bytes());self.check();self.helper.unlink()
        with patch.object(updates,'download_package') as dl, patch.object(updates,'fetch_feed') as fetch:
            self.service.start_update();result=self.wait()
        dl.assert_not_called();fetch.assert_not_called()
        self.assertEqual(result['state'],'error');self.assertIn('缺失',result['message'])
        self.assert_unchanged()

    def test_existing_other_install_is_not_silently_used(self):
        self.helper.unlink()
        other=self.root/'installed-B'/WORKER_RELATIVE_PATH;other.parent.mkdir(parents=True);other.write_bytes(pe_bytes())
        result=self.check()
        self.assertEqual(result['update_component']['path'],str(self.helper))
        self.assertFalse(result['can_update']);self.assert_unchanged()

    def test_unreadable_and_invalid_components_get_accurate_messages(self):
        self.helper.write_bytes(b'corrupt')
        self.assertIn('不完整或格式异常',self.check()['message'])
        self.helper.write_bytes(pe_bytes())
        with patch('pocket_memory.update_component.Path.open', side_effect=PermissionError(13,'denied')):
            result=self.check()
        # First failure remains recorded; read-only recheck reports the new observation.
        self.assertTrue(result['retry_blocked'])
        self.assertEqual(result['update_component']['status'],'unreadable');self.assert_unchanged()

    def test_known_good_component_reaches_downloader(self):
        self.helper.write_bytes(pe_bytes());self.check()
        class Reached(Exception):pass
        stage=self.root/'staging';stage.mkdir()
        with patch.object(updates,'fetch_feed',return_value=self.feed), \
                patch.object(updates.tempfile,'mkdtemp',return_value=str(stage)), \
                patch.object(updates,'download_package',side_effect=Reached) as dl:
            with self.assertRaises(Reached):
                self.service._download_and_handoff(self.feed,(self.data,self.config.path,self.data))
        dl.assert_called_once();self.assert_unchanged()

    def test_http_recheck_keeps_pause_even_if_file_reappears(self):
        self.helper.unlink()
        from pocket_memory.server import PocketMemoryHandler
        from test_update_integration import http
        server=ThreadingHTTPServer(('127.0.0.1',0),PocketMemoryHandler)
        server.operation_lock=threading.RLock();server._closing=False
        server.store=SimpleNamespace(data_dir=self.data);server.config=self.config
        server.app_updates=self.service;server.update_blocks_writes=lambda:False
        server.ask_cancellations=SimpleNamespace(finish=lambda _:None)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(updates,'fetch_feed',return_value=self.feed),patch.object(updates,'download_package') as dl:
                code,_=http(server.server_port,'/api/app-update/check',method='POST',body=b'{}',headers={'X-PocketMemory-Update':'1'})
                self.assertEqual(code,200);self.wait()
                code,result=http(server.server_port,'/api/app-update')
                self.assertEqual(code,200);self.assertEqual(result['state'],'error')
                self.assertIn('停止重复',result['message']);self.assertFalse(result['can_update'])
                self.assertEqual(result['diagnostic']['failure']['code'],'missing')
                self.helper.write_bytes(pe_bytes())
                http(server.server_port,'/api/app-update/check',method='POST',body=b'{}',headers={'X-PocketMemory-Update':'1'})
                self.wait();_,result=http(server.server_port,'/api/app-update')
                self.assertFalse(result['can_update']);self.assertTrue(result['retry_blocked']);dl.assert_not_called()
        finally:
            server.shutdown();server.server_close();thread.join(3)
        self.assert_unchanged()


if __name__=='__main__':unittest.main()
