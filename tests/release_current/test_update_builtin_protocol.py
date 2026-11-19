# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Explicit worker-free protocol: real file transactions, no build/inference.

Process/GUI health boundaries are simulated; this is not packaged EXE acceptance.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from zipfile import ZipFile

from pocket_memory import safe_update as safe, embedded_update as embedded, app_updates
from scripts import update_worker as worker
from scripts import prepare_offline_delivery as delivery, check_update_component as delivery_check, create_zip64
import test_embedded_update as fixtures
from test_embedded_update import WORKER, MODEL
from test_safe_update import entry, put


class BuiltinProtocolTests(unittest.TestCase):
    setUp = fixtures.EmbeddedExecutorTests.setUp
    runtime = fixtures.EmbeddedExecutorTests.runtime
    write_receipt = fixtures.EmbeddedExecutorTests.write_receipt
    change_manifest_file = fixtures.EmbeddedExecutorTests.change_manifest_file

    def builtin(self, name, *, version='1.0.15', root=None, receipt=True):
        root, manifest = self.runtime(name, worker=False, version=version, root=root, receipt=False)
        manifest['schema_version'] = 2
        manifest['update_executor'] = 'main-exe-v1'
        manifest['files'] = [e for e in manifest['files'] if e['path'] != WORKER]
        capability=json.dumps(dict(fixtures.CAPABILITY,schema_version=2,manifest_schemas=[1,2])).encode()
        put(root/embedded.CAPABILITY_PATH,capability)
        manifest['files']=[entry(e['path'],capability) if e['path']==embedded.CAPABILITY_PATH else e for e in manifest['files']]
        if receipt:
            self.write_receipt(root, manifest)
        return root, manifest

    def fixture(self, name='case', legacy=False):
        if legacy:
            install, old = self.runtime(name+'-install', worker=False, version='1.0.13')
        else:
            install, old = self.builtin(name+'-install', version='1.0.14')
        put(install/'models/bge/model.bin', MODEL)
        data = self.base/(name+'-data'); put(data/'notes.txt', b'user note - unchanged')
        config = self.base/(name+'-config.json'); put(config, b'{"theme":"original"}')
        work = self.base/(name+'-work'); source = work/'source'
        _, manifest = self.builtin(name+'-new', root=source/'payload', receipt=False)
        put(source/safe.MANIFEST_NAME, json.dumps(manifest).encode())
        req = dict(schema_version=1, install_dir=str(install), data_dir=str(data), config_path=str(config),
                   source_dir=str(source), target_version=manifest['version'], parent_pid=999999, launcher_pid=999998,
                   port=51322, health_token='a'*64, local_app_data=str(self.base/'profile'), debug=False)
        path=work/'request.json'; put(path,json.dumps(req).encode())
        put(work/'go.json',json.dumps({'token':req['health_token']}).encode())
        return install, source, path, req, old, manifest

    def program(self, install, manifest):
        return {e['path']:(install/e['path']).read_bytes() for e in manifest['files'] if (install/e['path']).exists()}

    def assert_preserved(self, install, req):
        self.assertEqual((Path(req['data_dir'])/'notes.txt').read_bytes(), b'user note - unchanged')
        self.assertEqual(Path(req['config_path']).read_bytes(), b'{"theme":"original"}')
        self.assertEqual((install/'models/bge/model.bin').read_bytes(), MODEL)

    def test_builtin_normal_apply_and_commit(self):
        install,source,_,req,_,manifest=self.fixture()
        updater=safe.SafeUpdater(install,protected_data_dirs=[req['data_dir']],config_paths=[req['config_path']])
        self.assertEqual(updater.check(source,read_only=True)['state'],'checked')
        self.assertEqual(updater.apply(source)['state'],'pending_health')
        self.assertEqual(updater.commit('source test health verified')['state'],'committed')
        self.assertEqual(embedded.verify_runtime(install),manifest)
        self.assertEqual(embedded.inspect_executor(install)['legacy_component_status'],'not_required')
        self.assertFalse((install/WORKER).exists())
        self.assert_preserved(install,req)

    def test_legacy_source_missing_worker_still_blocks_with_precise_error(self):
        install,source,_,_,_,_=self.fixture()
        _,legacy=self.runtime('legacy-payload',root=source/'payload',worker=False,receipt=False)
        put(source/safe.MANIFEST_NAME,json.dumps(legacy).encode())
        before=(install/'PocketMemory.exe').read_bytes()
        with self.assertRaisesRegex(safe.UpdateError,r'缺少：_internal/PocketMemoryUpdateWorker.exe'):
            safe.SafeUpdater(install).check(source,read_only=True)
        self.assertEqual((install/'PocketMemory.exe').read_bytes(),before)
        self.assertFalse((install/safe.STATE_DIR/'journal.json').exists())

    def test_missing_corrupt_extra_and_capability_mismatch_block(self):
        variants=['missing_main','missing_library','missing_marker','wrong_hash','extra','extra_worker','wrong_dll']
        for variant in variants:
            with self.subTest(variant=variant):
                install,source,_,req,old,manifest=self.fixture(variant)
                before=self.program(install,old); root=source/'payload'
                if variant.startswith('missing_'):
                    (root/{'missing_main':'PocketMemory.exe','missing_library':'_internal/base_library.zip','missing_marker':embedded.CAPABILITY_PATH}[variant]).unlink()
                elif variant=='wrong_hash':(root/'_internal/python313.dll').write_bytes(b'corrupt')
                elif variant=='wrong_dll':
                    value=json.dumps({'schema_version':1,'executor':'main-exe-v1','python_dll':'python314.dll'}).encode()
                    put(root/embedded.CAPABILITY_PATH,value)
                    manifest['files']=[entry(e['path'],value) if e['path']==embedded.CAPABILITY_PATH else e for e in manifest['files']]
                    put(source/safe.MANIFEST_NAME,json.dumps(manifest).encode())
                else:put(root/(WORKER if variant=='extra_worker' else '_internal/extra.pyd'),b'undeclared')
                with self.assertRaises(safe.UpdateError):safe.SafeUpdater(install).apply(source)
                self.assertEqual(self.program(install,old),before);self.assert_preserved(install,req)

    def test_schema_does_not_infer_exemptions_from_missing_files(self):
        _,manifest=self.builtin('schema')
        invalid=[dict(manifest, schema_version=1),dict(manifest,update_executor='downloaded-hook'),
                 dict(manifest,schema_version=True),dict(manifest,extra='hook')]
        legacy=copy.deepcopy(manifest);legacy.pop('update_executor');legacy['schema_version']=1;invalid.append(legacy)
        mixed=copy.deepcopy(manifest);mixed['files'].append(entry(WORKER,b'worker'));invalid.append(mixed)
        for m in invalid:
            with self.subTest(manifest=m), self.assertRaises(safe.UpdateError):safe.validate_manifest(m)

    def test_new_client_uses_separate_compatible_feed(self):
        self.assertEqual(app_updates.FEED_URL,f'https://{app_updates.TRUSTED_HOST}/PocketMemory-update-v2.json')

    def test_old_binary_capability_cannot_be_relabeled_as_v2(self):
        root,manifest=self.builtin('old-capability')
        self.change_manifest_file(root,manifest,embedded.CAPABILITY_PATH,json.dumps(fixtures.CAPABILITY).encode())
        with self.assertRaisesRegex(safe.UpdateError,'尚未声明支持新版'):
            embedded.verify_runtime(root,manifest)

    def test_inventory_read_error_is_not_reported_as_missing_worker(self):
        root,_=self.builtin('permission')
        real=os.scandir
        def denied(path):
            if Path(path)==root/'_internal':raise PermissionError(13,'fixture access denied',str(path))
            return real(path)
        with mock.patch.object(os,'scandir',side_effect=denied):
            with self.assertRaisesRegex(safe.UpdateError,'无法读取更新文件目录'):safe._inventory(root)

    def test_staged_executor_copies_only_verified_installed_runtime(self):
        root,manifest=self.builtin('executor')
        work=self.base/'stage';work.mkdir()
        command,exe=embedded.stage_executor(root,work)
        self.assertEqual(command,[str(exe),embedded.EMBEDDED_FLAG])
        self.assertEqual(embedded.verify_runtime(exe.parent),manifest)
        self.assertFalse((exe.parent/WORKER).exists())
        self.assertFalse((exe.parent/'models').exists())

    def test_rollback_restores_legacy_receipt_and_original_missing_worker(self):
        install,source,_,req,old,_=self.fixture(legacy=True)
        before=self.program(install,old); receipt=(install/safe.STATE_DIR/'installed.json').read_bytes()
        updater=safe.SafeUpdater(install);updater.apply(source)
        self.assertEqual(updater.rollback()['state'],'rolled_back')
        self.assertEqual(self.program(install,old),before)
        self.assertEqual((install/safe.STATE_DIR/'installed.json').read_bytes(),receipt)
        self.assertFalse((install/WORKER).exists());self.assert_preserved(install,req)

    def test_transaction_failure_after_replace_automatically_restores_old_files(self):
        install,source,_,req,old,_=self.fixture()
        before=self.program(install,old);receipt=(install/safe.STATE_DIR/'installed.json').read_bytes()
        verify=safe._verify_components
        def fail_installed(root,manifest,**kw):
            if root==install:raise safe.UpdateError('injected post-replacement failure')
            return verify(root,manifest,**kw)
        with mock.patch.object(safe,'_verify_components',side_effect=fail_installed):
            with self.assertRaisesRegex(safe.UpdateError,'rolled back'):safe.SafeUpdater(install).apply(source)
        self.assertEqual(self.program(install,old),before)
        self.assertEqual((install/safe.STATE_DIR/'installed.json').read_bytes(),receipt)
        self.assert_preserved(install,req)

    def test_worker_health_failure_rolls_back_and_preserves_notes(self):
        install,_,path,req,old,_=self.fixture()
        before=self.program(install,old);receipt=(install/safe.STATE_DIR/'installed.json').read_bytes()
        with mock.patch.object(worker,'wait_for_old_exit'), \
             mock.patch.object(worker,'spawn_application',return_value=mock.Mock(pid=123)), \
             mock.patch.object(worker,'verify_health',side_effect=safe.UpdateError('injected startup failure')), \
             mock.patch.object(worker,'stop_owned_probe'), mock.patch.object(worker,'stop_owned_desktop'), \
             mock.patch.object(worker,'verify_restored_desktop') as restored:
            result=worker.run_worker(path)
        self.assertEqual(result['state'],'rolled_back');restored.assert_called_once()
        self.assertEqual(self.program(install,old),before)
        self.assertEqual((install/safe.STATE_DIR/'installed.json').read_bytes(),receipt)
        self.assert_preserved(install,req)

    def test_worker_normal_health_commits_with_verified_identity(self):
        install,_,path,req,_,manifest=self.fixture()
        with mock.patch.object(worker,'wait_for_old_exit'), \
             mock.patch.object(worker,'spawn_application',return_value=mock.Mock(pid=123)), \
             mock.patch.object(worker,'verify_health'),mock.patch.object(worker,'stop_owned_probe'), \
             mock.patch.object(worker,'verify_desktop',return_value={'pid':123,'server_pid':124}), \
             mock.patch.object(safe,'running_install_processes',side_effect=lambda _: [123,124] if (install/safe.STATE_DIR/'journal.json').exists() and safe._read_json(install/safe.STATE_DIR/'journal.json')['state']=='pending_health' else []):
            result=worker.run_worker(path)
        self.assertEqual(result['state'],'complete',result)
        self.assertEqual(embedded.verify_runtime(install),manifest);self.assert_preserved(install,req)

    def test_archive_and_app_preflight_use_exact_worker_free_payload(self):
        install,source,_,req,_,manifest=self.fixture()
        archive=self.base/'fixture.zip'
        with ZipFile(archive,'w') as z:
            for p in source.rglob('*'):
                if p.is_file():z.write(p,p.relative_to(source).as_posix())
        offered={'schema_version':1,'app_id':'pocket-memory','published':True,'version':manifest['version'],'notes':'fixture',
                 'package':{'url':f'https://{app_updates.TRUSTED_HOST}/PocketMemory-fixture.zip','size':archive.stat().st_size,
                            'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}
        config=SimpleNamespace(path=Path(req['config_path']),data_dir=Path(req['data_dir']))
        work=self.base/'app-handoff';work.mkdir()
        with mock.patch.object(app_updates.sys,'frozen',True,create=True), \
             mock.patch.object(app_updates.sys,'executable',str(install/'PocketMemory.exe')), \
             mock.patch.object(app_updates,'fetch_feed',return_value=offered), \
             mock.patch.object(app_updates.tempfile,'mkdtemp',return_value=str(work)), \
             mock.patch.object(app_updates,'_open_https',return_value=open(archive,'rb')), \
             mock.patch.object(app_updates.subprocess,'Popen',side_effect=RuntimeError('fixture stops at process launch')):
            service=app_updates.AppUpdateService(config,config.data_dir,server_port=51322,exit_callback=mock.Mock(),current_version='1.0.14')
            with self.assertRaisesRegex(RuntimeError,'fixture stops at process launch'):
                service._download_and_handoff(offered,(config.data_dir,config.path,config.data_dir))
            service.exit_callback.assert_not_called()
        self.assertEqual(embedded.verify_runtime(work/'installed-runtime')['schema_version'],2)
        self.assertFalse((work/'source/payload'/WORKER).exists());self.assert_preserved(install,req)

    def test_default_delivery_and_archive_require_explicit_builtin_receipt(self):
        build,_=self.builtin('built',receipt=False)
        put(build/'data/private.txt',b'not distributed')
        put(build/'config.json',b'not distributed')
        models=self.base/'test-models';put(models/'Qwen/model.gguf',b'FIXTURE ONLY - NOT A MODEL')
        for mode in ('full','update'):
            with self.subTest(mode=mode):
                dest=self.base/mode
                result=delivery.prepare(build,models,dest,version='1.0.15',mode=mode,schema_id='legacy-unversioned')
                self.assertFalse(any(dest.rglob('PocketMemoryUpdateWorker.exe')))
                checked=delivery_check.check_layout(dest)
                self.assertEqual(checked['status'],'ok')
                archive=self.base/(mode+'.zip')
                create_zip64.create_archive(dest,archive)
                self.assertEqual(delivery_check.check_archive(archive)['status'],'ok')
                self.assertFalse((dest/'data').exists());self.assertFalse((dest/'config.json').exists())
                if mode=='full':
                    self.assertEqual(embedded.verify_runtime(dest)['schema_version'],2)
                else:
                    self.assertFalse(list(dest.rglob('*.gguf')))
                    app_updates.extract_update_archive(archive,self.base/'extracted','1.0.15')
        # The default cannot silently ship a missing capability marker.
        (build/embedded.CAPABILITY_PATH).unlink()
        with self.assertRaises((safe.UpdateError,ValueError,OSError)):
            delivery.prepare(build,models,self.base/'bad',version='1.0.15',mode='full',schema_id='legacy-unversioned')

    def test_new_build_cannot_be_relabelled_for_legacy_clients(self):
        build,_=self.builtin('built',receipt=False)
        models=self.base/'test-models';put(models/'Qwen/model.gguf',b'FIXTURE ONLY')
        helper=self.base/'helper.exe';put(helper,fixtures.pe_bytes())
        with self.assertRaisesRegex(ValueError,'不能重贴为旧协议'):
            delivery.prepare(build,models,self.base/'bad',version='1.0.15',mode='full',
                             updater_exe=helper,executor='standalone',schema_id='legacy-unversioned')
        self.assertFalse((self.base/'bad').exists())

    def test_v2_archive_rejects_extra_missing_and_tampered_program_members(self):
        _,source,_,_,_,_=self.fixture()
        members={p.relative_to(source).as_posix():p.read_bytes() for p in source.rglob('*') if p.is_file()}
        for mode in ('extra','missing','tampered'):
            with self.subTest(mode=mode):
                bad=dict(members)
                if mode=='extra':bad['payload/_internal/unknown.dll']=b'undeclared'
                elif mode=='missing':bad.pop('payload/_internal/base_library.zip')
                else:bad['payload/_internal/base_library.zip']=b'changed'
                archive=self.base/(mode+'.zip')
                with ZipFile(archive,'w') as z:
                    for name,value in bad.items():z.writestr(name,value)
                with self.assertRaises((safe.UpdateError,ValueError)):
                    delivery_check.check_archive(archive)
                with self.assertRaises(safe.UpdateError):
                    app_updates.extract_update_archive(archive,self.base/(mode+'-extracted'),'1.0.15')


if __name__=='__main__':unittest.main()
