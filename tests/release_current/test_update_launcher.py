# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Launcher-only update exit checks; no native windows or real process exits."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / '.'
sys.path.insert(0, str(CANDIDATE))
SPEC = importlib.util.spec_from_file_location('pm_update_launcher_fixture', CANDIDATE / 'app.py')
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class LauncherUpdateExitTests(unittest.TestCase):
    def test_unconfirmed_update_never_closes_window_or_exits(self):
        for status in (
                {'state': 'idle'},
                {'state': 'downloading', 'shutdown_requested': True},
                {'state': 'restarting', 'shutdown_requested': False},
                {'state': 'restarting', 'shutdown_requested': 'true'}):
            with self.subTest(status=status):
                destroy, exit_process = mock.Mock(), mock.Mock()
                with mock.patch.object(app, 'urlopen', return_value=io.BytesIO(json.dumps(status).encode())):
                    self.assertFalse(app.close_launcher_for_update('http://127.0.0.1:1234', destroy,
                                                                   exit_process=exit_process))
                destroy.assert_not_called()
                exit_process.assert_not_called()

    def test_confirmed_update_exits_even_when_destroy_returns(self):
        events = []
        response = io.BytesIO(b'{"state":"restarting","shutdown_requested":true}')
        with mock.patch.object(app, 'urlopen', return_value=response):
            result = app.close_launcher_for_update(
                'http://127.0.0.1:1234', lambda: events.append('destroy'),
                exit_process=lambda code: events.append(('exit_self', code)))
        self.assertTrue(result)
        self.assertEqual(events, ['destroy', ('exit_self', 0)])

    def test_confirmed_update_has_bounded_exit_when_destroy_is_stuck(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        exit_process = mock.Mock()

        def stuck_destroy():
            started.set()
            release.wait()
            finished.set()

        try:
            with mock.patch.object(app, 'update_is_restarting', return_value=True):
                self.assertTrue(app.close_launcher_for_update(
                    'http://127.0.0.1:1234', stuck_destroy,
                    exit_process=exit_process, close_timeout=0.03))
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.is_set())
            exit_process.assert_called_once_with(0)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_destroy_failure_and_browser_fallback_only_exit_own_launcher(self):
        for destroy in (None, mock.Mock(side_effect=RuntimeError('native host closed'))):
            with self.subTest(has_window=destroy is not None):
                exit_process = mock.Mock()
                with mock.patch.object(app, 'update_is_restarting', return_value=True):
                    self.assertTrue(app.close_launcher_for_update(
                        'http://127.0.0.1:1234', destroy, exit_process=exit_process))
                exit_process.assert_called_once_with(0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
