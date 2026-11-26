"""Focused executor, interrupted-status and actual process-termination checks.

Uses inert local files, never a production EXE, note library or model weights.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from pocket_memory import embedded_update as embedded, safe_update as safe, app_updates
import test_embedded_update as embedded_fixtures
from test_embedded_update import WORKER, MODEL
import test_update_builtin_protocol as builtin_fixtures
from test_safe_update import put


class StabilityTests(unittest.TestCase):
    setUp = embedded_fixtures.EmbeddedExecutorTests.setUp
    runtime = embedded_fixtures.EmbeddedExecutorTests.runtime
    write_receipt = embedded_fixtures.EmbeddedExecutorTests.write_receipt
    change_manifest_file = embedded_fixtures.EmbeddedExecutorTests.change_manifest_file
    builtin = builtin_fixtures.BuiltinProtocolTests.builtin
    fixture = builtin_fixtures.BuiltinProtocolTests.fixture
    program = builtin_fixtures.BuiltinProtocolTests.program
    assert_preserved = builtin_fixtures.BuiltinProtocolTests.assert_preserved

    def test_healthy_bridge_uses_verified_embedded_without_deleting_worker(self):
        root, manifest = self.runtime(worker=True)
        before = (root / WORKER).read_bytes()
        result = embedded.inspect_executor(root)
        self.assertEqual(result['executor'], 'embedded')
        self.assertEqual(result['legacy_component_status'], 'ok')
        stage = self.base / 'stage'; stage.mkdir()
        command, executable = embedded.stage_executor(root, stage)
        self.assertEqual(command, [str(executable), embedded.EMBEDDED_FLAG])
        self.assertFalse((executable.parent / WORKER).exists())
        self.assertEqual((root / WORKER).read_bytes(), before)
        self.assertEqual(embedded.verify_runtime(executable.parent), manifest)

    def test_declared_runtime_damage_never_falls_back_to_valid_worker(self):
        for variant in ('missing_marker', 'bad_marker', 'changed_dll', 'extra_library', 'bad_receipt'):
            with self.subTest(variant=variant):
                root, _ = self.runtime(variant, worker=True)
                if variant == 'missing_marker': (root / embedded.CAPABILITY_PATH).unlink()
                elif variant == 'bad_marker': (root / embedded.CAPABILITY_PATH).write_bytes(b'{}')
                elif variant == 'changed_dll': (root / '_internal/python313.dll').write_bytes(b'bad')
                elif variant == 'extra_library': put(root / '_internal/extra.pyd', b'unknown')
                else: (root / safe.STATE_DIR / 'installed.json').write_bytes(b'{')
                result = embedded.inspect_executor(root)
                self.assertNotEqual(result['status'], 'ok')
                self.assertNotEqual(result.get('executor'), 'standalone')

    def test_worker_corruption_is_not_ignored(self):
        root, _ = self.runtime(worker=True)
        (root / WORKER).write_bytes(b'broken')
        self.assertNotEqual(embedded.inspect_executor(root)['status'], 'ok')

    def service(self, root):
        config = SimpleNamespace(path=self.base/'config.json', data_dir=self.base/'library')
        with mock.patch.object(sys, 'frozen', True, create=True), mock.patch.object(sys, 'executable', str(root/'PocketMemory.exe')):
            return app_updates.AppUpdateService(config, config.data_dir, server_port=5001,
                                               exit_callback=mock.Mock(), current_version='1.0.16')

    def journal(self, root, state):
        data = dict(schema_version=1, transaction='a'*32, version='1.0.17', state=state, operations=[])
        put(root/safe.STATE_DIR/'journal.json', json.dumps(data).encode())

    def test_interrupted_journal_overrides_stale_success_and_blocks_network(self):
        for state in ('prepared', 'applying', 'pending_health', 'rolling_back'):
            with self.subTest(state=state):
                root, _ = self.builtin(state)
                self.journal(root, state)
                put(root/safe.STATE_DIR/'auto-update-result.json', b'{"state":"complete","message":"old success"}')
                service = self.service(root)
                status = service.status()
                self.assertNotIn('last_result', status)
                self.assertFalse(status['can_update'])
                self.assertFalse(status['transaction_recovery']['recovery_completed'])
                with mock.patch.object(app_updates, 'fetch_feed') as fetch, mock.patch.object(app_updates, 'download_package') as download:
                    with self.assertRaises(safe.UpdateError):
                        service._download_and_handoff({}, (service.data_dir, service.config.path, service.config.data_dir))
                    fetch.assert_not_called(); download.assert_not_called()

    def test_corrupt_journal_is_not_treated_as_no_transaction(self):
        root, _ = self.builtin('bad-journal')
        put(root/safe.STATE_DIR/'journal.json', b'{broken')
        self.assertEqual(self.service(root).status()['transaction_recovery']['state'], 'invalid')

    def test_normal_and_rolled_back_journals_allow_check(self):
        root, _ = self.builtin('clean')
        service = self.service(root)
        service._require_settled_transaction()
        self.journal(root, 'rolled_back')
        service._require_settled_transaction()
        self.assertNotIn('transaction_recovery', service.status())

    def test_new_journal_is_observed_before_next_download(self):
        root, _ = self.builtin('late')
        service = self.service(root)
        self.journal(root, 'pending_health')
        self.assertEqual(service.status()['transaction_recovery']['state'], 'pending_health')

    def test_completed_transaction_clears_block_without_inventing_success(self):
        root, _ = self.builtin('finish')
        self.journal(root, 'pending_health')
        service = self.service(root)
        self.journal(root, 'committed')
        status = service.status()
        self.assertEqual(status['state'], 'idle')
        self.assertNotIn('transaction_recovery', status)
        self.assertNotIn('last_result', status)
        self.assertFalse(status['can_update'])

    def test_missing_observed_journal_does_not_clear_recovery_block(self):
        root, _ = self.builtin('disappeared')
        self.journal(root, 'pending_health')
        service = self.service(root)
        (root/safe.STATE_DIR/'journal.json').unlink()
        for _ in range(2):
            self.assertEqual(service.status()['transaction_recovery']['state'], 'invalid')
            with self.assertRaises(safe.UpdateError): service._require_settled_transaction()

    def test_other_completed_transaction_does_not_clear_recovery_block(self):
        root, _ = self.builtin('replaced')
        self.journal(root, 'pending_health')
        service = self.service(root)
        self.journal(root, 'committed')
        path=root/safe.STATE_DIR/'journal.json'
        value=json.loads(path.read_bytes()); value['transaction']='b'*32
        put(path,json.dumps(value).encode())
        self.assertEqual(service.status()['transaction_recovery']['state'], 'invalid')
        with self.assertRaises(safe.UpdateError): service._require_settled_transaction()

    def test_real_process_termination_then_external_recovery_preserves_files(self):
        # Real child termination bypasses Python exception handlers. Fresh
        # recovery process must use persisted journal, never parent memory.
        child_code = '''
import sys,time
from pathlib import Path
from pocket_memory.safe_update import SafeUpdater
install,source,signal,phase=map(str,sys.argv[1:])
up=SafeUpdater(install)
if phase == 'after_backup':
 original=up._perform
 def perform(journal,op):
  original(journal,op)
  if op == {'kind':'backup','path':'PocketMemory.exe'}:
   Path(signal).write_text('ready'); time.sleep(120)
 up._perform=perform
up.apply(source)
Path(signal).write_text('ready'); time.sleep(120)
'''
        root = Path(__file__).resolve().parents[2]
        environment = dict(os.environ, PYTHONPATH=str(root))
        for phase in ('after_backup', 'pending_health'):
            with self.subTest(phase=phase):
                install, source, _, req, old, _ = self.fixture(phase)
                before = self.program(install, old)
                signal = self.base/(phase+'.ready')
                process = subprocess.Popen([sys.executable,'-c',child_code,str(install),str(source),str(signal),phase],
                    cwd=root,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    import time
                    deadline=time.monotonic()+20
                    while not signal.exists() and process.poll() is None and time.monotonic()<deadline: time.sleep(.05)
                    if not signal.exists():
                        process.terminate(); output=process.communicate(timeout=10)
                        self.fail(repr(output))
                    process.terminate(); process.communicate(timeout=10)
                    action='rollback' if phase=='pending_health' else 'recover'
                    code='from pocket_memory.safe_update import SafeUpdater; import sys,json; u=SafeUpdater(sys.argv[1]); print(json.dumps(getattr(u,sys.argv[2])())); print(json.dumps(u.recover()))'
                    result=subprocess.run([sys.executable,'-c',code,str(install),action],cwd=root,env=environment,
                        capture_output=True,text=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertTrue(all(json.loads(s)['state']=='rolled_back' for s in result.stdout.splitlines()))
                    self.assertEqual(self.program(install,old),before)
                    self.assert_preserved(install,req)
                finally:
                    if process.poll() is None: process.terminate(); process.communicate(timeout=10)


if __name__ == '__main__': unittest.main()
