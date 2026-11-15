# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
import unittest
from unittest import mock
import test_app_updates as fixtures
from test_app_updates import worker, Response, put, UpdateError


class DesktopTransactionTests(unittest.TestCase):
    setUp = fixtures.AppUpdateTests.setUp
    tearDown = fixtures.AppUpdateTests.tearDown
    installed_fixture = fixtures.AppUpdateTests.installed_fixture
    manifest = fixtures.AppUpdateTests.manifest

    def test_http_success_but_native_failure_rolls_back_before_commit(self):
        install, _, path, _ = self.installed_fixture()
        events = []
        with mock.patch.object(worker, 'wait_for_old_exit'), \
             mock.patch.object(worker, 'spawn_application', return_value=mock.Mock(pid=123)) as spawn, \
             mock.patch.object(worker, 'verify_health', side_effect=lambda *a: events.append('http_ready')), \
             mock.patch.object(worker, 'verify_desktop', side_effect=UpdateError('native DLL failure')), \
             mock.patch.object(worker, 'stop_owned_probe'), mock.patch.object(worker, 'stop_owned_desktop'), \
             mock.patch.object(worker, 'verify_restored_desktop', side_effect=lambda *a: events.append('old_window_ready')):
            result = worker.run_worker(path)
        self.assertEqual(result['state'], 'rolled_back')
        self.assertEqual(events, ['http_ready', 'old_window_ready'])
        self.assertEqual(spawn.call_count, 3)
        self.assertTrue(spawn.call_args_list[1].kwargs['desktop'])
        self.assertEqual((install / 'PocketMemory.exe').read_bytes(), b'fixture old exe')
        self.assertEqual((install / 'data-beta/note.txt').read_bytes(), b'user data remains')

    def test_rollback_without_old_window_is_not_reported_success(self):
        _, _, path, _ = self.installed_fixture()
        with mock.patch.object(worker, 'wait_for_old_exit'), \
             mock.patch.object(worker, 'spawn_application', return_value=mock.Mock(pid=123)), \
             mock.patch.object(worker, 'verify_health', side_effect=UpdateError('bad new health')), \
             mock.patch.object(worker, 'stop_owned_probe'), \
             mock.patch.object(worker, 'verify_restored_desktop', side_effect=UpdateError('old window missing')):
            self.assertEqual(worker.run_worker(path)['state'], 'recovery_required')

    def test_desktop_requires_fresh_identity_and_rendered_page(self):
        _, _, path, request = self.installed_fixture()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        good = dict(state='ready', token=request['health_token'], pid=123, server_pid=124,
                    executable=str(Path(request['install_dir']) / 'PocketMemory.exe'),
                    app_version=request['target_version'], data_dir=request['data_dir'],
                    config_path=request['config_path'], shown=True, page_ready=True,
                    url='http://127.0.0.1:51323')
        health = dict(ok=True, token=request['health_token'], pid=124,
                      app_version=request['target_version'], data_dir=request['data_dir'], config_path=request['config_path'])
        receipt = path.parent / 'desktop-ready.json'
        put(receipt, json.dumps(good).encode())
        with mock.patch.object(worker, '_local_request', return_value=Response(json.dumps(health).encode())) as http:
            self.assertEqual(worker.verify_desktop(request, process, path.parent), good)
            self.assertEqual(http.call_args.args[0]['port'], 51323)
        for change in ({'token':'b'*64}, {'pid':125}, {'app_version':'0.0.0'},
                       {'shown':False}, {'page_ready':False}, {'state':'error', 'reason':'DLL'},
                       {'url':'http://outside.example:51323'}):
            with self.subTest(change=change):
                put(receipt, json.dumps(dict(good, **change)).encode())
                with self.assertRaises(UpdateError), mock.patch.object(worker.time, 'sleep'):
                    worker.verify_desktop(request, process, path.parent, timeout=.002)

    def test_live_commit_rejects_unrelated_or_missing_process(self):
        install, source, _, _ = self.installed_fixture()
        updater = worker.SafeUpdater(install)
        updater.apply(source)
        for actual in ([], [123], [123,124,999]):
            with mock.patch('pocket_memory.safe_update.running_install_processes', return_value=actual):
                with self.assertRaises(UpdateError):
                    updater.commit('native window and data confirmed', desktop_pids=[123,124])
        with mock.patch('pocket_memory.safe_update.running_install_processes', return_value=[124,123]):
            self.assertEqual(updater.commit('native window and data confirmed', desktop_pids=[123,124])['state'], 'committed')

    def test_exited_launcher_still_shuts_down_authenticated_owned_daemon(self):
        _, _, _, request = self.installed_fixture()
        process = mock.Mock(pid=123)
        process.poll.return_value = 1
        health = dict(token=request['health_token'], data_dir=request['data_dir'], config_path=request['config_path'])
        with mock.patch.object(worker, 'desktop_endpoint', return_value=request), \
             mock.patch.object(worker, '_local_request', side_effect=[Response(json.dumps(health).encode()), Response(b'{}')]) as http, \
             mock.patch.object(worker, 'running_install_processes', return_value=[]):
            worker.stop_owned_desktop(request, process)
        self.assertEqual([c.args[1] for c in http.call_args_list], ['/api/update-health', '/api/shutdown'])
        process.terminate.assert_not_called()

    def test_legacy_handoff_finalizes_same_transaction_without_apply(self):
        from pocket_memory import legacy_update_handoff as h
        install, source, path, request = self.installed_fixture()
        updater = worker.SafeUpdater(install)
        initial = updater.apply(source)
        updater.commit('legacy HTTP preflight succeeded')
        info = dict(request=request, marker=dict(transaction=initial['transaction'], legacy_identity={}),
                    work=path.parent, updater=updater, old_version='1.0.0')
        with mock.patch.object(h, 'load_handoff', return_value=info), \
             mock.patch.object(h, 'identity_alive', return_value=False), \
             mock.patch.object(worker, 'running_install_processes', return_value=[]), \
             mock.patch.object(worker, 'spawn_application', return_value=mock.Mock(pid=123)), \
             mock.patch.object(worker, 'verify_desktop', return_value=dict(pid=123,server_pid=124)), \
             mock.patch.object(updater, 'commit', return_value={'state':'committed'}) as commit, \
             mock.patch.object(updater, 'apply', side_effect=AssertionError('No second apply')):
            result = worker.finalize_existing(path)
        self.assertEqual(result['state'], 'complete')
        commit.assert_called_once()
        self.assertEqual(updater._journal()['transaction'], initial['transaction'])

    def test_legacy_committed_update_gui_failure_restores_original(self):
        from pocket_memory import legacy_update_handoff as h
        install, source, path, request = self.installed_fixture()
        updater = worker.SafeUpdater(install)
        initial = updater.apply(source)
        updater.commit('legacy HTTP preflight succeeded')
        info = dict(request=request, marker=dict(transaction=initial['transaction'], legacy_identity={}),
                    work=path.parent, updater=updater, old_version='1.0.0')
        with mock.patch.object(h, 'load_handoff', return_value=info), \
             mock.patch.object(h, 'identity_alive', return_value=False), \
             mock.patch.object(worker, 'running_install_processes', return_value=[]), \
             mock.patch.object(worker, 'spawn_application', return_value=mock.Mock(pid=123)), \
             mock.patch.object(worker, 'verify_desktop', side_effect=UpdateError('native DLL failure')), \
             mock.patch.object(worker, 'stop_owned_desktop'), \
             mock.patch.object(worker, 'verify_restored_desktop') as restored:
            result = worker.finalize_existing(path)
        self.assertEqual(result['state'], 'rolled_back')
        self.assertEqual((install/'PocketMemory.exe').read_bytes(), b'fixture old exe')
        self.assertEqual((install/'data-beta/note.txt').read_bytes(), b'user data remains')
        self.assertEqual(updater._journal()['transaction'], initial['transaction'])
        restored.assert_called_once()


if __name__ == '__main__':
    unittest.main()
