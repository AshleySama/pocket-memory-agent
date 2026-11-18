# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

SPEC=importlib.util.spec_from_file_location('start_repair',Path(__file__).resolve().parents[2]/'scripts/repair_legacy_update_start.py')
repair=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(repair)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.install=self.root/'install';self.install.mkdir()
        (self.install/'PocketMemory.exe').write_bytes(b'fixture')
        self.local=self.root/'profile';self.local.mkdir()
        self.env={'TEMP':str(self.root),'TMP':str(self.root),'LOCALAPPDATA':str(self.local)}
    def tearDown(self):self.tmp.cleanup()

    def test_environment_only_and_exact_config_preserved(self):
        cfg=self.root/'custom.json';cfg.write_text(json.dumps({'data_dir':str(self.root/'data')}))
        before=cfg.read_bytes();self.env['PM_UPDATE_CONFIG_PATH']=str(cfg)
        original=dict(self.env)
        exe,env=repair.prepare(self.install,self.env)
        self.assertEqual(exe,self.install/'PocketMemory.exe')
        self.assertEqual(env['PM_UPDATE_CONFIG_PATH'],str(cfg))
        self.assertEqual(original,self.env);self.assertEqual(before,cfg.read_bytes())

    def test_raw_short_config_is_not_silently_rewritten(self):
        cfg=self.local/'PocketMemory/config.json';cfg.parent.mkdir()
        cfg.write_text(json.dumps({'data_dir':str(self.root/'DATA~1')}));before=cfg.read_bytes()
        with self.assertRaisesRegex(repair.UpdateError,'原配置'):
            repair.prepare(self.install,self.env)
        self.assertEqual(before,cfg.read_bytes())

    def test_check_only_does_not_stop_or_launch(self):
        with patch.dict(os.environ,self.env), patch.object(repair.subprocess,'run') as stop, patch.object(repair.subprocess,'Popen') as launch:
            self.assertEqual(repair.run(self.install,check_only=True)['state'],'checked')
        stop.assert_not_called();launch.assert_not_called()
        self.assertFalse((self.install/'PocketMemory.exe.config').exists())

    def test_existing_process_prevents_launch_without_forced_kill(self):
        with patch.dict(os.environ,self.env), patch.object(repair.subprocess,'run',return_value=Mock(returncode=0)), patch.object(repair.subprocess,'Popen') as launch, patch.object(repair,'running_install_processes',return_value=[123]), patch.object(repair.time,'monotonic',side_effect=[0,31]):
            with self.assertRaisesRegex(repair.UpdateError,'没有强制'):
                repair.run(self.install)
        launch.assert_not_called()

if __name__=='__main__':unittest.main()
