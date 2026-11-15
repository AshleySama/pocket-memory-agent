# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Small filesystem/process-identity fixtures; no build, GUI or real installation."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock

from pocket_memory import legacy_update_handoff as h


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, dict):
        value = json.dumps(value).encode()
    path.write_bytes(value)


def entry(path, value):
    return dict(path=path, size=len(value), sha256=hashlib.sha256(value).hexdigest())


class LegacyHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pm-handoff-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.install = self.base / 'install'
        self.work = self.base / 'PocketMemory-update-fixture'
        self.data = self.base / 'library'
        self.config = SimpleNamespace(path=self.base / 'profile/config.json')
        self.supervisor = self.work / 'desktop-supervisor/PocketMemoryUpdateWorker.exe'
        self.state = self.install / h.STATE_DIR
        self.token, self.tx = 'a' * 64, 'b' * 32
        files = {'PocketMemory.exe': b'new-main', h.WORKER_RELATIVE_PATH: b'new-worker'}
        self.manifest = dict(schema_version=1, app_id='pocket-memory', kind='program-update',
                             version=h.APP_VERSION, files=[entry(p, v) for p, v in files.items()],
                             model_requirements=[entry('Qwen3-4B/model.gguf', b'preserved-model')],
                             data_compat=dict(accepted_schema_ids=['legacy-unversioned'],
                                              target_schema_id='legacy-unversioned', migration='none'))
        for relative, content in files.items():
            write(self.install / relative, content)
        write(self.config.path, {'data_dir': str(self.data)})
        write(self.data / 'notes.txt', b'never alter this note')
        write(self.work / 'source/update-manifest.json', self.manifest)
        write(self.work / 'PocketMemoryUpdateWorker.exe', b'known-old-worker')
        self.legacy = dict(pid=111, path=str(self.work / 'PocketMemoryUpdateWorker.exe'), created=11111)
        self.supervisor_identity = dict(pid=222, path=str(self.supervisor), created=22222)
        self.request = dict(schema_version=1, install_dir=str(self.install), data_dir=str(self.data),
                            config_path=str(self.config.path), source_dir=str(self.work / 'source'),
                            target_version=h.APP_VERSION, parent_pid=101, launcher_pid=102,
                            port=14764, health_token=self.token, local_app_data=None, debug=False)
        self.path = self.work / 'request.json'
        write(self.path, self.request)
        write(self.work / 'ready.json', {'token': self.token, 'pid': 111})
        write(self.work / 'go.json', {'token': self.token})
        write(self.work / 'handoff-ready.json', {'token': self.token, 'transaction': self.tx, 'pid': 222})
        self.journal = dict(schema_version=1, transaction=self.tx, version=h.APP_VERSION,
                            state='pending_health', operations=[])
        write(self.state / 'journal.json', self.journal)
        write(self.state / 'installed.json', dict(version=h.APP_VERSION, manifest=self.manifest))
        write(self.state / self.tx / 'receipt-backup.json', {'version': '1.0.3'})
        patchers = [mock.patch.dict(os.environ, {'LOCALAPPDATA': str(self.base / 'local'),
                       'PM_UPDATE_CONFIG_PATH': str(self.config.path),
                       'PM_UPDATE_HEALTH_TOKEN': self.token, 'PM_UPDATE_DESKTOP_PROTOCOL': '',
                       'PM_UPDATE_DESKTOP_TOKEN': ''}),
                    mock.patch.object(h.sys, 'frozen', True, create=True),
                    mock.patch.object(h.sys, 'executable', str(self.install / 'PocketMemory.exe')),
                    mock.patch.object(h.tempfile, 'gettempdir', return_value=str(self.base)),
                    mock.patch.object(h.os, 'getppid', return_value=111),
                    mock.patch.object(h, 'LEGACY_WORKER_SHA256', hashlib.sha256(b'known-old-worker').hexdigest()),
                    mock.patch.object(h, 'process_identity', side_effect=lambda pid:
                                      self.legacy if pid == 111 else self.supervisor_identity if pid == 222 else None),
                    mock.patch.object(h, 'worker_identity_matches', return_value=True)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def stage(self):
        process = mock.Mock(pid=222)
        process.poll.return_value = None
        with mock.patch.object(h.subprocess, 'Popen', return_value=process) as spawn:
            marker = h.stage_handoff(self.config, self.data)
        return marker, spawn

    def test_stages_verified_new_supervisor_without_changing_original_worker_or_user_files(self):
        marker, spawn = self.stage()
        self.assertEqual(marker['supervisor_identity'], self.supervisor_identity)
        self.assertEqual(self.supervisor.read_bytes(), b'new-worker')
        self.assertEqual((self.work / 'PocketMemoryUpdateWorker.exe').read_bytes(), b'known-old-worker')
        self.assertEqual((self.data / 'notes.txt').read_bytes(), b'never alter this note')
        self.assertEqual(spawn.call_args.args[0][-1], '--finalize-existing')
        self.assertNotIn('PM_UPDATE_HEALTH_TOKEN', spawn.call_args.kwargs['env'])
        result = h.load_handoff(self.path)
        self.assertEqual(result['old_version'], '1.0.3')
        self.assertEqual(result['transaction'], self.tx)

    def test_current_worker_probe_bypasses_legacy_protocol(self):
        with mock.patch.dict(os.environ, {'PM_UPDATE_DESKTOP_PROTOCOL': '1'}), \
             mock.patch.object(h.subprocess, 'Popen') as spawn:
            self.assertIsNone(h.stage_handoff(self.config, self.data))
            spawn.assert_not_called()

    def test_unknown_legacy_worker_fails_before_any_process_launch(self):
        write(self.work / 'PocketMemoryUpdateWorker.exe', b'another-old-worker')
        with mock.patch.object(h.subprocess, 'Popen') as spawn, self.assertRaises(h.UpdateError):
            h.stage_handoff(self.config, self.data)
        spawn.assert_not_called()

    def test_wrong_data_or_config_rejected(self):
        with self.assertRaises(h.UpdateError):
            h.stage_handoff(self.config, self.base / 'other-data')
        with self.assertRaises(h.UpdateError):
            h.stage_handoff(SimpleNamespace(path=self.base / 'other.json'), self.data)

    def test_manifest_tamper_and_changed_transaction_rejected(self):
        marker, _ = self.stage()
        write(self.supervisor, b'altered-worker')
        with self.assertRaises(h.UpdateError):
            h.load_handoff(self.path)
        write(self.supervisor, b'new-worker')
        write(self.state / 'journal.json', dict(self.journal, transaction='c' * 32))
        with self.assertRaises((h.UpdateError, OSError)):
            h.load_handoff(self.path)

    def test_expired_marker_or_token_mismatch_rejected(self):
        marker, _ = self.stage()
        for change in ({'expires': time.time() - 1}, {'token': 'c' * 64}, {'version': '9.9.9'}):
            with self.subTest(change=change):
                write(self.state / h.MARKER_NAME, dict(marker, **change))
                with self.assertRaises(h.UpdateError):
                    h.load_handoff(self.path)

    def test_yield_only_original_restart_with_live_supervisor(self):
        self.stage()
        args = SimpleNamespace(server_only=False, quit=False, host='127.0.0.1', port=14764)
        with mock.patch.dict(os.environ, {'PM_UPDATE_HEALTH_TOKEN': ''}):
            self.assertTrue(h.yield_to_supervisor(self.config, self.data, args))
            with mock.patch.object(h.os, 'getppid', return_value=999):
                self.assertFalse(h.yield_to_supervisor(self.config, self.data, args))
            with mock.patch.object(h, 'identity_alive', return_value=False):
                self.assertFalse(h.yield_to_supervisor(self.config, self.data, args))
            with mock.patch.dict(os.environ, {'PM_UPDATE_DESKTOP_TOKEN': self.token}):
                self.assertFalse(h.yield_to_supervisor(self.config, self.data, args))

    def test_pid_reuse_not_treated_as_original_process(self):
        with mock.patch.object(h, 'process_identity', return_value=dict(self.legacy, created=99999)):
            self.assertFalse(h.identity_alive(self.legacy))

    def test_source_manifest_must_match_installed_inventory(self):
        self.stage()
        write(self.work / 'source/update-manifest.json', dict(self.manifest, version='0.0.0'))
        with self.assertRaises(h.UpdateError):
            h.load_handoff(self.path)


if __name__ == '__main__':
    unittest.main()
