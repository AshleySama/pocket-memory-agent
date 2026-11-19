# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Disposable build/layout checks: PyInstaller is mocked, never builds or runs EXEs."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings
from zipfile import ZipFile, ZipInfo

from update_component_fixture import pe_bytes

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pocket_memory.update_component import WORKER_RELATIVE_PATH
from scripts import check_update_component as check
from scripts import create_zip64 as packaging


def worker_bytes(marker=0):
    return pe_bytes()[:-1] + bytes([marker])


class UpdateBuildGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worker-layout-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expected = self.root / 'built-worker.exe'
        self.expected.write_bytes(worker_bytes())

    def layout(self, name='full', *, update=False):
        root = self.root / name
        worker = root / ('payload' if update else '') / WORKER_RELATIVE_PATH
        worker.parent.mkdir(parents=True)
        worker.write_bytes(worker_bytes())
        return root, worker

    def archive(self, entries, name='fixture.zip'):
        path = self.root / name
        with warnings.catch_warnings(), ZipFile(path, 'w') as archive:
            warnings.simplefilter('ignore', UserWarning)
            for member, data in entries:
                archive.writestr(member, data)
        return path

    def test_full_and_update_zip_keep_the_exact_built_worker(self):
        for update in (False, True):
            with self.subTest(update=update):
                root, worker = self.layout('update' if update else 'full', update=update)
                before = check.check_layout(root, self.expected)
                archive = self.root / (root.name + '.zip')
                with contextlib.redirect_stdout(io.StringIO()):
                    packaging.create_archive(root, archive, expected_worker=self.expected)
                after = check.check_archive(archive, self.expected)
                self.assertEqual(before['sha256'], after['sha256'])
                self.assertEqual(worker.read_bytes(), self.expected.read_bytes())
                self.assertEqual(after['status'], 'ok')

    def test_absent_or_invalid_worker_prevents_archive_creation(self):
        root, worker = self.layout()
        for data in (None, b'', b'MZ', b'NOT-PE'):
            with self.subTest(data=data):
                if data is None:
                    worker.unlink()
                else:
                    worker.write_bytes(data)
                target = self.root / 'must-not-exist.zip'
                with self.assertRaises(ValueError):
                    packaging.create_archive(root, target, expected_worker=self.expected)
                self.assertFalse(target.exists())

    def test_valid_but_different_worker_is_rejected_in_directory_and_zip(self):
        root, worker = self.layout()
        worker.write_bytes(worker_bytes(1))
        with self.assertRaisesRegex(ValueError, 'identity'):
            check.check_layout(root, self.expected)
        archive = self.archive([(WORKER_RELATIVE_PATH, worker_bytes(1))])
        with self.assertRaisesRegex(ValueError, 'identity'):
            check.check_archive(archive, self.expected)

    def test_zip_missing_duplicate_alternative_and_unsafe_worker_members(self):
        good = (WORKER_RELATIVE_PATH, worker_bytes())
        link = ZipInfo(WORKER_RELATIVE_PATH)
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [
            [('README.txt', b'no worker')],
            [good, good],
            [good, ('other/PocketMemoryUpdateWorker.exe', worker_bytes())],
            [(WORKER_RELATIVE_PATH.lower(), worker_bytes())],
            [('one/two/' + WORKER_RELATIVE_PATH, worker_bytes())],
            [('../' + WORKER_RELATIVE_PATH, worker_bytes())],
            [(link, worker_bytes())],
            [(WORKER_RELATIVE_PATH, b'MZ')],
        ]
        for index, entries in enumerate(cases):
            with self.subTest(index=index):
                archive = self.archive(entries, name=f'bad-{index}.zip')
                with self.assertRaises(ValueError):
                    check.check_archive(archive, self.expected)

    def test_zip_postcheck_catches_worker_changed_during_compression(self):
        root, worker = self.layout()
        original_write = ZipFile.write
        def corrupt_write(archive, path, arcname, **kwargs):
            if Path(path).name == Path(WORKER_RELATIVE_PATH).name:
                archive.writestr(arcname, worker_bytes(1))
            else:
                original_write(archive, path, arcname, **kwargs)
        with mock.patch.object(ZipFile, 'write', new=corrupt_write), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'changed'):
                packaging.create_archive(root, self.root / 'changed.zip')
        self.assertEqual(worker.read_bytes(), worker_bytes())
        self.assertFalse((self.root / 'changed.zip').exists())
        self.assertFalse(list(self.root.glob('changed.zip.*.part')))

    def test_existing_archive_is_never_overwritten(self):
        root, _ = self.layout()
        archive = self.root / 'existing.zip'
        archive.write_bytes(b'PREEXISTING-DELIVERY')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            packaging.create_archive(root, archive, expected_worker=self.expected)
        self.assertEqual(archive.read_bytes(), b'PREEXISTING-DELIVERY')
        self.assertFalse(list(self.root.glob('existing.zip.*.part')))

    def test_cli_failure_returns_nonzero(self):
        root, worker = self.layout()
        worker.unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check.main([str(root)]), 2)


POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')


@unittest.skipUnless(POWERSHELL, 'PowerShell is needed to exercise the legacy build entry.')
class LegacyBuildEntryTests(unittest.TestCase):
    def test_retired_entry_stops_before_build_even_with_skip_tests(self):
        with tempfile.TemporaryDirectory(prefix='retired-build-') as folder:
            root=Path(folder); script=root/'build_release.ps1'
            shutil.copy2(ROOT/'scripts/build_release.ps1',script)
            output=root/'must-not-be-created'
            result=subprocess.run([POWERSHELL,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
                                   '-SkipTests','-WithoutModels','-OutputDir',str(output)],
                                  cwd=root,capture_output=True,text=True,errors='replace',timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('prepare_offline_delivery',result.stdout+result.stderr)
            self.assertFalse(output.exists())
            self.assertEqual([p.name for p in root.iterdir()],['build_release.ps1'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
