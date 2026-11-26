# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Required-component lifecycle: inert transactions + real local HTTP, no model/build."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

import test_safe_update as fixtures
import test_app_updates as appfixtures
from test_app_updates import worker
from test_update_integration import http
from update_component_fixture import pe_bytes
from pocket_memory import app_updates, update_probe
from pocket_memory.update_component import WORKER_RELATIVE_PATH as REL, inspect_installed_worker
from pocket_memory.server import PocketMemoryHandler


class TransactionIntegrityTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    tearDown = fixtures.SafetyTests.tearDown
    fixture = fixtures.SafetyTests.fixture
    write_manifest = fixtures.SafetyTests.write_manifest
    assert_original = fixtures.SafetyTests.assert_original

    def test_both_manifest_and_payload_omission_refused_before_mutation(self):
        install, source, manifest = self.fixture()
        manifest['files'] = [x for x in manifest['files'] if x['path'] != REL]
        (source/'payload'/REL).unlink()
        self.write_manifest(source, manifest)
        updater = fixtures.up.SafeUpdater(install)
        for method in (updater.check, updater.apply):
            with self.assertRaisesRegex(fixtures.up.UpdateError, '必需'):
                method(source)
        self.assert_original(install)
        self.assertFalse(updater.receipt_path.exists())

    def test_declared_missing_or_hash_matched_invalid_component_refused(self):
        for mode in ('missing', 'invalid', 'wrong_case'):
            with self.subTest(mode=mode):
                install, source, manifest = self.fixture(mode)
                item = next(x for x in manifest['files'] if x['path'] == REL)
                if mode == 'missing':
                    (source/'payload'/REL).unlink()
                elif mode == 'invalid':
                    (source/'payload'/REL).write_bytes(b'not an executable')
                    item.update(fixtures.entry(REL, b'not an executable'))
                else:
                    item['path'] = REL.lower()
                self.write_manifest(source, manifest)
                with self.assertRaises(fixtures.up.UpdateError):
                    fixtures.up.SafeUpdater(install).apply(source)
                self.assert_original(install)

    def test_success_checks_commits_and_preserves_backup(self):
        install, source, _ = self.fixture()
        updater = fixtures.up.SafeUpdater(install)
        result = updater.apply(source)
        self.assertEqual(inspect_installed_worker(install)['status'], 'ok')
        self.assertEqual(inspect_installed_worker(install)['verification'], 'installed_manifest')
        self.assertEqual(updater.commit('Inert fixture verified, no native EXE launched')['state'], 'committed')
        self.assertEqual((updater.state/result['transaction']/'backup'/REL).read_bytes(), pe_bytes()+b'old')
        updater.rollback()
        self.assert_original(install)

    def test_disappearance_or_replacement_before_commit_rejected_and_rollback_works(self):
        for mode in ('missing', 'changed'):
            with self.subTest(mode=mode):
                install, source, _ = self.fixture(mode)
                updater = fixtures.up.SafeUpdater(install)
                updater.apply(source)
                if mode == 'missing': (install/REL).unlink()
                else: (install/REL).write_bytes(pe_bytes()+b'bad')
                self.assertEqual(inspect_installed_worker(install)['status'], 'missing' if mode == 'missing' else 'integrity_mismatch')
                with self.assertRaises(fixtures.up.UpdateError):
                    updater.commit('Must not confirm missing or changed component')
                self.assertEqual(updater.recover()['state'], 'pending_health')
                self.assertEqual(updater.rollback()['state'], 'rolled_back')
                self.assert_original(install)

    def test_component_lost_during_replacement_rolls_back_automatically(self):
        install, source, _ = self.fixture()
        updater = fixtures.up.SafeUpdater(install)
        original = updater._perform
        def perform(journal, operation):
            original(journal, operation)
            if operation == {'kind':'install', 'path':'PocketMemory.exe'}:
                (install/REL).unlink()
        with mock.patch.object(updater, '_perform', side_effect=perform):
            with self.assertRaisesRegex(fixtures.up.UpdateError, 'rolled back'):
                updater.apply(source)
        self.assert_original(install)
        self.assertEqual(updater.recover()['state'], 'rolled_back')

    def test_old_incomplete_install_can_be_restored_without_new_guard_blocking_rollback(self):
        install, source, _ = self.fixture()
        (install/REL).unlink()  # A legacy incomplete installation is not made un-recoverable.
        updater = fixtures.up.SafeUpdater(install)
        updater.apply(source)
        self.assertEqual(updater.rollback()['state'], 'rolled_back')
        self.assertFalse((install/REL).exists())
        self.assertEqual((install/'PocketMemory.exe').read_bytes(), b'old exe')
        self.assertEqual((install/'data/notes/keep.txt').read_bytes(), b'user note')

    def test_corrupt_or_incomplete_receipt_does_not_downgrade_to_pe_only(self):
        install, source, manifest = self.fixture()
        updater = fixtures.up.SafeUpdater(install)
        updater.apply(source)
        receipt = json.loads(updater.receipt_path.read_text())
        receipt['manifest']['files'] = [x for x in manifest['files'] if x['path'] != REL]
        updater.receipt_path.write_text(json.dumps(receipt))
        self.assertEqual(inspect_installed_worker(install)['status'], 'invalid_receipt')
        with self.assertRaises(fixtures.up.UpdateError): updater.commit('Not a complete receipt')
        updater.rollback()
        self.assert_original(install)


class StartupIntegrityTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    tearDown = fixtures.SafetyTests.tearDown

    def service(self, valid=False):
        install = self.base/'installation'; (install/'_internal').mkdir(parents=True)
        (install/'PocketMemory.exe').write_bytes(b'inert fixture')
        if valid: (install/REL).write_bytes(pe_bytes())
        data = self.base/'data'; data.mkdir()
        config = SimpleNamespace(path=self.base/'config.json', data_dir=data)
        config.path.write_text('{}')
        with mock.patch.object(sys, 'frozen', True, create=True), mock.patch.object(sys, 'executable', str(install/'PocketMemory.exe')):
            service = app_updates.AppUpdateService(config, data, server_port=1234, exit_callback=mock.Mock())
        return service, install, config

    def test_startup_is_local_and_failed_check_cannot_be_hidden_by_feed(self):
        with mock.patch.object(app_updates, 'fetch_feed', side_effect=AssertionError('network not allowed')) as fetch:
            service, install, _ = self.service()
            status = service.status()
            self.assertEqual(status['state'], 'error')
            self.assertEqual(status['recovery']['action'], 'pause_and_diagnose')
            self.assertIn('保留原安装', ''.join(status['recovery']['steps']))
            service.check_async()
            for _ in range(300):
                if not service._busy: break
                time.sleep(.01)
            self.assertFalse(service._busy)
            self.assertEqual(service.status()['installation_integrity']['status'], 'missing')
            fetch.assert_not_called()
        (install/REL).write_bytes(pe_bytes())
        self.assertEqual(service.refresh_installation_integrity()['status'], 'ok')
        self.assertEqual(service.status()['recovery']['action'],'pause_and_diagnose')
        service.exit_callback.assert_not_called()

    def test_liveness_and_authenticated_health_are_separate_and_detect_late_loss(self):
        service, install, config = self.service(valid=True)
        server = ThreadingHTTPServer(('127.0.0.1',0), PocketMemoryHandler)
        server.app_updates=service; server._update_health_ready=True
        server.store=SimpleNamespace(data_dir=config.data_dir);server.config=config
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        token='a'*64
        try:
            with mock.patch.dict(os.environ, PM_UPDATE_HEALTH_TOKEN=token):
                _, value=http(server.server_port,'/api/update-health',headers={'X-Pocket-Update-Token':token})
                self.assertTrue(value['ok'])
                (install/REL).unlink()
                _, value=http(server.server_port,'/api/update-health',headers={'X-Pocket-Update-Token':token})
                self.assertFalse(value['ok'])
                self.assertEqual(value['installation_integrity']['status'],'missing')
                _, live=http(server.server_port,'/api/status')
                self.assertTrue(live['ok']);self.assertFalse(live['update_ready'])
                self.assertEqual(http(server.server_port,'/api/update-health')[0],403)
        finally:
            server.shutdown();server.server_close();thread.join(3)

    def test_actual_readonly_probe_route_rechecks_worker_and_preserves_database(self):
        _, install, config = self.service(valid=True)
        database=config.data_dir/'pocket_memory.db'
        with sqlite3.connect(database) as db: db.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY, content TEXT)')
        db.close()
        original=database.read_bytes()
        frontend=self.base/'frontend';frontend.mkdir()
        for name in ('index.html','app.js','app-update.js','app-update.css'):(frontend/name).write_text('fixture')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        token='c'*64
        with mock.patch.object(sys,'frozen',True,create=True), mock.patch.object(sys,'executable',str(install/'PocketMemory.exe')), \
             mock.patch.dict(os.environ,PM_UPDATE_HEALTH_TOKEN=token), mock.patch('pocket_memory.legacy_update_handoff.stage_handoff'):
            thread=threading.Thread(target=update_probe.run_probe,args=(config,config.data_dir,frontend,'127.0.0.1',port),daemon=True);thread.start()
            try:
                for _ in range(200):
                    try: _, value=http(port,'/api/update-health',headers={'X-Pocket-Update-Token':token});break
                    except OSError:time.sleep(.01)
                self.assertTrue(value['ok'])
                (install/REL).unlink()
                _, value=http(port,'/api/update-health',headers={'X-Pocket-Update-Token':token})
                self.assertFalse(value['ok']);self.assertEqual(value['installation_integrity']['status'],'missing')
                with self.assertRaisesRegex(ValueError,'官方完整包'):
                    update_probe.check_inputs(config,config.data_dir,frontend)
            finally:
                http(port,'/api/shutdown',method='POST',body=b'{}',headers={'X-Pocket-Update-Token':token})
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(database.read_bytes(),original)


class WorkerRollbackTests(unittest.TestCase):
    setUp=appfixtures.AppUpdateTests.setUp
    tearDown=appfixtures.AppUpdateTests.tearDown
    installed_fixture=appfixtures.AppUpdateTests.installed_fixture
    manifest=appfixtures.AppUpdateTests.manifest

    def test_real_worker_orchestrator_rolls_back_if_component_lost_after_health(self):
        install, _, path, _ = self.installed_fixture()
        old=pe_bytes()+b'old';(install/REL).write_bytes(old)
        def desktop(*args):
            (install/REL).unlink()
            return {'pid':123,'server_pid':124}
        with mock.patch.object(worker,'wait_for_old_exit'), mock.patch.object(worker,'spawn_application',return_value=mock.Mock(pid=123)), \
             mock.patch.object(worker,'verify_health'), mock.patch.object(worker,'verify_desktop',side_effect=desktop), \
             mock.patch.object(worker,'stop_owned_probe'),mock.patch.object(worker,'stop_owned_desktop'), \
             mock.patch.object(worker,'verify_restored_desktop'),mock.patch('pocket_memory.safe_update.running_install_processes',return_value=[]) as procs:
            # Only the live commit identity query has a running desktop; no actual EXEs.
            original_commit=worker.SafeUpdater.commit
            def commit(updater,*args,**kwargs):
                with mock.patch('pocket_memory.safe_update.running_install_processes',return_value=[123,124]):
                    return original_commit(updater,*args,**kwargs)
            with mock.patch.object(worker.SafeUpdater,'commit',commit): result=worker.run_worker(path)
        self.assertEqual(result['state'],'rolled_back')
        self.assertEqual((install/REL).read_bytes(),old)
        self.assertEqual((install/'data-beta/note.txt').read_bytes(),b'user data remains')


if __name__ == '__main__':unittest.main()
