# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Tiny fake builds exercise layout and updater integration, not real packaging."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / '.'
sys.path.insert(0, str(CANDIDATE))
spec = importlib.util.spec_from_file_location('delivery_prepare', CANDIDATE / 'scripts/prepare_offline_delivery.py')
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)
from pocket_memory.safe_update import SafeUpdater, UpdateError


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='delivery-fixture-', dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.build = self.root / 'fake-build'
        self.models = self.root / 'fake-models'
        for relative, data in {
            'PocketMemory.exe': b'FAKE-APPLICATION-NOT-EXECUTABLE',
            '_internal/runtime.dll': b'FAKE-RUNTIME',
            '_internal/frontend/index.html': b'<p>fixture</p>',
            'README.txt': b'fixture', 'data/private.db': b'PRIVATE',
            'config.json': b'{"private":"KEEP"}',
            '_internal/models/Qwen3-4B/model.gguf': b'FAKE-MODEL',
        }.items():
            path = self.build / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.models / 'Qwen3-4B').mkdir(parents=True)
        (self.models / 'Qwen3-4B/model.gguf').write_bytes(b'FAKE-MODEL')
        (self.models / 'tokenizer.json').write_bytes(b'{}')
        self.helper = self.root / 'fake-updater.exe'
        self.helper.write_bytes(b'NOT-EXECUTED')

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, mode, dest):
        return delivery.prepare(self.build, self.models, dest, version='fixture.2',
                                mode=mode, updater_exe=self.helper, schema_id='legacy-unversioned')

    def test_full_layout_preserves_source_excludes_user_data(self):
        dest = self.root / 'full'
        self.prepare('full', dest)
        self.assertEqual((dest / 'models/Qwen3-4B/model.gguf').read_bytes(), b'FAKE-MODEL')
        self.assertFalse((dest / '_internal/models').exists())
        self.assertEqual((dest / '_internal/PocketMemoryUpdateWorker.exe').read_bytes(), b'NOT-EXECUTED')
        self.assertFalse((dest / 'PocketMemoryUpdate.exe').exists())
        self.assertFalse((dest / 'data').exists())
        self.assertFalse((dest / 'config.json').exists())
        self.assertEqual((self.build / 'data/private.db').read_bytes(), b'PRIVATE')

    def test_update_has_no_models_and_manifest_matches_core(self):
        full, update = self.root / 'full', self.root / 'update'
        self.prepare('full', full)
        result = self.prepare('update', update)
        self.assertGreater(result['model_bytes_omitted'], 0)
        self.assertFalse(any(update.rglob('*.gguf')))
        manifest = json.loads((update / 'update-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['model_requirements']), 2)
        # Check against the real update consumer rather than another schema copy.
        checked = SafeUpdater(full).check(update)
        self.assertIsNotNone(checked)

    def test_duplicate_rapidocr_weights_omitted_external_resources_still_verified(self):
        weights = (
            'ch_PP-OCRv4_det_infer.onnx',
            'ch_PP-OCRv4_rec_infer.onnx',
            'ch_ppocr_mobile_v2.0_cls_infer.onnx',
        )
        packaged = self.build / '_internal/rapidocr/models'
        external = self.models / 'rapidocr'
        packaged.mkdir(parents=True)
        external.mkdir()
        expected = {}
        for name in weights:
            (packaged / name).write_bytes(b'REDUNDANT-PACKAGE-WEIGHT')
            expected[name] = b'EXTERNAL-' + name.encode('ascii')
            (external / name).write_bytes(expected[name])
        for name in ('ppocr_keys_v1.txt', 'ppocrv5_dict.txt'):
            (packaged / name).write_bytes(b'PACKAGE-KEYS')
        expected['ppocr_keys_v1.txt'] = b'EXTERNAL-KEYS'
        (external / 'ppocr_keys_v1.txt').write_bytes(expected['ppocr_keys_v1.txt'])

        full, update = self.root / 'full', self.root / 'update'
        self.prepare('full', full)
        self.prepare('update', update)
        manifest = json.loads((update / 'update-manifest.json').read_text(encoding='utf-8'))
        program_paths = {item['path'] for item in manifest['files']}
        for destination in (full, update / 'payload'):
            for name in weights:
                relative = '_internal/rapidocr/models/' + name
                self.assertFalse((destination / relative).exists())
                self.assertNotIn(relative, program_paths)
                self.assertTrue((packaged / name).is_file())
            for name in ('ppocr_keys_v1.txt', 'ppocrv5_dict.txt'):
                self.assertEqual((destination / '_internal/rapidocr/models' / name).read_bytes(), b'PACKAGE-KEYS')

        requirements = {item['path']: item for item in manifest['model_requirements']}
        for name, data in expected.items():
            item = requirements['rapidocr/' + name]
            self.assertEqual(item['size'], len(data))
            self.assertEqual(item['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual((full / 'models/rapidocr' / name).read_bytes(), data)
        SafeUpdater(full).check(update)
        # Same-size corruption must still fail through the real consumer's hash
        # check; excluding the redundant copy must not weaken external resources.
        first = weights[0]
        (full / 'models/rapidocr' / first).write_bytes(b'!' + expected[first][1:])
        with self.assertRaises(UpdateError):
            SafeUpdater(full).check(update)

    def test_no_overwrite_and_no_output_inside_inputs(self):
        with self.assertRaises(ValueError):
            self.prepare('full', self.build / 'nested')
        dest = self.root / 'full'
        self.prepare('full', dest)
        with self.assertRaises(ValueError):
            self.prepare('full', dest)

    def test_incomplete_model_rejected_before_writing(self):
        (self.models / 'download.gguf.part').write_bytes(b'PARTIAL')
        dest = self.root / 'full'
        with self.assertRaises(ValueError):
            self.prepare('full', dest)
        self.assertFalse(dest.exists())

    def test_update_requires_standalone_helper(self):
        with self.assertRaises(ValueError):
            delivery.prepare(self.build, self.models, self.root/'update',
                             version='fixture', mode='update', schema_id='legacy-unversioned')


if __name__ == '__main__':
    unittest.main(verbosity=2)
