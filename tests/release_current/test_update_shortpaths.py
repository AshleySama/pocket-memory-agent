# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Actual Windows aliases; tiny update/rollback fixtures, no user installation."""
import ctypes
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_safe_update as fixtures
up, put = fixtures.up, fixtures.put


@unittest.skipUnless(os.name == 'nt', 'Windows 8.3 alias behavior')
class ShortPathTests(fixtures.SafetyTests):
    # Do not repeat the inherited safety suite here.
    def short(self, path):
        path.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(['fsutil', 'file', 'setshortname', str(path), 'PMTEST~1'],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        function = kernel.GetShortPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        self.assertTrue(function(str(path), buffer, len(buffer)))
        self.assertIn('~', buffer.value)
        return Path(buffer.value)

    def test_short_temp_profile_config_and_data_preserved_through_update_rollback(self):
        long = self.base / 'Long Local Profile Directory'
        short = self.short(long)
        install, source, _ = self.fixture('Long Local Profile Directory/fixture')
        short_install = short / 'fixture/install'
        short_source = short / 'fixture/update'
        data = long / 'User Notes'; data.mkdir()
        put(data / 'private.txt', b'keep private bytes')
        cfg = long / 'PocketMemory/config.json'
        put(cfg, ('{"data_dir":' + __import__('json').dumps(str(short/'User Notes')) + '}').encode())
        config_bytes = cfg.read_bytes()
        with mock.patch.dict(os.environ, {'LOCALAPPDATA':str(short),'TEMP':str(short),'TMP':str(short)}):
            updater = up.SafeUpdater(short_install, protected_data_dirs=[short/'User Notes'])
            self.assertEqual(updater.install, install)
            self.assertEqual(up._absolute(short/'new/nested/file.json'), long/'new/nested/file.json')
            self.assertEqual(updater.apply(short_source)['state'], 'pending_health')
            self.assertEqual(updater.rollback()['state'], 'rolled_back')
            self.assert_original(install)
            self.assertEqual(cfg.read_bytes(), config_bytes)
            self.assertEqual((data/'private.txt').read_bytes(), b'keep private bytes')

    def test_short_alias_cannot_bypass_overlap(self):
        long = self.base/'Long Installation'; short=self.short(long)
        self.assertTrue(up._overlap(up._absolute(short), up._absolute(long/'data')))

    def test_nonexistent_or_literal_tilde_and_namespace_rejected(self):
        for name in ['MISSING~1/file','bad.','bad ','foo:stream']:
            with self.subTest(name=name), self.assertRaises(up.UpdateError):
                up._absolute(self.base/name)
        literal=self.base/'literal~name';literal.mkdir()
        with self.assertRaises(up.UpdateError):up._absolute(literal)
        with self.assertRaises(up.UpdateError):up._absolute('\\\\?\\'+str(self.base))
        with self.assertRaises(up.UpdateError):up._relative('_internal/FOO~1/a')

    def test_reparse_point_is_checked_before_expansion(self):
        outside=self.base/'outside';outside.mkdir()
        link=self.base/'linked'
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True,check=True)
        try:
            with self.assertRaises(up.UpdateError):up._absolute(link/'new/file')
        finally:link.rmdir()


# Only run the new cases here; the existing safety suite is run once separately.
for _name in list(vars(fixtures.SafetyTests)):
    if _name.startswith('test_'):
        setattr(ShortPathTests, _name, None)

if __name__ == '__main__':unittest.main()
