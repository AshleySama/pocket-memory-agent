# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Native startup must stay native and configure the host before CLR loads."""
import importlib.util
import io
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('desktop_startup_app', ROOT / 'app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
URL = 'http://127.0.0.1:51239'


class DesktopStartupTests(unittest.TestCase):
    def fake_webview(self):
        window = types.SimpleNamespace(destroy=mock.Mock(), native=None)
        return types.SimpleNamespace(create_window=mock.Mock(return_value=window), start=mock.Mock(),
                                     settings={'ALLOW_DOWNLOADS': False})

    def run_native(self, webview):
        with mock.patch.dict(sys.modules, {'webview': webview}), \
             mock.patch.object(app, 'is_webview2_installed', return_value=True), \
             mock.patch.object(app, 'find_app_icon', return_value=None), \
             mock.patch.object(app, 'open_browser_session') as browser, \
             mock.patch.object(app, 'spawn_server_process') as spawn, \
             mock.patch.object(app, 'ensure_native_runtime_config') as prepare, \
             mock.patch.object(app, 'show_native_startup_error') as error:
            result = app.open_webview(URL, False)
            spawn.assert_not_called()
            prepare.assert_called_once()
            self.last_result, self.last_error = result, error
            return browser

    def test_colleague_clr_failure_never_silently_switches_to_browser(self):
        webview = self.fake_webview()
        webview.start.side_effect = RuntimeError('Failed to resolve Python.Runtime.Loader.Initialize from Python.Runtime.dll')
        with self.assertLogs(app.logger, level='ERROR') as logs:
            browser = self.run_native(webview)
        browser.assert_not_called()
        self.assertIn('Python.Runtime.Loader.Initialize', '\n'.join(logs.output))
        self.assertFalse(self.last_result)
        self.last_error.assert_called_once()

    def test_create_window_and_renderer_failures_stay_desktop(self):
        for stage in ('create_window', 'start'):
            for error in (OSError('runtime unavailable'), ImportError('missing managed DLL')):
                with self.subTest(stage=stage, error=type(error).__name__):
                    webview = self.fake_webview()
                    getattr(webview, stage).side_effect = error
                    self.run_native(webview).assert_not_called()

    def test_missing_webview_has_clear_desktop_error(self):
        self.run_native(None).assert_not_called()

    def test_missing_webview2_does_not_attempt_native_start_or_browser(self):
        webview = self.fake_webview()
        with mock.patch.dict(sys.modules, {'webview': webview}), \
             mock.patch.object(app, 'is_webview2_installed', return_value=False), \
             mock.patch.object(app, 'open_browser_session') as browser, \
             mock.patch.object(app, 'show_native_startup_error') as error:
            self.assertFalse(app.open_webview(URL, False))
            self.assertIn('WebView2', error.call_args.args[0])
        browser.assert_not_called()
        webview.create_window.assert_not_called()
        webview.start.assert_not_called()

    def test_native_close_does_not_open_browser_or_restart_server(self):
        webview = self.fake_webview()
        self.run_native(webview).assert_not_called()
        webview.start.assert_called_once_with(gui='edgechromium')

    def test_native_download_copy_enabled_before_webview_starts(self):
        webview = self.fake_webview()
        webview.start.side_effect = lambda **kwargs: self.assertTrue(webview.settings['ALLOW_DOWNLOADS'])
        self.run_native(webview)
        self.assertTrue(self.last_result)

    def test_explicit_browser_option_keeps_update_handoff(self):
        with mock.patch.object(app.webbrowser, 'open', return_value=True) as launch, \
             mock.patch.object(app, 'close_launcher_for_update', return_value=True) as handoff, \
             mock.patch.object(app, 'urlopen') as request:
            app.open_browser_session(URL)
        launch.assert_called_once_with(URL, new=2)
        handoff.assert_called_once_with(URL)
        request.assert_not_called()

    def test_runtime_config_is_prepared_before_native_window(self):
        events=[];webview=self.fake_webview()
        webview.create_window.side_effect=lambda *a,**k: (events.append('create'),types.SimpleNamespace(destroy=mock.Mock(),native=None))[1]
        webview.start.side_effect=lambda **k:events.append('start')
        with mock.patch.dict(sys.modules,{'webview':webview}), \
             mock.patch.object(app,'ensure_native_runtime_config',side_effect=lambda:events.append('config')), \
             mock.patch.object(app,'is_webview2_installed',return_value=True), \
             mock.patch.object(app,'find_app_icon',return_value=None):
            self.assertTrue(app.open_webview(URL,False))
        self.assertEqual(events,['config','create','start'])

    def test_failed_window_stops_only_daemon_created_by_this_launch(self):
        args=types.SimpleNamespace(host='127.0.0.1',port=0,data_dir=None,beta=False,quit=False,restart=False,server_only=False,no_browser=False,browser=False,debug=False)
        for existing in (None,{'url':URL}):
            with self.subTest(existing=bool(existing)), \
                 mock.patch.dict(app.os.environ), \
                 mock.patch.object(app,'parse_args',return_value=args), \
                 mock.patch.object(app.AppConfig,'load',return_value=types.SimpleNamespace(data_dir=Path('owned-test-data'))), \
                 mock.patch.object(app,'configure_launcher_log'), \
                 mock.patch.object(app,'detect_running_server',return_value=existing), \
                 mock.patch.object(app,'find_free_port',return_value=51239), \
                 mock.patch.object(app,'spawn_server_process'), \
                 mock.patch.object(app,'wait_for_server_ready',return_value=True), \
                 mock.patch.object(app,'open_webview',return_value=False), \
                 mock.patch.object(app,'quit_running_server') as quit_server:
                self.assertEqual(app.main(),1)
                if existing is None:quit_server.assert_called_once_with(Path('owned-test-data'))
                else:quit_server.assert_not_called()

    def test_browser_launcher_finishes_after_service_stops(self):
        with mock.patch.object(app.webbrowser, 'open', return_value=True), \
             mock.patch.object(app, 'close_launcher_for_update', return_value=False), \
             mock.patch.object(app, 'urlopen', side_effect=OSError('service stopped')) as request, \
             mock.patch.object(app.time, 'sleep'):
            app.open_browser_session(URL)
        self.assertEqual(request.call_count, 3)

    def test_failed_shell_launch_shows_url_without_restarting_service(self):
        for result in (False, OSError('no default browser')):
            with self.subTest(result=result), \
                 mock.patch.object(app.webbrowser, 'open', **({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})), \
                 mock.patch.object(app, 'show_browser_launch_help') as help_dialog, \
                 mock.patch.object(app, 'spawn_server_process') as spawn:
                app.open_browser_session(URL)
                help_dialog.assert_called_once_with(URL)
                spawn.assert_not_called()

    def test_launcher_logs_survive_a_console_free_start(self):
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            with tempfile.TemporaryDirectory() as folder:
                app.configure_launcher_log(Path(folder))
                app.configure_launcher_log(Path(folder))
                added = [h for h in root.handlers if h not in before]
                self.assertEqual(len(added), 1)
                app.logger.error('fixture desktop failure')
                added[0].flush()
                self.assertIn('fixture desktop failure', next(Path(folder).rglob('launcher-*.log')).read_text('utf-8'))
                root.removeHandler(added[0]); added[0].close()
        finally:
            for handler in list(root.handlers):
                if handler not in before:root.removeHandler(handler);handler.close()


if __name__ == '__main__':
    unittest.main()
