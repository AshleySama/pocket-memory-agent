"""Failure/retry/diagnostic contracts. Inert fixtures; no real worker or network."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pocket_memory import app_updates as u, update_failure as f
from pocket_memory import safe_update as s
from update_component_fixture import pe_bytes


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root/'install'
        self.helper = self.install/'_internal/PocketMemoryUpdateWorker.exe'
        self.helper.parent.mkdir(parents=True)
        self.helper.write_bytes(pe_bytes())
        (self.install/'PocketMemory.exe').write_bytes(b'inert old program')
        self.data = self.root/'notes'; self.data.mkdir()
        (self.data/'keep.txt').write_bytes(b'private note')
        self.model = self.install/'_internal/models/model.gguf'
        self.model.parent.mkdir(); self.model.write_bytes(b'inert model')
        self.config = SimpleNamespace(path=self.root/'config.json', data_dir=self.data)
        self.config.path.write_text('{}')
        self.service = self.create_service()
        self.feed = {'published':True, 'version':'1.0.17', 'notes':'fixture',
                     'package': {'size':10,'sha256':'a'*64,'url':'https://'+u.TRUSTED_HOST+'/PocketMemory-1.0.17-Program.zip'}}

    def create_service(self):
        with patch.object(sys,'frozen',True,create=True), patch.object(sys,'executable',str(self.install/'PocketMemory.exe')):
            return u.AppUpdateService(self.config,self.data,server_port=1234,exit_callback=Mock(),current_version='1.0.16')

    def wait(self, service=None):
        service = service or self.service
        end = time.monotonic()+5
        while service._busy and time.monotonic()<end: time.sleep(.005)
        self.assertFalse(service._busy)
        return service.status()

    def unchanged(self):
        self.assertEqual((self.install/'PocketMemory.exe').read_bytes(), b'inert old program')
        self.assertEqual((self.data/'keep.txt').read_bytes(), b'private note')
        self.assertEqual(self.model.read_bytes(), b'inert model')
        self.assertEqual(self.config.path.read_text(), '{}')
        self.service.exit_callback.assert_not_called()

    def induce_failure(self, exc):
        self.service._phase='verify'
        self.service._state['last_result']={'state':'complete','message':'old success'}
        def bad(): raise exc
        self.service._begin(bad)
        return self.wait()

    def test_integrity_failure_stops_check_start_and_direct_handoff_across_restart(self):
        status=self.induce_failure(s.UpdateValidationError('private path should not export','inventory_mismatch'))
        self.assertEqual(status['update_failure']['code'],'inventory_mismatch')
        self.assertNotIn('last_result',status)
        self.assertTrue(status['failure_record_saved'])
        restarted=self.create_service()
        for service in (self.service,restarted):
            with patch.object(u,'fetch_feed') as fetch,patch.object(u,'download_package') as download,patch.object(u.subprocess,'Popen') as popen:
                for _ in range(2):
                    self.assertTrue(service.check_async()['retry_blocked'])
                    with self.assertRaises(s.UpdateError): service.start_update()
                    with self.assertRaises(s.UpdateError): service._download_and_handoff(self.feed,(self.data,self.config.path,self.data))
                fetch.assert_not_called();download.assert_not_called();popen.assert_not_called()
        self.unchanged()

    def test_missing_component_is_not_restored_and_startup_recheck_does_not_unlock(self):
        self.helper.unlink()
        svc=self.create_service()
        self.assertEqual(svc.status()['update_failure']['code'],'missing')
        self.assertFalse(self.helper.exists())
        self.helper.write_bytes(pe_bytes())  # Fixture of changed external state, not product recovery.
        status=svc.check_async()
        self.assertEqual(status['installation_integrity']['status'],'ok')
        self.assertTrue(status['retry_blocked']);self.unchanged()

    def test_component_loss_after_offer_blocks_before_network_or_execution(self):
        with patch.object(u,'fetch_feed',return_value=self.feed):
            self.service.check_async(); self.assertTrue(self.wait()['can_update'])
        self.helper.unlink()
        with patch.object(u,'fetch_feed') as fetch,patch.object(u,'download_package') as dl,patch.object(u.subprocess,'Popen') as popen:
            self.service.start_update(); result=self.wait()
        self.assertEqual(result['update_failure']['code'],'missing')
        fetch.assert_not_called();dl.assert_not_called();popen.assert_not_called();self.unchanged()

    def test_permission_failure_distinct_with_errno_and_no_private_exception(self):
        status=self.induce_failure(PermissionError(13,'denied C:/Users/private-owner/notes token=secret'))
        self.assertEqual(status['diagnostic']['failure'],{'code':'unreadable','stage':'verify','errno':13})
        self.assertNotIn('private-owner',status['message']);self.unchanged()

    def test_diagnostic_whitelist_ignores_private_fields_and_forged_status(self):
        state={'current_version':'1.0.16','retry_blocked':True,'update_failure':{
            'code':'missing','stage':'verify','path':'C:/Users/private-owner/a','token':'secret','errno':2},
            'installation_integrity':{'status':'missing','path':'C:/private'},
            'last_result':{'message':'secret'},'notes':'private note','config_path':'C:/private',
            'transaction_recovery':{'state':'invalid','detail':'private note','journal_path':'C:/private'}}
        raw=json.dumps(f.diagnostic(state))
        for word in ('secret','private','path','notes','token'): self.assertNotIn(word,raw)
        self.assertEqual(f.diagnostic({'current_version':'secret/path','update_failure':{'code':'secret','stage':'private'}})['current_version'],'unknown')

    def test_failure_receipt_has_only_allowlisted_facts(self):
        self.induce_failure(PermissionError(13,'C:/private/token=secret'))
        raw=self.service._hold_path.read_text('utf8')
        self.assertNotIn('private',raw);self.assertNotIn('secret',raw)
        self.assertEqual(set(json.loads(raw)),{'schema_version','failure'})

    def test_corrupt_pause_record_fails_closed(self):
        self.induce_failure(s.UpdateError('bad'))
        self.service._hold_path.write_text('{broken')
        restarted=self.create_service()
        self.assertEqual(restarted.status()['update_failure']['code'],'invalid_hold')
        with patch.object(u,'fetch_feed') as fetch: restarted.check_async()
        fetch.assert_not_called()

    def test_persistence_failure_keeps_current_session_paused_and_reports_unsaved(self):
        with patch.object(u,'_atomic_json',side_effect=PermissionError(13,'private')):
            status=self.induce_failure(s.UpdateError('bad'))
        self.assertTrue(status['retry_blocked']);self.assertFalse(status['failure_record_saved'])
        with self.assertRaises(s.UpdateError): self.service.start_update()

    def test_transient_feed_failure_not_no_updates_and_not_permanent_pause(self):
        with patch.object(u,'fetch_feed',side_effect=u.UpdateFeedUnavailable('offline')):
            self.service.check_async();status=self.wait()
        self.assertEqual(status['state'],'unavailable');self.assertNotIn('暂无可用',status['message'])
        self.assertFalse(status.get('retry_blocked',False))
        with patch.object(u,'fetch_feed',return_value=self.feed):
            self.service.check_async();self.assertTrue(self.wait()['can_update'])

    def test_offer_changes_require_recheck_without_persistent_hold(self):
        for changed in ({'published': False}, dict(self.feed, notes='updated release notes')):
            with self.subTest(changed=changed):
                service = self.create_service()
                with patch.object(u, 'fetch_feed', return_value=self.feed):
                    service.check_async()
                    self.assertTrue(self.wait(service)['can_update'])
                with patch.object(u, 'fetch_feed', return_value=changed), \
                        patch.object(u, 'download_package') as download, \
                        patch.object(u.subprocess, 'Popen') as popen:
                    service.start_update()
                    status = self.wait(service)
                self.assertEqual(status['state'], 'error')
                self.assertIn('重新检查', status['message'])
                self.assertFalse(status['has_update'])
                self.assertFalse(status['can_update'])
                self.assertFalse(status.get('retry_blocked', False))
                self.assertIsNone(service._offer)
                self.assertFalse(service._hold_path.exists())
                download.assert_not_called(); popen.assert_not_called()
                service.exit_callback.assert_not_called()
                with self.assertRaises(s.UpdateError): service.start_update()
                for resumed in (service, self.create_service()):
                    with patch.object(u, 'fetch_feed', return_value=self.feed):
                        resumed.check_async()
                        self.assertTrue(self.wait(resumed)['can_update'])
                self.unchanged()

    def test_validation_error_text_does_not_bypass_persistent_hold(self):
        status = self.induce_failure(s.UpdateError('发布信息已变化，请重新检查更新。'))
        self.assertTrue(status['retry_blocked'])
        self.assertTrue(self.service._hold_path.exists())
        self.unchanged()

    def test_library_change_before_execution_requires_fresh_check(self):
        from pocket_memory import embedded_update
        stage = self.root / 'staging'; stage.mkdir()
        with patch.object(u, 'fetch_feed', return_value=self.feed):
            self.service.check_async(); self.assertTrue(self.wait()['can_update'])
        def changed_library(*args, **kwargs):
            self.service.data_dir = self.root / 'other-library'
        with patch.object(u, 'fetch_feed', return_value=self.feed), \
                patch.object(u.tempfile, 'mkdtemp', return_value=str(stage)), \
                patch.object(u, 'download_package', side_effect=changed_library), \
                patch.object(u, 'extract_update_archive'), \
                patch.object(u.SafeUpdater, 'check', return_value={'previous_transaction': 'none'}), \
                patch.object(embedded_update, 'stage_executor', return_value=([str(self.helper)], self.helper)), \
                patch.object(u.subprocess, 'Popen') as popen:
            self.service.start_update(); status = self.wait()
        self.assertEqual(status['state'], 'error')
        self.assertIn('重新检查', status['message'])
        self.assertFalse(status.get('retry_blocked', False))
        self.assertIsNone(self.service._offer)
        self.assertFalse(self.service._hold_path.exists())
        self.assertFalse((stage / 'request.json').exists())
        popen.assert_not_called(); self.unchanged()

    def test_real_payload_inventory_missing_and_extra_still_rejected(self):
        from test_app_updates import AppUpdateTests
        fixture=AppUpdateTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        archive=fixture.zip_fixture()
        source=self.root/'payload-test'
        u.extract_update_archive(archive,source,'1.2.1')
        missing=source/'payload/_internal/runtime.bin';missing.unlink()
        (source/'payload/unlisted.txt').write_text('do not trust')
        with self.assertRaises(s.UpdateValidationError) as cm:
            s.SafeUpdater(self.install).check(source,read_only=True)
        self.assertEqual(cm.exception.failure_code,'inventory_mismatch');self.unchanged()

    def test_verify_separates_missing_unreadable_hash_and_success(self):
        path=self.root/'verify.bin';path.write_bytes(b'abcd')
        entry={'size':4,'sha256':hashlib.sha256(b'abcd').hexdigest()}
        s._verify(path,entry)
        with patch.object(s,'_hash',side_effect=PermissionError(13,'private')):
            with self.assertRaises(s.UpdateError) as cm: s._verify(path,entry)
        self.assertEqual(f.from_exception(cm.exception,'verify')['code'],'unreadable')
        path.write_bytes(b'xxxx')
        with self.assertRaises(s.UpdateError) as cm: s._verify(path,entry)
        self.assertEqual(f.from_exception(cm.exception,'verify')['code'],'integrity_mismatch')
        path.unlink()
        with self.assertRaises(s.UpdateError) as cm: s._verify(path,entry)
        self.assertEqual(f.from_exception(cm.exception,'verify')['code'],'missing')

    def test_handshake_failure_does_not_claim_install_success(self):
        self.service._phase='handshake'
        self.service._begin(lambda: (_ for _ in ()).throw(s.UpdateError('no ready')))
        result=self.wait()
        self.assertEqual(result['update_failure']['code'],'handshake_failed')
        self.assertNotIn('shutdown_requested',result);self.unchanged()

    def test_previous_worker_failure_pauses_on_start_even_after_rollback(self):
        receipt=self.install/'.pocket-memory-update/auto-update-result.json'
        receipt.parent.mkdir()
        for state in ('rolled_back','not_updated','error','recovery_required'):
            with self.subTest(state=state):
                receipt.write_text(json.dumps({'state':state,'message':'private path','target_version':'1.0.17'}))
                svc=self.create_service()
                result=svc.status()
                self.assertTrue(result['retry_blocked']);self.assertNotIn('last_result',result)
                self.assertEqual(result['update_failure']['code'],'previous_attempt_failed')
                with patch.object(u,'fetch_feed') as fetch: svc.check_async()
                fetch.assert_not_called()
        self.unchanged()

    def test_wrapped_receipt_read_error_keeps_errno(self):
        try:
            try: raise PermissionError(13,'private')
            except PermissionError as exc: raise s.UpdateError('receipt') from exc
        except s.UpdateError as exc: result=f.from_exception(exc,'handshake')
        self.assertEqual(result,{'code':'unreadable','stage':'handshake','errno':13})

    def test_network_failure_during_download_has_no_persistent_pause_or_launch(self):
        import urllib.error
        with patch.object(u,'fetch_feed',return_value=self.feed):
            self.service.check_async();self.wait()
        stage=self.root/'download';stage.mkdir()
        with patch.object(u,'fetch_feed',return_value=self.feed),patch.object(u.tempfile,'mkdtemp',return_value=str(stage)),\
             patch.object(u,'download_package',side_effect=urllib.error.URLError('offline')),patch.object(u.subprocess,'Popen') as popen:
            self.service.start_update();result=self.wait()
        self.assertEqual(result['state'],'unavailable')
        self.assertFalse(result.get('retry_blocked',False));popen.assert_not_called();self.unchanged()


if __name__=='__main__': unittest.main()
