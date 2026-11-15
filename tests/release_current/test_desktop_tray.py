# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Close/restore/quit/update policy without spawning a native window or model."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pocket_memory.desktop_tray import DesktopSession, NativeProcessWatch

spec = importlib.util.spec_from_file_location('desktop_tray_app', ROOT / 'app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
URL = 'http://127.0.0.1:51327'


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class DesktopTrayPolicyTests(unittest.TestCase):
    def make(self, active=True, shutdown=None, must_close=None):
        window = types.SimpleNamespace(
            events=types.SimpleNamespace(shown=Event(), closing=Event()),
            show=mock.Mock(), hide=mock.Mock(), restore=mock.Mock(), destroy=mock.Mock())
        tray = mock.Mock()
        tray.start.return_value = active
        factory = mock.Mock(return_value=tray)
        controller = DesktopSession(window, 'test.ico', shutdown, must_close=must_close,
                                    tray_factory=factory, show_error=mock.Mock())
        controller.attach()
        controller.start()
        return controller, window, tray

    def test_close_hides_only_with_a_working_icon_and_restore_returns_window(self):
        shutdown = mock.Mock()
        controller, window, tray = self.make(shutdown=shutdown)
        self.assertFalse(controller.on_closing())
        window.hide.assert_called_once()
        tray.hidden_hint.assert_not_called()
        window.destroy.assert_not_called()
        shutdown.assert_not_called()
        controller.restore()
        window.show.assert_called_once()
        window.restore.assert_not_called()  # Preserve an already maximized window.
        self.assertFalse(controller.on_closing())
        self.assertEqual(window.hide.call_count, 2)
        tray.hidden_hint.assert_not_called()
        tray.stop.assert_not_called()

    def test_restore_unminimizes_an_actually_minimized_window(self):
        controller, window, tray = self.make()
        controller._on_minimized()
        controller.restore()
        window.show.assert_called_once()
        window.restore.assert_called_once()
        self.assertFalse(controller.minimized)

    def test_quit_stops_bound_service_then_removes_icon_and_destroys(self):
        order = []
        controller, window, tray = self.make(shutdown=lambda: order.append('shutdown') or True)
        tray.stop.side_effect = lambda: order.append('icon')
        window.destroy.side_effect = lambda: order.append(('destroy', controller.on_closing()))
        controller._quit()
        self.assertEqual(order, ['shutdown', 'icon', ('destroy', True)])
        self.assertFalse(controller.active)

    def test_failed_shutdown_restores_window_and_allows_retry(self):
        shutdown = mock.Mock(side_effect=[False, True])
        shutdown.has_exited.return_value = False
        controller, window, tray = self.make(shutdown=shutdown)
        controller._quit()
        self.assertFalse(controller.allow_close)
        self.assertTrue(controller.active)
        window.destroy.assert_not_called()
        window.show.assert_called_once()
        controller.show_error.assert_called_once()
        shutdown.has_exited.return_value = True
        controller._quit()
        window.destroy.assert_called_once()

    def test_unavailable_tray_does_not_leave_an_unreachable_hidden_server(self):
        controller, window, tray = self.make(active=False, shutdown=lambda: True)
        with mock.patch.object(controller, 'request_quit') as quit_session:
            self.assertFalse(controller.on_closing())
        quit_session.assert_called_once()
        window.hide.assert_not_called()

    def test_readiness_failure_bypasses_hide_without_stopping_update_server(self):
        shutdown = mock.Mock()
        controller, window, tray = self.make(shutdown=shutdown, must_close=lambda: True)
        self.assertTrue(controller.on_closing())
        window.hide.assert_not_called()
        shutdown.assert_not_called()

    def test_update_handoff_bypasses_hide_and_owns_service_shutdown(self):
        shutdown = mock.Mock()
        controller, window, tray = self.make(shutdown=shutdown)
        window.destroy.side_effect = lambda: self.assertTrue(controller.on_closing())
        controller.close_for_update()
        self.assertTrue(controller.updating)
        window.destroy.assert_called_once()
        tray.stop.assert_called_once()
        window.hide.assert_not_called()
        shutdown.assert_not_called()
        with mock.patch('pocket_memory.desktop_tray.threading.Thread') as thread:
            controller.request_quit()
        thread.assert_not_called()

    def test_duplicate_quit_clicks_create_one_shutdown_worker(self):
        controller, window, tray = self.make(shutdown=lambda: True)
        with mock.patch('pocket_memory.desktop_tray.threading.Thread') as thread:
            controller.request_quit()
            controller.request_quit()
        thread.assert_called_once()

    def test_accepted_slow_shutdown_waits_until_process_really_exits(self):
        shutdown = mock.Mock(return_value=True)
        shutdown.has_exited.side_effect = [False, False, True]
        controller, window, tray = self.make(shutdown=shutdown)
        with mock.patch.object(controller._stopped, 'wait', return_value=False) as wait:
            controller._quit()
        self.assertEqual(wait.call_count, 2)
        tray.set_quitting.assert_called_with(True)
        controller.show_error.assert_not_called()
        window.destroy.assert_called_once()

    def test_shared_shutdown_race_does_not_reopen_a_closed_session(self):
        controller, window, tray = self.make()
        def concurrent_shutdown():
            controller.close_for_server_exit()
            return False  # Own request lost its connection during that close.
        controller.shutdown = concurrent_shutdown
        controller._quit()
        self.assertTrue(controller.allow_close)
        window.destroy.assert_called_once()
        window.show.assert_not_called()
        controller.show_error.assert_not_called()

    def test_closed_server_after_failed_post_closes_instead_of_reporting_retry(self):
        shutdown = mock.Mock(return_value=False)
        shutdown.has_exited.return_value = True
        controller, window, tray = self.make(shutdown=shutdown)
        controller._quit()
        window.destroy.assert_called_once()
        window.show.assert_not_called()
        controller.show_error.assert_not_called()

    def test_shared_server_exit_closes_each_gui_without_another_shutdown(self):
        shutdown = mock.Mock()
        shutdown.has_exited.return_value = True
        for _ in range(2):
            controller, window, tray = self.make(shutdown=shutdown)
            self.assertTrue(app.close_desktop_if_server_exited(shutdown, controller, window))
            window.destroy.assert_called_once()
            tray.stop.assert_called_once()
            self.assertTrue(controller.on_closing())
        shutdown.assert_not_called()

    def test_live_shared_server_does_not_close_other_gui(self):
        shutdown = mock.Mock()
        shutdown.has_exited.return_value = False
        controller, window, tray = self.make(shutdown=shutdown)
        self.assertFalse(app.close_desktop_if_server_exited(shutdown, controller, window))
        window.destroy.assert_not_called()
        tray.stop.assert_not_called()

    def test_quit_and_watcher_close_window_only_once(self):
        controller, window, tray = self.make(shutdown=lambda: True)
        controller._quit()
        controller.close_for_server_exit()
        window.destroy.assert_called_once()
        tray.stop.assert_called_once()

    def test_no_policy_lock_held_while_native_start_waits_for_ui(self):
        controller, window, tray = self.make()
        self.assertEqual(window.events.shown.handlers, [controller.start])
        self.assertEqual(window.events.closing.handlers, [controller.on_closing])
        controller.start()
        tray.start.assert_called_once()


class DesktopShutdownIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = {'pid': 23456, 'url': URL, 'started_at': '2026-11-14T12:00:00'}
        self.folder = Path('session-library')
        self.watch = mock.Mock()
        self.watch.has_exited.return_value = False

    def bind(self):
        with mock.patch.object(app, 'read_lock', return_value=dict(self.identity)), \
             mock.patch.object(app, 'NativeProcessWatch', return_value=self.watch), \
             mock.patch.object(app, 'server_status', return_value={'pid': self.identity['pid']}):
            return app.desktop_shutdown_callback(self.folder, URL)

    def test_quit_only_requests_captured_server(self):
        callback = self.bind()
        with mock.patch.object(app, 'server_status', return_value={'pid': 23456}), \
             mock.patch.object(app, 'request_server_shutdown', return_value=True) as shutdown:
            self.assertTrue(callback())
        shutdown.assert_called_once_with(URL, timeout=120)

    def test_data_directory_move_does_not_break_same_server_quit(self):
        callback = self.bind()
        with mock.patch.object(app, 'read_lock', return_value=None) as old_lock, \
             mock.patch.object(app, 'server_status', return_value={'pid': 23456}), \
             mock.patch.object(app, 'request_server_shutdown', return_value=True) as shutdown:
            self.assertTrue(callback())
        old_lock.assert_not_called()
        shutdown.assert_called_once_with(URL, timeout=120)

    def test_another_server_at_same_port_is_not_stopped(self):
        callback = self.bind()
        with mock.patch.object(app, 'server_status', return_value={'pid': 99999}), \
             mock.patch.object(app, 'request_server_shutdown') as shutdown:
            self.assertFalse(callback())
        shutdown.assert_not_called()

    def test_dead_original_process_is_not_confused_by_reused_pid(self):
        callback = self.bind()
        self.watch.has_exited.return_value = True
        with mock.patch.object(app, 'server_status') as status, \
             mock.patch.object(app, 'request_server_shutdown') as shutdown:
            self.assertTrue(callback())
            self.assertTrue(callback.has_exited())
        status.assert_not_called()
        shutdown.assert_not_called()

    def test_failed_shutdown_never_claims_success_or_uses_process_kill(self):
        callback = self.bind()
        with mock.patch.object(app, 'server_status', return_value={'pid': 23456}), \
             mock.patch.object(app, 'request_server_shutdown', return_value=False):
            self.assertFalse(callback())

    def test_missing_session_identity_retains_desktop_without_stopping_unknown_server(self):
        with mock.patch.object(app, 'read_lock', return_value=None), \
             mock.patch.object(app, 'request_server_shutdown') as shutdown:
            callback = app.desktop_shutdown_callback(self.folder, URL)
            self.assertFalse(callback())
        shutdown.assert_not_called()

    def test_initial_identity_mismatch_disposes_read_only_handle(self):
        with mock.patch.object(app, 'read_lock', return_value=self.identity), \
             mock.patch.object(app, 'NativeProcessWatch', return_value=self.watch), \
             mock.patch.object(app, 'server_status', return_value={'pid': 99999}):
            callback = app.desktop_shutdown_callback(self.folder, URL)
        self.assertFalse(callback())
        self.watch.close.assert_called_once()

    def test_window_cleanup_closes_only_observer_handle(self):
        callback = self.bind()
        callback.close()
        self.watch.close.assert_called_once()


class NativeProcessWatchTests(unittest.TestCase):
    def test_process_handle_only_observes_and_is_closed_once(self):
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.side_effect = [258, 0]
        with mock.patch('ctypes.WinDLL', return_value=kernel):
            watch = NativeProcessWatch(23456)
        kernel.OpenProcess.assert_called_once_with(0x00100000, False, 23456)
        self.assertFalse(watch.has_exited())
        self.assertTrue(watch.has_exited())
        watch.close()
        watch.close()
        kernel.CloseHandle.assert_called_once_with(123)
        self.assertTrue(watch.has_exited())
        kernel.TerminateProcess.assert_not_called()


if __name__ == '__main__':
    unittest.main()
