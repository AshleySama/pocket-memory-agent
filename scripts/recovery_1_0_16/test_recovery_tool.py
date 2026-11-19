# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Controller guards and real file transactions with simulated process health.

No EXE is launched, no system process is stopped, and these tests do not replace
the release owner's final frozen-tool/native-window acceptance.
"""
from contextlib import ExitStack, closing
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

BASE = Path(__file__).resolve().parent
sys.path[:0] = [str(BASE / 'source'), str(BASE / 'source/tests/release_current')]
spec = importlib.util.spec_from_file_location('pm_recovery_tool_test_target', BASE / 'recovery_tool.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
import test_embedded_update as embedded_fixtures
import test_update_builtin_protocol as builtin_fixtures
from test_safe_update import put
from pocket_memory import safe_update


class RecoveryToolTests(unittest.TestCase):
    runtime = embedded_fixtures.EmbeddedExecutorTests.runtime
    write_receipt = embedded_fixtures.EmbeddedExecutorTests.write_receipt

    def setUp(self):
        embedded_fixtures.EmbeddedExecutorTests.setUp(self)
        self.install, self.old_manifest = self.runtime('old-install', worker=False, version='1.0.14')
        put(self.install / 'models/bge/model.bin', embedded_fixtures.MODEL)
        self.data = self.base / 'library'
        self.data.mkdir()
        with closing(sqlite3.connect(self.data / 'pocket_memory.db')) as connection:
            with connection:
                connection.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY, content TEXT)')
                connection.execute("INSERT INTO notes VALUES(1, 'preserved note')")
        put(self.data / 'attachments/note.txt', b'preserved attachment')
        self.config = self.base / 'config.json'
        self.config.write_text(json.dumps({'data_dir': str(self.data), 'theme': 'aqua-memory'}), encoding='utf-8')
        self.tool_dir = self.base / 'repair-bundle'
        self.tool_dir.mkdir()
        self.payload, manifest = builtin_fixtures.BuiltinProtocolTests.builtin(
            self, 'new-payload', version='1.0.16', receipt=False)
        self.package = self.tool_dir / 'PocketMemory-1.0.16-Program-v2.zip'
        self.manifest = manifest
        self.write_package()
        self.release = dict(schema_version=1, app_id='pocket-memory', version='1.0.16',
                            package=dict(filename=self.package.name, size=self.package.stat().st_size,
                                         sha256=tool._hash(self.package)),
                            allowed_previous=[dict(version='1.0.14', sha256=tool._hash(self.install / 'PocketMemory.exe'))])
        self.original = self.snapshot()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(tool, 'tool_directory', return_value=self.tool_dir))
        self.stack.enter_context(mock.patch.object(tool, 'load_release', side_effect=lambda: tool.validate_release(copy.deepcopy(self.release))))
        self.stop = self.stack.enter_context(mock.patch.object(tool, 'stop_original'))
        self.active = []
        self.stack.enter_context(mock.patch.object(safe_update, 'running_install_processes', side_effect=lambda _: list(self.active)))
        self.stack.enter_context(mock.patch.object(tool, 'available_port', return_value=51326))

    def write_package(self, omit=None):
        with ZipFile(self.package, 'w') as archive:
            archive.writestr('update-manifest.json', json.dumps(self.manifest))
            for path in self.payload.rglob('*'):
                if path.is_file():
                    relative = path.relative_to(self.payload).as_posix()
                    if relative != omit:
                        archive.write(path, 'payload/' + relative)

    def snapshot(self):
        protected = [self.config, self.data / 'pocket_memory.db',
                     self.data / 'attachments/note.txt', self.install / 'models/bge/model.bin']
        return {str(path): path.read_bytes() for path in protected}

    def prepare(self):
        prepared = tool.prepare_recovery(self.install, self.config, port=51326)
        self.addCleanup(prepared['attempt'].close)
        return prepared

    def fake_processes(self, *, fail_health=False, fail_restored=False):
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        self.stack.enter_context(mock.patch.object(tool.worker, 'spawn_application', return_value=process))
        self.stack.enter_context(mock.patch.object(tool.worker, 'verify_health',
            side_effect=safe_update.UpdateError('controlled health failure') if fail_health else None))
        self.stack.enter_context(mock.patch.object(tool.worker, 'stop_owned_probe'))
        self.stack.enter_context(mock.patch.object(tool.worker, 'stop_owned_desktop'))
        def ready(*args):
            self.active[:] = [123, 124]
            return {'pid': 123, 'server_pid': 124}
        self.stack.enter_context(mock.patch.object(tool.worker, 'verify_desktop', side_effect=ready))
        self.stack.enter_context(mock.patch.object(tool.worker, 'verify_restored_desktop',
            side_effect=safe_update.UpdateError('old desktop not ready') if fail_restored else None))

    def test_missing_old_worker_can_preflight_without_closing_old_program(self):
        self.assertFalse((self.install / embedded_fixtures.WORKER).exists())
        prepared = self.prepare()
        self.assertEqual(prepared['context']['old_version'], '1.0.14')
        self.assertEqual(prepared['release']['version'], '1.0.16')
        self.stop.assert_not_called()
        self.assertEqual(self.snapshot(), self.original)
        self.assertFalse(safe_update._overlap(prepared['attempt'].work, self.install))

    def test_release_accepts_original_113_identity(self):
        self.release['allowed_previous'][0]['version'] = '1.0.13'
        prepared = self.prepare()
        self.assertEqual(prepared['context']['old_version'], '1.0.13')
        self.stop.assert_not_called()

    def test_original_113_requires_exact_executable_identity(self):
        self.release['allowed_previous'][0].update(version='1.0.13', sha256='0'*64)
        with self.assertRaises(safe_update.UpdateError):
            self.prepare()
        self.assertEqual(self.snapshot(), self.original)
        self.stop.assert_not_called()

    def test_unpublished_115_not_claimed_as_supported_start(self):
        self.release['allowed_previous'][0]['version'] = '1.0.15'
        with self.assertRaises(safe_update.UpdateError):
            tool.validate_release(self.release)
        self.assertEqual(self.snapshot(), self.original)

    def test_normal_transaction_commits_v2_and_preserves_original_data(self):
        prepared = self.prepare()
        self.fake_processes()
        result = tool.execute_recovery(prepared, lambda *_: True)
        self.assertEqual(result['state'], 'complete')
        self.stop.assert_called_once()
        receipt = json.loads((self.install / '.pocket-memory-update/installed.json').read_text('utf-8'))
        self.assertEqual(receipt['version'], '1.0.16')
        self.assertEqual(receipt['manifest']['schema_version'], 2)
        self.assertFalse((self.install / embedded_fixtures.WORKER).exists())
        self.assertEqual(self.snapshot(), self.original)
        self.assertTrue((prepared['attempt'].work / 'recovery-result.json').is_file())

    def test_failed_new_health_restores_original_program_receipt_and_data(self):
        original_main = (self.install / 'PocketMemory.exe').read_bytes()
        original_receipt = (self.install / '.pocket-memory-update/installed.json').read_bytes()
        prepared = self.prepare()
        self.fake_processes(fail_health=True)
        result = tool.execute_recovery(prepared, lambda *_: True)
        self.assertEqual(result['state'], 'rolled_back')
        self.assertEqual((self.install / 'PocketMemory.exe').read_bytes(), original_main)
        self.assertEqual((self.install / '.pocket-memory-update/installed.json').read_bytes(), original_receipt)
        self.assertEqual(self.snapshot(), self.original)
        self.assertFalse((self.install / embedded_fixtures.WORKER).exists())
        tool.worker.verify_restored_desktop.assert_called_once()

    def test_failed_old_desktop_confirmation_does_not_claim_successful_recovery(self):
        prepared = self.prepare()
        self.fake_processes(fail_health=True, fail_restored=True)
        self.assertEqual(tool.execute_recovery(prepared, lambda *_: True)['state'], 'recovery_required')

    def test_user_cancel_and_failed_normal_exit_never_replace_program(self):
        prepared = self.prepare()
        self.assertEqual(tool.execute_recovery(prepared, lambda *_: False)['state'], 'cancelled')
        self.stop.assert_not_called()
        self.stop.side_effect = safe_update.UpdateError('old window remains open')
        self.assertEqual(tool.execute_recovery(prepared, lambda *_: True)['state'], 'error')
        self.assertEqual(tool._hash(self.install / 'PocketMemory.exe'), self.release['allowed_previous'][0]['sha256'])
        self.assertEqual(self.snapshot(), self.original)

    def test_changed_config_after_confirmation_is_rejected_before_close(self):
        prepared = self.prepare()
        self.config.write_text(json.dumps({'data_dir': str(self.data), 'theme': 'changed'}), encoding='utf-8')
        result = tool.execute_recovery(prepared, lambda *_: True)
        self.assertEqual(result['state'], 'error')
        self.stop.assert_not_called()
        self.assertEqual(tool._hash(self.install / 'PocketMemory.exe'), self.release['allowed_previous'][0]['sha256'])

    def test_unknown_exe_and_package_hash_mismatch_are_rejected_before_close(self):
        main = self.install / 'PocketMemory.exe'
        original = main.read_bytes()
        main.write_bytes(original + b'unknown')
        with self.assertRaises(safe_update.UpdateError):
            self.prepare()
        main.write_bytes(original)
        self.package.write_bytes(self.package.read_bytes() + b'changed')
        with self.assertRaises(safe_update.UpdateError):
            self.prepare()
        self.stop.assert_not_called()
        self.assertEqual(self.snapshot(), self.original)

    def test_missing_payload_runtime_is_rejected_even_with_bound_archive_hash(self):
        self.write_package(omit='_internal/base_library.zip')
        self.release['package'].update(size=self.package.stat().st_size, sha256=tool._hash(self.package))
        with self.assertRaises(safe_update.UpdateError):
            self.prepare()
        self.stop.assert_not_called()
        self.assertEqual(self.snapshot(), self.original)

    def test_pending_transaction_is_preserved_without_recovery_or_close(self):
        journal = self.install / '.pocket-memory-update/journal.json'
        raw = json.dumps(dict(schema_version=1, transaction='a' * 32, version='1.0.16',
                              state='pending_health', operations=[])).encode()
        journal.write_bytes(raw)
        with self.assertRaises(safe_update.UpdateError), mock.patch.object(tool.SafeUpdater, 'recover') as recover:
            self.prepare()
        recover.assert_not_called()
        self.assertEqual(journal.read_bytes(), raw)
        self.stop.assert_not_called()

    def test_pending_transaction_appearing_after_preflight_is_not_recovered(self):
        prepared = self.prepare()
        journal = self.install / '.pocket-memory-update/journal.json'
        raw = json.dumps(dict(schema_version=1, transaction='b' * 32, version='1.0.16',
                              state='applying', operations=[])).encode()
        journal.write_bytes(raw)
        with mock.patch.object(prepared['updater'], 'recover') as recover:
            result = tool.execute_recovery(prepared, lambda *_: True)
        self.assertEqual(result['state'], 'error')
        recover.assert_not_called()
        self.stop.assert_not_called()
        self.assertEqual(journal.read_bytes(), raw)

    def test_changed_model_is_rejected_before_closing_or_replacing_program(self):
        model = self.install / 'models/bge/model.bin'
        model.write_bytes(b'changed model')
        with self.assertRaises(safe_update.UpdateError):
            self.prepare()
        self.stop.assert_not_called()
        self.assertEqual(model.read_bytes(), b'changed model')
        self.assertEqual(tool._hash(self.install / 'PocketMemory.exe'), self.release['allowed_previous'][0]['sha256'])

    def test_data_overlapping_program_root_and_tool_inside_install_are_rejected(self):
        self.config.write_text(json.dumps({'data_dir': str(self.install)}), encoding='utf-8')
        (self.install / 'pocket_memory.db').write_bytes((self.data / 'pocket_memory.db').read_bytes())
        with self.assertRaises(safe_update.UpdateError):
            self.prepare()
        self.config.write_bytes(self.original[str(self.config)])
        with mock.patch.object(tool, 'tool_directory', return_value=self.install), self.assertRaises(safe_update.UpdateError):
            self.prepare()
        self.stop.assert_not_called()


class RecoveryTrustTests(unittest.TestCase):
    def test_all_launch_modes_clean_child_environment_and_restore_parent(self):
        request = dict(config_path='C:/fixture/config.json', local_app_data='C:/fixture/profile',
                       health_token='b' * 64, data_dir='C:/fixture/library')
        parent = dict(_PYI_APPLICATION_HOME_DIR='C:/parent-runtime', _PYI_PARENT_PROCESS_LEVEL='1',
                      _MEIPASS2='C:/old-runtime', PYINSTALLER_RESET_ENVIRONMENT='0',
                      PM_UPDATE_CONFIG_PATH='C:/unrelated/config.json', PM_UPDATE_HEALTH_TOKEN='old-token',
                      PM_UPDATE_PROBE='1', PM_UPDATE_DESKTOP_TOKEN='old-desktop',
                      PM_UPDATE_DESKTOP_PROTOCOL='old-protocol', PM_UPDATE_READY_FILE='old-ready',
                      UNRELATED_SETTING='preserve')
        for options in ({'probe': True}, {'desktop': True}, {}):
            with self.subTest(options=options), mock.patch.dict(os.environ, parent, clear=True):
                before = dict(os.environ)
                def launch(actual_request, work, **actual_options):
                    self.assertEqual(actual_request, request)
                    self.assertEqual(actual_options, options)
                    self.assertFalse(any(key.startswith(('_PYI_', 'PM_UPDATE_')) for key in os.environ))
                    self.assertNotIn('_MEIPASS2', os.environ)
                    self.assertEqual(os.environ['PYINSTALLER_RESET_ENVIRONMENT'], '1')
                    self.assertEqual(os.environ['UNRELATED_SETTING'], 'preserve')
                    # The unchanged worker must reintroduce this request's
                    # config and handshake after inherited values are cleared.
                    child = tool.worker.restart_environment(request, probe=options.get('probe', False),
                        desktop_receipt=Path('C:/fixture/desktop-ready.json') if options.get('desktop') else None)
                    self.assertEqual(child['PM_UPDATE_CONFIG_PATH'], request['config_path'])
                    if options.get('probe'):
                        self.assertEqual(child['PM_UPDATE_HEALTH_TOKEN'], request['health_token'])
                        self.assertEqual(child['PM_UPDATE_DESKTOP_PROTOCOL'], '1')
                    elif options.get('desktop'):
                        self.assertEqual(child['PM_UPDATE_DESKTOP_TOKEN'], request['health_token'])
                        self.assertNotIn('PM_UPDATE_HEALTH_TOKEN', child)
                    else:
                        self.assertFalse(any(key.startswith('PM_UPDATE_') and key != 'PM_UPDATE_CONFIG_PATH'
                                             for key in child))
                    return 'owned-child'
                with mock.patch.object(tool.worker, 'spawn_application', side_effect=launch):
                    self.assertEqual(tool.spawn_application(request, Path('C:/fixture/work'), **options), 'owned-child')
                self.assertEqual(dict(os.environ), before)

    def test_failed_launch_restores_parent_and_does_not_leave_reset_flag(self):
        with mock.patch.dict(os.environ, {'_PYI_PARENT_PROCESS_LEVEL': '1',
                                         'PM_UPDATE_CONFIG_PATH': 'parent-config',
                                         'UNRELATED_SETTING': 'preserve'}, clear=True):
            before = dict(os.environ)
            with mock.patch.object(tool.worker, 'spawn_application', side_effect=OSError('launch failed')):
                with self.assertRaises(OSError):
                    tool.spawn_application({}, Path('C:/fixture/work'), probe=True)
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn('PYINSTALLER_RESET_ENVIRONMENT', os.environ)

    def test_source_mode_never_loads_adjacent_external_release_json(self):
        with mock.patch.object(tool.sys, 'frozen', False, create=True):
            with self.assertRaisesRegex(safe_update.UpdateError, '不接受外部发布JSON'):
                tool.load_release()

    def test_release_rejects_paths_boolean_sizes_and_unknown_previous_versions(self):
        good = dict(schema_version=1, app_id='pocket-memory', version='1.0.16',
                    package=dict(filename='PocketMemory-1.0.16-v2.zip', size=123, sha256='a' * 64),
                    allowed_previous=[dict(version='1.0.14', sha256='b' * 64)])
        tool.validate_release(good)
        invalid = [dict(good, schema_version=True), dict(good, version='1.0.17'),
                   dict(good, package=dict(good['package'], filename='../other.zip')),
                   dict(good, package=dict(good['package'], size=True)),
                   dict(good, allowed_previous=[dict(version='0.0.0', sha256='b' * 64)])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(safe_update.UpdateError):
                tool.validate_release(value)


if __name__ == '__main__':
    unittest.main()
