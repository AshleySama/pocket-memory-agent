# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Desktop readiness is a native-window + frontend + storage identity contract."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pocket_memory import desktop_readiness as dr
from pocket_memory.safe_update import UpdateError

spec = importlib.util.spec_from_file_location('readiness_app', ROOT / 'app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
URL = 'http://127.0.0.1:51239'


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


class DesktopReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.install = base / 'install'
        self.data = base / 'library'
        self.work = base / 'update-work'
        for path in (self.install, self.data, self.work):
            path.mkdir()
        self.exe = self.install / 'PocketMemory.exe'
        self.exe.write_bytes(b'fixture')
        self.config = base / 'user-config.json'
        self.config.write_text('{}', encoding='utf-8')
        self.receipt = self.work / 'desktop-ready.json'
        self.env = dict(PM_UPDATE_DESKTOP_TOKEN='a' * 64,
                        PM_UPDATE_DESKTOP_RECEIPT=str(self.receipt),
                        PM_UPDATE_DESKTOP_DATA_DIR=str(self.data),
                        PM_UPDATE_CONFIG_PATH=str(self.config))
        self.stack = []
        for patcher in (mock.patch.dict(os.environ, self.env, clear=True),
                        mock.patch.object(dr, 'app_directory', return_value=self.install),
                        mock.patch.object(dr.sys, 'executable', str(self.exe))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def create(self):
        monitor = dr.DesktopReadiness.from_environment(URL)
        window = types.SimpleNamespace(events=types.SimpleNamespace(shown=Event(), loaded=Event()),
                                       evaluate_js=mock.Mock(return_value=True), destroy=mock.Mock(),
                                       native=None)
        monitor.attach(window)
        return monitor, window

    def health(self, **changes):
        value = dict(ok=True, token='a' * 64, app_version=dr.APP_VERSION,
                     pid=os.getpid() + 100, data_dir=str(self.data), config_path=str(self.config))
        value.update(changes)
        return value

    def read(self):
        return json.loads(self.receipt.read_text('utf-8'))

    def test_normal_launch_is_noop_and_config_override_alone_is_not_an_update(self):
        with mock.patch.dict(os.environ, {'PM_UPDATE_CONFIG_PATH': str(self.config)}, clear=True):
            self.assertIsNone(dr.DesktopReadiness.from_environment(URL))

    def test_missing_or_invalid_flags_never_write_a_receipt(self):
        for name in self.env:
            with self.subTest(missing=name), mock.patch.dict(os.environ, self.env, clear=True):
                del os.environ[name]
                with self.assertRaises(UpdateError):
                    dr.DesktopReadiness.from_environment(URL)
        with mock.patch.dict(os.environ, {'PM_UPDATE_DESKTOP_TOKEN': 'secret-wrong-format'}):
            with self.assertRaises(UpdateError):
                dr.DesktopReadiness.from_environment(URL)
        self.assertFalse(self.receipt.exists())

    def test_receipt_cannot_replace_user_file_or_live_install_content(self):
        targets = (self.data / 'desktop-ready.json', self.install / 'desktop-ready.json',
                   self.work / 'config.json', self.work / 'missing' / 'desktop-ready.json')
        for target in targets:
            with self.subTest(target=target), mock.patch.dict(os.environ, {'PM_UPDATE_DESKTOP_RECEIPT': str(target)}):
                with self.assertRaises(UpdateError):
                    dr.DesktopReadiness.from_environment(URL)
        self.receipt.write_text('existing user content', encoding='utf-8')
        with self.assertRaises(UpdateError):
            dr.DesktopReadiness.from_environment(URL)
        self.assertEqual(self.receipt.read_text('utf-8'), 'existing user content')

    def test_only_local_nonredirecting_health_urls_are_accepted(self):
        for url in ('https://127.0.0.1:51239', 'http://example.com:51239',
                    'http://user@127.0.0.1:51239', URL + '/other', URL + '?key=1'):
            with self.subTest(url=url), self.assertRaises(UpdateError):
                dr.DesktopReadiness.from_environment(url)

    def test_loaded_does_not_imply_native_window_was_shown(self):
        monitor, window = self.create()
        window.events.loaded.fire()
        with mock.patch.object(monitor, '_health') as health:
            monitor.run(timeout=.01, poll_interval=.001)
        window.evaluate_js.assert_not_called()
        health.assert_not_called()
        self.assertEqual(self.read()['state'], 'error')
        self.assertFalse(self.read()['shown'])

    def test_shown_alone_does_not_imply_document_loaded(self):
        monitor, window = self.create()
        window.events.shown.fire()
        monitor.run(timeout=.01, poll_interval=.001)
        window.evaluate_js.assert_not_called()
        self.assertEqual(self.read()['state'], 'error')

    def test_shown_loaded_and_initial_refresh_are_all_required_in_either_order(self):
        for events in (('shown', 'loaded'), ('loaded', 'shown')):
            with self.subTest(events=events):
                if self.receipt.exists():
                    self.receipt.unlink()
                monitor, window = self.create()
                for event in events:
                    getattr(window.events, event).fire()
                with mock.patch.object(monitor, '_health', return_value=self.health()):
                    monitor.run()
                receipt = self.read()
                self.assertEqual(receipt['state'], 'ready')
                self.assertTrue(receipt['shown'])
                self.assertTrue(receipt['page_ready'])
                self.assertEqual(receipt['pid'], os.getpid())
                self.assertEqual(Path(receipt['executable']), self.exe)
                window.destroy.assert_not_called()
                # Closing an already accepted desktop must not revoke its receipt.
                monitor.close()
                self.assertEqual(self.read(), receipt)

    def test_false_js_marker_cannot_pass_with_healthy_server(self):
        monitor, window = self.create()
        window.events.shown.fire()
        window.events.loaded.fire()
        window.evaluate_js.return_value = False
        with mock.patch.object(monitor, '_health') as health:
            monitor.run(timeout=.01, poll_interval=.001)
        health.assert_not_called()
        self.assertEqual(self.read()['state'], 'error')
        window.destroy.assert_called_once()

    def test_initial_refresh_can_finish_after_loaded_event(self):
        monitor, window = self.create()
        window.events.shown.fire()
        window.events.loaded.fire()
        window.evaluate_js.side_effect = [False, True]
        with mock.patch.object(monitor, '_health', return_value=self.health()):
            monitor.run(poll_interval=.001)
        self.assertEqual(window.evaluate_js.call_count, 2)
        self.assertEqual(self.read()['state'], 'ready')

    def test_transient_health_timeout_retries_without_skipping_validation(self):
        monitor, window = self.create()
        window.events.shown.fire(); window.events.loaded.fire()
        with mock.patch.object(monitor, '_health', side_effect=[TimeoutError(), self.health()]) as health:
            monitor.run(timeout=1, poll_interval=.001)
        self.assertEqual(health.call_count, 2)
        self.assertLessEqual(health.call_args.kwargs['timeout'], 1)
        self.assertEqual(self.read()['state'], 'ready')

    def test_persistent_timeout_is_bounded_and_http_denial_is_not_retried(self):
        from urllib.error import HTTPError
        for error, retries in ((TimeoutError(), True), (HTTPError(URL, 403, 'denied', {}, None), False)):
            with self.subTest(error=type(error).__name__):
                if self.receipt.exists(): self.receipt.unlink()
                monitor, window = self.create()
                window.events.shown.fire(); window.events.loaded.fire()
                with mock.patch.object(monitor, '_health', side_effect=error) as health:
                    monitor.run(timeout=.02, poll_interval=.001)
                self.assertEqual(self.read()['state'], 'error')
                if retries: self.assertGreater(health.call_count, 1)
                else: self.assertEqual(health.call_count, 1)
                window.destroy.assert_called_once()

    def test_wrong_server_version_token_pid_or_storage_cannot_pass(self):
        changes = ({'token': 'b' * 64}, {'app_version': '0.0.0'}, {'pid': os.getpid()},
                   {'pid': '123'}, {'ok': False}, {'data_dir': str(self.install)},
                   {'config_path': str(self.exe)})
        for change in changes:
            with self.subTest(change=change):
                if self.receipt.exists():
                    self.receipt.unlink()
                monitor, window = self.create()
                window.events.shown.fire()
                window.events.loaded.fire()
                with mock.patch.object(monitor, '_health', return_value=self.health(**change)):
                    monitor.run()
                self.assertEqual(self.read()['state'], 'error')
                self.assertNotIn('server_pid', self.read())
                window.destroy.assert_called_once()

    def test_native_failure_reports_without_blocking_messagebox_or_browser(self):
        window = types.SimpleNamespace(events=types.SimpleNamespace(shown=Event(), loaded=Event()),
                                       destroy=mock.Mock(), native=None)
        webview = types.SimpleNamespace(create_window=mock.Mock(return_value=window),
                                        start=mock.Mock(side_effect=RuntimeError('native DLL failure')))
        with mock.patch.dict(sys.modules, {'webview': webview}), \
             mock.patch.object(app, 'ensure_native_runtime_config'), \
             mock.patch.object(app, 'is_webview2_installed', return_value=True), \
             mock.patch.object(app, 'find_app_icon', return_value=None), \
             mock.patch.object(app, 'show_native_startup_error') as dialog, \
             mock.patch.object(app, 'open_browser_session') as browser:
            self.assertFalse(app.open_webview(URL, False))
        dialog.assert_not_called()
        browser.assert_not_called()
        self.assertEqual(self.read()['reason'], 'native_startup_failed')
        self.assertEqual(self.read()['state'], 'error')


if __name__ == '__main__':
    unittest.main()
