# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Embedded-executor trust boundaries using inert files; never launch an EXE.

Execution belongs to the batch's designated test runner. These fixtures prove
integrity and transaction behavior, not PyInstaller or native desktop startup.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pocket_memory import embedded_update as embedded
from pocket_memory import safe_update as safe
from pocket_memory import update_component as component
from test_safe_update import entry, put
from update_component_fixture import pe_bytes


WORKER = component.WORKER_RELATIVE_PATH
MODEL = b'inert model resource; no inference'
CAPABILITY = {'schema_version': 1, 'executor': 'main-exe-v1',
              'python_dll': 'python313.dll'}


class EmbeddedExecutorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='pm-embedded-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {'LOCALAPPDATA': str(self.base / 'profile')})
        environment.start()
        self.addCleanup(environment.stop)

    def runtime(self, name='installed', *, worker=False, marker=True, receipt=True,
                version='1.0.13', root=None):
        root = self.base / name if root is None else root
        files = {
            'PocketMemory.exe': pe_bytes() + version.encode(),
            '_internal/base_library.zip': b'inert standard library archive',
            '_internal/python313.dll': pe_bytes() + b'inert python runtime',
            '_internal/package/native.pyd': b'inert package extension',
            WORKER: pe_bytes() + b'inert legacy worker',
            'README.txt': b'program readme',
            'licenses/example.txt': b'program license',
        }
        if marker:
            files[embedded.CAPABILITY_PATH] = json.dumps(CAPABILITY).encode()
        for relative, value in files.items():
            if relative != WORKER or worker:
                put(root / relative, value)
        manifest = {
            'schema_version': 1, 'app_id': 'pocket-memory', 'kind': 'program-update',
            'version': version,
            'files': [entry(relative, value) for relative, value in files.items()],
            'model_requirements': [entry('bge/model.bin', MODEL)],
            'data_compat': {'accepted_schema_ids': ['legacy-unversioned'],
                            'target_schema_id': 'legacy-unversioned', 'migration': 'none'},
        }
        if receipt:
            self.write_receipt(root, manifest)
        return root, manifest

    def write_receipt(self, root, manifest):
        put(root / safe.STATE_DIR / 'installed.json', json.dumps({
            'schema_version': 1, 'app_id': 'pocket-memory', 'version': manifest['version'],
            'data_schema_id': 'legacy-unversioned', 'manifest': manifest,
        }).encode())

    def change_manifest_file(self, root, manifest, relative, content):
        """Keep inventory hashes valid when testing semantic marker validation."""
        put(root / relative, content)
        manifest['files'] = [entry(relative, content) if item['path'] == relative else item
                             for item in manifest['files']]
        self.write_receipt(root, manifest)

    def transaction(self, name, *, old_worker=True):
        install, old_manifest = self.runtime(name + '-installed', worker=old_worker,
                                             version='1.0.12')
        put(install / 'models/bge/model.bin', MODEL)
        put(install / 'data/notes/keep.txt', b'user note')
        source = self.base / (name + '-source')
        _, manifest = self.runtime(root=source / 'payload', worker=True, receipt=False)
        put(source / safe.MANIFEST_NAME, json.dumps(manifest).encode())
        return install, source, old_manifest, manifest

    def assert_blocked(self, root):
        diagnostic = embedded.inspect_executor(root)
        self.assertNotEqual(diagnostic['status'], 'ok', diagnostic)
        self.assertNotEqual(diagnostic.get('executor'), 'embedded', diagnostic)

    def test_missing_worker_uses_only_receipt_verified_marked_runtime(self):
        root, manifest = self.runtime()
        diagnostic = embedded.inspect_executor(root)
        self.assertEqual(diagnostic['status'], 'ok')
        self.assertEqual(diagnostic['executor'], 'embedded')
        self.assertEqual(diagnostic['legacy_component_status'], 'missing')
        self.assertEqual(diagnostic['verification'], 'installed_manifest_full_runtime')
        self.assertEqual(diagnostic['path'], str(root / 'PocketMemory.exe'))
        self.assertEqual(embedded.verify_runtime(root), manifest)
        self.assertFalse((root / WORKER).exists())

    def test_healthy_standalone_remains_available_without_embedded_capability(self):
        root, _ = self.runtime(worker=True, marker=False)
        with mock.patch.object(embedded, 'verify_runtime', side_effect=AssertionError('no fallback')):
            diagnostic = embedded.inspect_executor(root)
        self.assertEqual(diagnostic['status'], 'ok')
        self.assertEqual(diagnostic['executor'], 'standalone')

    def test_old_unmarked_or_missing_corrupt_receipt_never_enables_fallback(self):
        for variant in ('no_marker', 'old_receipt', 'no_receipt', 'corrupt_receipt',
                        'invalid_manifest_type', 'invalid_receipt_identity'):
            with self.subTest(variant=variant):
                root, manifest = self.runtime(variant, marker=variant != 'no_marker',
                                              receipt=variant != 'no_receipt')
                if variant == 'old_receipt':
                    manifest['files'] = [item for item in manifest['files']
                                         if item['path'] != embedded.CAPABILITY_PATH]
                    self.write_receipt(root, manifest)
                elif variant == 'corrupt_receipt':
                    (root / safe.STATE_DIR / 'installed.json').write_bytes(b'{broken')
                elif variant == 'invalid_manifest_type':
                    put(root / safe.STATE_DIR / 'installed.json', json.dumps({
                        'schema_version': 1, 'app_id': 'pocket-memory', 'manifest': [],
                    }).encode())
                elif variant == 'invalid_receipt_identity':
                    envelope = safe._read_json(root / safe.STATE_DIR / 'installed.json')
                    envelope['app_id'] = 'another-application'
                    put(root / safe.STATE_DIR / 'installed.json', json.dumps(envelope).encode())
                self.assert_blocked(root)

    def test_missing_changed_or_undeclared_runtime_blocks_fallback(self):
        for variant in ('missing_library', 'changed_library', 'changed_main', 'extra_library'):
            with self.subTest(variant=variant):
                root, _ = self.runtime(variant)
                if variant == 'missing_library':
                    (root / '_internal/base_library.zip').unlink()
                elif variant == 'changed_library':
                    (root / '_internal/python313.dll').write_bytes(b'changed runtime')
                elif variant == 'changed_main':
                    (root / 'PocketMemory.exe').write_bytes(pe_bytes() + b'changed main')
                else:
                    put(root / '_internal/undeclared.pyd', b'undeclared executable code')
                self.assert_blocked(root)

    def test_hash_matched_invalid_main_or_missing_required_inventory_blocks_fallback(self):
        root, manifest = self.runtime('invalid-main')
        self.change_manifest_file(root, manifest, 'PocketMemory.exe', b'not a PE executable')
        self.assert_blocked(root)
        root, manifest = self.runtime('missing-runtime-inventory')
        manifest['files'] = [item for item in manifest['files']
                             if item['path'] != '_internal/python313.dll']
        (root / '_internal/python313.dll').unlink()
        self.write_receipt(root, manifest)
        self.assert_blocked(root)

    def test_unreadable_runtime_inventory_cannot_establish_absence_of_extra_code(self):
        root, _ = self.runtime()
        original_scandir = os.scandir
        def denied_scandir(path):
            if Path(path) == root / '_internal':
                raise PermissionError(13, 'runtime directory cannot be enumerated')
            return original_scandir(path)
        with mock.patch.object(embedded.os, 'scandir', side_effect=denied_scandir):
            self.assert_blocked(root)

    def test_hash_matched_wrong_capability_is_not_an_execution_authority(self):
        invalid = [
            dict(CAPABILITY, schema_version=True),
            dict(CAPABILITY, executor='downloaded-worker'),
            dict(CAPABILITY, python_dll='../python313.dll'),
            dict(CAPABILITY, python_dll='python314.dll'),
            dict(CAPABILITY, extra='unsupported'),
        ]
        for index, capability in enumerate(invalid):
            with self.subTest(capability=capability):
                root, manifest = self.runtime('bad-marker-' + str(index))
                self.change_manifest_file(root, manifest, embedded.CAPABILITY_PATH,
                                          json.dumps(capability).encode())
                self.assert_blocked(root)

    def test_existing_damaged_changed_or_directory_worker_cannot_trigger_fallback(self):
        for variant, expected in (('damaged', 'invalid_executable'),
                                  ('changed', 'integrity_mismatch'), ('directory', 'not_file')):
            with self.subTest(variant=variant):
                root, _ = self.runtime(variant, worker=True)
                if variant == 'damaged':
                    (root / WORKER).write_bytes(b'broken')
                elif variant == 'changed':
                    (root / WORKER).write_bytes(pe_bytes() + b'not the installed worker')
                else:
                    (root / WORKER).unlink()
                    (root / WORKER).mkdir()
                with mock.patch.object(embedded, 'verify_runtime', side_effect=AssertionError('no fallback')):
                    self.assertEqual(embedded.inspect_executor(root)['status'], expected)

    def test_unreadable_worker_is_not_treated_as_absent(self):
        root, _ = self.runtime(worker=True)
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == root / WORKER:
                raise PermissionError(13, 'inert access-denied fixture')
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', guarded_open), \
                mock.patch.object(embedded, 'verify_runtime', side_effect=AssertionError('no fallback')):
            self.assertEqual(embedded.inspect_executor(root)['status'], 'unreadable')

    def test_linked_worker_is_not_treated_as_absent(self):
        root, _ = self.runtime(worker=True)
        os.link(root / WORKER, self.base / 'worker-hardlink.exe')
        with mock.patch.object(embedded, 'verify_runtime', side_effect=AssertionError('no fallback')):
            self.assertEqual(embedded.inspect_executor(root)['status'], 'unsafe_path')

    def test_staging_copies_verified_runtime_outside_install_and_preserves_user_data(self):
        root, manifest = self.runtime()
        put(root / 'data/notes/keep.txt', b'user note')
        put(root / 'models/bge/model.bin', MODEL)
        work = self.base / 'work'
        work.mkdir()
        command, executable = embedded.stage_executor(root, work)
        snapshot = work / 'installed-runtime'
        self.assertEqual(executable, snapshot / 'PocketMemory.exe')
        self.assertEqual(command, [str(executable), embedded.EMBEDDED_FLAG])
        self.assertEqual(embedded.verify_runtime(snapshot), manifest)
        for item in manifest['files']:
            if item['path'] != WORKER:
                safe._verify(snapshot / item['path'], item)
        self.assertFalse((snapshot / WORKER).exists())
        self.assertFalse((snapshot / 'data').exists())
        self.assertFalse((snapshot / 'models').exists())
        self.assertEqual((root / 'data/notes/keep.txt').read_bytes(), b'user note')
        self.assertEqual((root / 'models/bge/model.bin').read_bytes(), MODEL)
        self.assertFalse((root / WORKER).exists())

    def test_staging_rejects_source_change_or_corrupt_copy(self):
        for variant in ('source_changes', 'copy_changes'):
            with self.subTest(variant=variant):
                root, _ = self.runtime(variant)
                work = self.base / (variant + '-work')
                work.mkdir()
                original_copy = shutil.copy2
                def corrupt_copy(source, destination, *args, **kwargs):
                    if Path(source).name == 'native.pyd' and variant == 'source_changes':
                        Path(source).write_bytes(b'changed after preflight')
                    result = original_copy(source, destination, *args, **kwargs)
                    if Path(source).name == 'native.pyd' and variant == 'copy_changes':
                        Path(destination).write_bytes(b'copy does not match receipt')
                    return result
                with mock.patch.object(embedded.shutil, 'copy2', side_effect=corrupt_copy):
                    with self.assertRaises(safe.UpdateError):
                        embedded.stage_executor(root, work)
                self.assertFalse((work / 'installed-runtime' / safe.STATE_DIR / 'installed.json').exists())

    def test_staging_refuses_insufficient_space_before_creating_snapshot(self):
        root, _ = self.runtime()
        work = self.base / 'work'
        work.mkdir()
        with mock.patch.object(embedded.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(safe.UpdateError, '磁盘空间不足'):
                embedded.stage_executor(root, work)
        self.assertFalse((work / 'installed-runtime').exists())
        self.assertEqual(embedded.inspect_executor(root)['executor'], 'embedded')

    def test_staging_cannot_use_installation_subdirectory(self):
        root, _ = self.runtime()
        work = root / 'temporary-work'
        work.mkdir()
        with self.assertRaises(safe.UpdateError):
            embedded.stage_executor(root, work)
        self.assertFalse((work / 'installed-runtime').exists())

    def test_missing_worker_exception_is_scoped_to_installed_validation(self):
        root, manifest = self.runtime()
        with self.assertRaises(safe.UpdateError):
            safe._verify_components(root, manifest)
        safe._verify_components(root, manifest, allow_embedded=True)
        (root / '_internal/base_library.zip').unlink()
        with self.assertRaises(safe.UpdateError):
            safe._verify_components(root, manifest, allow_embedded=True)

    def test_capability_contents_are_validated_even_with_legacy_worker_present(self):
        root, manifest = self.runtime(worker=True)
        safe._verify_components(root, manifest)
        self.change_manifest_file(root, manifest, embedded.CAPABILITY_PATH,
                                  json.dumps(dict(CAPABILITY, executor='unsupported')).encode())
        for allow_embedded in (False, True):
            with self.subTest(allow_embedded=allow_embedded):
                with self.assertRaises(safe.UpdateError):
                    safe._verify_components(root, manifest, allow_embedded=allow_embedded)

    def test_payload_or_same_volume_stage_cannot_omit_transition_worker(self):
        for variant in ('payload', 'stage'):
            with self.subTest(variant=variant):
                install, source, old_manifest, _ = self.transaction(variant)
                updater = safe.SafeUpdater(install)
                original = safe._verify_components
                if variant == 'payload':
                    (source / 'payload' / WORKER).unlink()
                    with self.assertRaises(safe.UpdateError):
                        updater.check(source)
                def drop_stage_worker(root, manifest, **kwargs):
                    if variant == 'stage' and Path(root).name == 'stage':
                        (Path(root) / WORKER).unlink()
                    return original(root, manifest, **kwargs)
                with mock.patch.object(safe, '_verify_components', side_effect=drop_stage_worker):
                    with self.assertRaises(safe.UpdateError):
                        updater.apply(source)
                self.assertEqual((install / 'PocketMemory.exe').read_bytes(), pe_bytes() + b'1.0.12')
                self.assertEqual(safe._read_json(updater.receipt_path)['manifest'], old_manifest)
                self.assertEqual((install / 'data/notes/keep.txt').read_bytes(), b'user note')

    def test_commit_accepts_only_verified_missing_worker_and_rollback_restores_original_state(self):
        for old_worker in (False, True):
            with self.subTest(old_worker=old_worker):
                install, source, old_manifest, _ = self.transaction('commit-' + str(old_worker),
                                                                  old_worker=old_worker)
                updater = safe.SafeUpdater(install)
                self.assertEqual(updater.apply(source)['state'], 'pending_health')
                (install / WORKER).unlink()
                committed = updater.commit('Inert transaction attestation; no executable launched')
                self.assertEqual(committed['state'], 'committed')
                self.assertTrue(committed['backups_retained'])
                self.assertEqual(embedded.inspect_executor(install)['executor'], 'embedded')
                self.assertEqual(updater.rollback()['state'], 'rolled_back')
                self.assertEqual((install / WORKER).exists(), old_worker)
                self.assertEqual(safe._read_json(updater.receipt_path)['manifest'], old_manifest)
                self.assertEqual((install / 'PocketMemory.exe').read_bytes(), pe_bytes() + b'1.0.12')
                self.assertEqual((install / 'data/notes/keep.txt').read_bytes(), b'user note')

    def test_commit_rejects_other_runtime_damage_and_keeps_rollback_available(self):
        for variant in ('changed_library', 'extra_library', 'damaged_worker', 'wrong_marker'):
            with self.subTest(variant=variant):
                install, source, _, manifest = self.transaction('reject-' + variant)
                updater = safe.SafeUpdater(install)
                updater.apply(source)
                if variant == 'damaged_worker':
                    (install / WORKER).write_bytes(b'corrupt worker is not missing')
                else:
                    (install / WORKER).unlink()
                    if variant == 'changed_library':
                        (install / '_internal/base_library.zip').write_bytes(b'changed library')
                    elif variant == 'extra_library':
                        put(install / '_internal/undeclared.pyd', b'unlisted code')
                    else:
                        self.change_manifest_file(install, copy.deepcopy(manifest),
                                                  embedded.CAPABILITY_PATH,
                                                  json.dumps(dict(CAPABILITY, executor='unsupported')).encode())
                with self.assertRaises(safe.UpdateError):
                    updater.commit('Must not attest a damaged runtime')
                self.assertEqual(updater.recover()['state'], 'pending_health')
                self.assertEqual(updater.rollback()['state'], 'rolled_back')
                self.assertEqual((install / 'data/notes/keep.txt').read_bytes(), b'user note')
                self.assertEqual((install / 'PocketMemory.exe').read_bytes(), pe_bytes() + b'1.0.12')

    def test_worker_environment_is_detached_without_mutating_parent_environment(self):
        inherited = {
            '_PYI_APPLICATION_HOME_DIR': 'old-runtime', '_PYI_PARENT_PROCESS_LEVEL': '1',
            '_MEIPASS2': 'old-runtime', 'PM_UPDATE_HEALTH_TOKEN': 'old-token',
            'PM_UPDATE_PROBE': '1', 'PYINSTALLER_RESET_ENVIRONMENT': '0',
            'LOCALAPPDATA': 'original-profile', 'PM_UPDATE_CONFIG_PATH': 'original-config',
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            actual = embedded.clean_worker_environment()
            self.assertEqual(dict(os.environ), inherited)
        for key in ('_PYI_APPLICATION_HOME_DIR', '_PYI_PARENT_PROCESS_LEVEL', '_MEIPASS2',
                    'PM_UPDATE_HEALTH_TOKEN', 'PM_UPDATE_PROBE'):
            self.assertNotIn(key, actual)
        self.assertEqual(actual['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertEqual(actual['LOCALAPPDATA'], 'original-profile')
        self.assertEqual(actual['PM_UPDATE_CONFIG_PATH'], 'original-config')


if __name__ == '__main__':
    unittest.main()
