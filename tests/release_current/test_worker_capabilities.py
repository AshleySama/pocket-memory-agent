# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import update_worker as worker
from pocket_memory.version import APP_VERSION


class WorkerCapabilityTests(unittest.TestCase):
    def test_probe_does_not_run_or_finalize_update(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'probe.json'
            with mock.patch.object(worker, 'run_worker') as run, mock.patch.object(worker, 'finalize_existing') as finish:
                self.assertEqual(worker.main(['--describe-capabilities', str(output)]), 0)
                run.assert_not_called()
                finish.assert_not_called()
            self.assertEqual(json.loads(output.read_text('utf-8')), dict(
                schema_version=1, app_id='pocket-memory', version=APP_VERSION,
                manifest_schemas=[1, 2], executor='standalone'))

    def test_probe_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'keep.json'
            output.write_text('preserve', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                worker.main(['--describe-capabilities', str(output)])
            self.assertEqual(output.read_text('utf-8'), 'preserve')

    def test_probe_and_update_modes_cannot_be_mixed(self):
        for args in (['--request', 'request.json', '--describe-capabilities', 'probe.json'],
                     ['--describe-capabilities', 'probe.json', '--finalize-existing']):
            with self.assertRaises(SystemExit):
                worker.main(args)
