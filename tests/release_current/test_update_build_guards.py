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
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worker-build-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'isolated-source'
        self.scripts = self.source / 'scripts'
        self.scripts.mkdir(parents=True)
        python_stub = self.source / '.venv/Scripts/python.exe'
        python_stub.parent.mkdir(parents=True)
        python_stub.write_bytes(b'NEVER-EXECUTED')
        for name in ('check_update_component.py', 'create_zip64.py', 'PACKAGE_README_NO_MODELS.txt',
                     'MODELS_README.txt'):
            shutil.copy2(ROOT / 'scripts' / name, self.scripts / name)
        self.fixture = self.root / 'worker-fixture.exe'
        self.fixture.write_bytes(worker_bytes())
        self.output = self.root / 'release'
        self.calls = self.root / 'python-calls.jsonl'
        (self.root / 'temp').mkdir()
        script = (ROOT / 'scripts/build_release.ps1').read_text(encoding='utf-8-sig')
        marker = '& $python -m PyInstaller --version | Out-Null'
        self.assertEqual(script.count(marker), 1)
        # Keep actual build control flow and scripts. Only replace the Python
        # command dispatcher so no invocation can reach real PyInstaller.
        script = script.replace(marker, "$python = 'Invoke-FakePython'\n" + marker)
        (self.scripts / 'build_release.ps1').write_text(script, encoding='utf-8-sig')
        harness = r'''
function Invoke-FakePython {
    $call = @{ argv = @($args); cwd = (Get-Location).Path }
    Add-Content -LiteralPath $env:PM_TEST_CALLS -Value (ConvertTo-Json -InputObject $call -Compress)
    if ($args[0] -eq '-c') {
        Write-Output '0.0.1-guard-fixture'
        $global:LASTEXITCODE = 0
        return
    }
    if ($args[0] -eq '-m') {
        if ($args[1] -ne 'PyInstaller') { throw 'Unexpected Python module in mock build.' }
        if ($args -contains '--version') {
            Write-Output 'mock-pyinstaller'
            $global:LASTEXITCODE = 0
            return
        }
        $dist = $args[[array]::IndexOf($args, '--distpath') + 1]
        if ($args -contains '--onefile') {
            if ($env:PM_TEST_MODE -eq 'worker-build-failed') {
                $global:LASTEXITCODE = 19
                return
            }
            New-Item -ItemType Directory -Path $dist -Force | Out-Null
            $worker = Join-Path $dist 'PocketMemoryUpdateWorker.exe'
            if ($env:PM_TEST_MODE -eq 'worker-empty') {
                [IO.File]::WriteAllBytes($worker, [byte[]]@())
            } elseif ($env:PM_TEST_MODE -ne 'worker-missing') {
                Copy-Item -LiteralPath $env:PM_TEST_WORKER -Destination $worker
            }
        } else {
            $app = Join-Path $dist 'PocketMemory'
            New-Item -ItemType Directory -Path (Join-Path $app '_internal') -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $app 'PocketMemory.exe') -Value 'FAKE-APP-NEVER-EXECUTED'
            Set-Content -LiteralPath (Join-Path $app '_internal/runtime.dll') -Value 'FAKE-RUNTIME'
        }
        $global:LASTEXITCODE = 0
        return
    }
    if ($env:PM_TEST_MODE -eq 'copy-corrupt' -and
        (Split-Path -Leaf $args[0]) -eq 'check_update_component.py' -and
        (Split-Path -Leaf $args[1]) -eq 'PocketMemory') {
        $copied = Join-Path $args[1] '_internal/PocketMemoryUpdateWorker.exe'
        $bytes = [IO.File]::ReadAllBytes($copied)
        $bytes[$bytes.Length - 1] = 1
        [IO.File]::WriteAllBytes($copied, $bytes)
    }
    & $env:PM_TEST_PYTHON @args
    $global:LASTEXITCODE = $LASTEXITCODE
}
& $env:PM_TEST_BUILD -SkipTests -WithoutModels -OutputDir $env:PM_TEST_OUTPUT
'''
        self.harness = self.root / 'mock-build.ps1'
        self.harness.write_text(harness, encoding='utf-8-sig')

    def run_build(self, mode):
        environment = dict(os.environ, PM_TEST_MODE=mode, PM_TEST_WORKER=str(self.fixture),
                           PM_TEST_PYTHON=sys.executable, PM_TEST_OUTPUT=str(self.output),
                           PM_TEST_CALLS=str(self.calls),
                           PM_TEST_BUILD=str(self.scripts / 'build_release.ps1'),
                           PYTHONPATH=str(ROOT), TEMP=str(self.root / 'temp'), TMP=str(self.root / 'temp'))
        return subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(self.harness)], cwd=self.root, env=environment,
                              capture_output=True, text=True, errors='replace', timeout=60)

    def test_skip_tests_still_builds_and_checks_worker_before_success(self):
        result = self.run_build('ok')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        package = self.output / 'PocketMemory-0.0.1-guard-fixture-no-models'
        self.assertEqual((package / WORKER_RELATIVE_PATH).read_bytes(), worker_bytes())
        check.check_archive(Path(str(package) + '.zip'), self.fixture)
        calls = [json.loads(line) for line in self.calls.read_text(encoding='utf-8-sig').splitlines()]
        worker_build = [call for call in calls if '--onefile' in call['argv']]
        self.assertEqual(len(worker_build), 1)
        for flag in ('--noconsole', '--noupx', '--paths', '--specpath'):
            self.assertIn(flag, worker_build[0]['argv'])
        self.assertTrue(all(Path(call['cwd']) == self.source for call in calls))
        self.assertGreaterEqual(sum(Path(call['argv'][0]).name == 'check_update_component.py'
                                    for call in calls), 4)

    def test_skip_tests_cannot_bypass_worker_build_or_copy_failure(self):
        for mode in ('worker-build-failed', 'worker-missing', 'worker-empty', 'copy-corrupt'):
            with self.subTest(mode=mode):
                result = self.run_build(mode)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(list(self.output.glob('*.zip')))
                self.assertFalse(list(self.output.glob('*-SHA256.txt')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
