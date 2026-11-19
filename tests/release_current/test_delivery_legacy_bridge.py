# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Legacy envelope preparation with inert PE fixtures, not native acceptance.

The receipt fixtures model a release runner's attestation. They do not claim
that a fake executable supports any protocol; final EXE probes remain required.
"""
import copy
import json
from pathlib import Path
import sys
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pocket_memory import embedded_update, safe_update
from scripts import prepare_offline_delivery as delivery
from scripts import check_update_component as delivery_check
import test_embedded_update as embedded_fixtures
import test_update_builtin_protocol as builtin_fixtures
from test_safe_update import put


class LegacyBridgeDeliveryTests(TestCase):
    runtime = embedded_fixtures.EmbeddedExecutorTests.runtime

    def setUp(self):
        embedded_fixtures.EmbeddedExecutorTests.setUp(self)
        self.build, _ = builtin_fixtures.BuiltinProtocolTests.builtin(
            self, 'built-v2', version='1.0.16', receipt=False)
        self.models = self.base / 'models'
        put(self.models / 'Qwen/model.gguf', b'INERT MODEL FIXTURE')
        self.worker = self.base / 'new-worker.exe'
        put(self.worker, embedded_fixtures.pe_bytes() + b'INERT NEW WORKER')
        self.receipt_path = self.base / 'worker-validation.json'
        self.receipt = dict(
            schema_version=1, app_id='pocket-memory',
            kind='legacy-bridge-worker-validation', version='1.0.16',
            verification='frozen-worker-capability-probe',
            main_sha256=delivery.digest(self.build / 'PocketMemory.exe'),
            worker=dict(size=self.worker.stat().st_size, sha256=delivery.digest(self.worker)),
            capabilities=dict(schema_version=1, app_id='pocket-memory', version='1.0.16',
                              manifest_schemas=[1, 2], executor='standalone'))
        self.write_receipt(self.receipt)

    def write_receipt(self, value):
        self.receipt_path.write_text(json.dumps(value), encoding='utf-8')

    def prepare(self, destination='bridge', **changes):
        arguments = dict(version='1.0.16', mode='update', schema_id='legacy-unversioned',
                         updater_exe=self.worker, executor='legacy-bridge',
                         bridge_worker_receipt=self.receipt_path)
        arguments.update(changes)
        return delivery.prepare(self.build, self.models, self.base / destination, **arguments)

    def test_explicit_bridge_has_v1_envelope_and_bound_worker_without_changing_build(self):
        marker_path = self.build / embedded_update.CAPABILITY_PATH
        original_marker = marker_path.read_bytes()
        result = self.prepare()
        output = self.base / 'bridge'
        payload = output / 'payload'
        manifest = json.loads((output / safe_update.MANIFEST_NAME).read_text('utf-8'))
        self.assertEqual(manifest['schema_version'], 1)
        self.assertNotIn('update_executor', manifest)
        self.assertEqual(json.loads((payload / embedded_update.CAPABILITY_PATH).read_text('utf-8')),
                         dict(schema_version=1, executor='main-exe-v1', python_dll='python313.dll'))
        self.assertEqual((payload / embedded_fixtures.WORKER).read_bytes(), self.worker.read_bytes())
        entries = {item['path']: item for item in manifest['files']}
        for relative in (embedded_update.CAPABILITY_PATH, embedded_fixtures.WORKER):
            self.assertEqual(entries[relative], delivery.record(payload / relative, relative))
        self.assertEqual(marker_path.read_bytes(), original_marker)
        self.assertFalse((self.build / embedded_fixtures.WORKER).exists())
        self.assertFalse(any(output.rglob('*.gguf')))
        self.assertFalse(list(output.rglob(self.receipt_path.name)))
        self.assertEqual(delivery_check.check_layout(output)['manifest_schema'], 1)
        self.assertEqual(result['executor'], 'legacy-bridge')
        self.assertEqual(result['bridge_worker_receipt_sha256'], delivery.digest(self.receipt_path))

    def test_bridge_does_not_change_standard_v2_full_or_update_delivery(self):
        self.prepare()
        for mode in ('full', 'update'):
            with self.subTest(mode=mode):
                destination = self.base / ('v2-' + mode)
                delivery.prepare(self.build, self.models, destination, version='1.0.16',
                                 mode=mode, schema_id='legacy-unversioned')
                self.assertEqual(delivery_check.check_layout(destination)['manifest_schema'], 2)
                program_root = destination / 'payload' if mode == 'update' else destination
                marker = json.loads((program_root / embedded_update.CAPABILITY_PATH).read_text('utf-8'))
                self.assertEqual(marker['schema_version'], 2)
                self.assertEqual(marker['manifest_schemas'], [1, 2])
                self.assertFalse(list(destination.rglob('PocketMemoryUpdateWorker.exe')))

    def test_bridge_requires_explicit_update_mode_worker_and_receipt(self):
        cases = ({'mode': 'full'}, {'bridge_worker_receipt': None}, {'updater_exe': None},
                 {'executor': 'standalone'}, {'executor': 'main-exe-v1', 'updater_exe': None})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare(**changes)
            self.assertFalse((self.base / 'bridge').exists())

    def test_ordinary_standalone_still_cannot_relabel_v2_build(self):
        with self.assertRaisesRegex(ValueError, '不能重贴为旧协议'):
            self.prepare(executor='standalone', bridge_worker_receipt=None)
        self.assertFalse((self.base / 'bridge').exists())

    def test_bridge_requires_original_v2_build_capability(self):
        path = self.build / embedded_update.CAPABILITY_PATH
        original = json.loads(path.read_text('utf-8'))
        cases = [dict(schema_version=1, executor='main-exe-v1', python_dll='python313.dll'),
                 dict(original, manifest_schemas=[1]), dict(original, manifest_schemas=[True, 2])]
        for marker in cases:
            path.write_text(json.dumps(marker), encoding='utf-8')
            with self.subTest(marker=marker), self.assertRaises((ValueError, safe_update.UpdateError)):
                self.prepare()
            self.assertFalse((self.base / 'bridge').exists())

    def test_receipt_rejects_legacy_worker_claims_and_wrong_build_identity(self):
        cases = []
        for key, value in (('schema_version', True), ('version', '1.0.14'),
                           ('verification', 'not-an-executable-probe'), ('main_sha256', '0' * 64),
                           ('extra', 'undeclared')):
            cases.append(dict(self.receipt, **{key: value}))
        for key, value in (('size', self.worker.stat().st_size + 1), ('sha256', '0' * 64)):
            item = copy.deepcopy(self.receipt)
            item['worker'][key] = value
            cases.append(item)
        for key, value in (('schema_version', True), ('version', '1.0.14'),
                           ('manifest_schemas', [1]), ('manifest_schemas', [True, 2]),
                           ('executor', 'main-exe-v1')):
            item = copy.deepcopy(self.receipt)
            item['capabilities'][key] = value
            cases.append(item)
        for receipt in cases:
            self.write_receipt(receipt)
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse((self.base / 'bridge').exists())

    def test_previously_valid_receipt_cannot_authorize_changed_executable_bytes(self):
        for executable in (self.build / 'PocketMemory.exe', self.worker):
            original = executable.read_bytes()
            executable.write_bytes(original + b'UNVERIFIED CHANGE')
            with self.subTest(executable=executable), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse((self.base / 'bridge').exists())
            executable.write_bytes(original)

    def test_bridge_missing_payload_worker_still_fails_exact_inventory(self):
        self.prepare()
        output = self.base / 'bridge'
        (output / 'payload' / embedded_fixtures.WORKER).unlink()
        with self.assertRaisesRegex(ValueError, 'missing='):
            delivery_check.check_layout(output)
