# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from pocket_memory.storage import NoteStore, NoteContentConflict
from pocket_memory.server import PocketMemoryServer
from pocket_memory.config import AppConfig
from pocket_memory.ocr import OcrService
from pocket_memory.embedding import EmbeddingService
from pocket_memory.intelligence import IntelligenceService


class Offline:
    available = False
    unavailable_reason = 'offline test'


class NoteAutosaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.tmp.name)/'data')
        self.note = self.store.create_note('已有笔记', '原正文', ai_status='not_requested')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stale_editor_cannot_overwrite_but_identical_retry_is_idempotent(self):
        first = self.store.update_note(self.note.id, content='新正文', expected_content='原正文', autosave=True)
        with self.assertRaises(NoteContentConflict):
            self.store.update_note(self.note.id, content='旧窗口编辑', expected_content='原正文', autosave=True)
        retry = self.store.update_note(self.note.id, content='新正文', expected_content='原正文', autosave=True)
        self.assertEqual(first, retry)
        self.assertEqual(len(self.store.list_versions(self.note.id)), 1)
        self.assertIn('新正文', (self.store.data_dir/retry.markdown_path).read_text(encoding='utf8'))

    def test_checkpoint_preserves_edit_start_and_current_content_survives_reopen(self):
        content = self.note.content
        for i in range(20):
            with patch('pocket_memory.storage.time.monotonic', return_value=100+i):
                self.store.update_note(self.note.id, content=f'编辑{i}', expected_content=content, autosave=True)
            content = f'编辑{i}'
        versions = self.store.list_versions(self.note.id)
        self.assertEqual([v['content'] for v in versions], ['原正文'])
        with patch('pocket_memory.storage.time.monotonic', return_value=161):
            self.store.update_note(self.note.id, content='最后正文', expected_content=content, autosave=True)
        self.assertEqual([v['content'] for v in self.store.list_versions(self.note.id)], ['编辑19', '原正文'])
        data = self.store.data_dir
        self.store.close()
        self.store = NoteStore(data)
        self.assertEqual(self.store.get_note(self.note.id).content, '最后正文')

    def test_explicit_edit_resets_checkpoint_and_metadata_is_not_accepted_as_autosave(self):
        self.store.update_note(self.note.id, content='A', expected_content='原正文', autosave=True)
        self.store.update_note(self.note.id, content='B')
        self.store.update_note(self.note.id, content='C', expected_content='B', autosave=True)
        self.assertEqual([v['content'] for v in self.store.list_versions(self.note.id)], ['B','A','原正文'])
        with self.assertRaises(ValueError):
            self.store.update_note(self.note.id, content='D', category='新分类', autosave=True)

    def test_existing_note_recovers_missing_markdown_month_directory(self):
        path = self.store.data_dir/self.note.markdown_path
        path.unlink()
        path.parent.rmdir()
        updated = self.store.update_note(self.note.id, content='恢复后保存的完整正文',
                                         expected_content='原正文', autosave=True)
        self.assertEqual(updated.content, '恢复后保存的完整正文')
        self.assertIn(updated.content, path.read_text(encoding='utf-8'))
        self.assertEqual([v['content'] for v in self.store.list_versions(self.note.id)], ['原正文'])

    def test_failed_file_replace_keeps_original_and_does_not_add_or_prune_history(self):
        for i in range(50):
            self.store.update_note(self.note.id, content=f'历史{i}')
        before = self.store.get_note(self.note.id)
        versions = self.store.list_versions(self.note.id)
        path = self.store.data_dir/before.markdown_path
        original_bytes = path.read_bytes()
        with patch.object(Path, 'replace', side_effect=PermissionError('disk denied')):
            for _ in range(3):
                with self.assertRaises(PermissionError):
                    self.store.update_note(self.note.id, content='未保存草稿',
                                           expected_content=before.content, autosave=True)
        self.assertEqual(self.store.get_note(self.note.id), before)
        self.assertEqual(self.store.list_versions(self.note.id), versions)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(list(path.parent.glob('.*.tmp')), [])
        self.store.update_note(self.note.id, content='重试成功', expected_content=before.content, autosave=True)
        self.assertEqual(self.store.list_versions(self.note.id)[0]['content'], before.content)

    def test_http_autosave_conflict_noop_and_deferred_organisation(self):
        config = AppConfig(Path(self.tmp.name)/'config.json', self.store.data_dir,
                           ai_warmup_on_start=False, ai_auto_apply=True)
        intelligence = IntelligenceService(self.store, Offline(), auto_apply=False)
        embedding = EmbeddingService(self.store, Offline())
        server = PocketMemoryServer(('127.0.0.1',0), self.store, config, lambda _:None,
            OcrService(self.store,Offline()), embedding, intelligence)
        ai_calls, index_calls = [], []
        intelligence.enqueue = lambda nid, **kw: ai_calls.append((nid,kw))
        embedding.enqueue = lambda nid: index_calls.append(nid)
        worker = threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        url=f'http://127.0.0.1:{server.server_port}'
        def put(body):
            request=Request(url+f'/api/notes/{self.note.id}',data=json.dumps(body).encode(),method='PUT',
                            headers={'Content-Type':'application/json','Origin':url})
            try:
                with urlopen(request,timeout=5) as result:return result.status,json.load(result)
            except HTTPError as error:return error.code,json.load(error)
        try:
            body=dict(content='HTTP保存',expected_content='原正文',autosave=True)
            status, result = put(body)
            self.assertEqual(status,200);self.assertEqual(result['content'],'HTTP保存')
            self.assertEqual(ai_calls,[(self.note.id,{'delay':15})])
            self.assertEqual(put(body)[0],200)
            self.assertEqual(len(ai_calls),1);self.assertEqual(len(index_calls),1)
            status, result=put(dict(body,content='过期窗口'))
            self.assertEqual(status,409);self.assertEqual(result['code'],'content_conflict')
            self.assertEqual(self.store.get_note(self.note.id).content,'HTTP保存')
            self.assertEqual(put(dict(content='bad',autosave=True))[0],400)
            self.assertEqual(len(ai_calls),1)
        finally:
            server.shutdown();server.server_close();worker.join(timeout=5)

    def test_failed_directory_change_resumes_pending_organisation(self):
        for operation in ('switch_data_dir', 'migrate_data_dir'):
            fake = Mock()
            fake.store.data_dir = self.store.data_dir
            getattr(fake.store, operation).side_effect = ValueError('target rejected')
            fake._recover_ai_after_cancelled_library_change = lambda: PocketMemoryServer._recover_ai_after_cancelled_library_change(fake)
            with self.assertRaisesRegex(ValueError, 'target rejected'):
                getattr(PocketMemoryServer, operation)(fake, Path(self.tmp.name)/'rejected')
            fake.intelligence_service.recover.assert_called_once()

    def test_timed_out_library_drain_resumes_pending_organisation(self):
        fake = Mock()
        fake.report_workspace = None
        fake.office_tools.busy = False
        fake.document_service.executor.submit.return_value.result.side_effect = TimeoutError()
        fake._recover_ai_after_cancelled_library_change = lambda: PocketMemoryServer._recover_ai_after_cancelled_library_change(fake)
        with self.assertRaisesRegex(ValueError, '后台任务'):
            PocketMemoryServer._prepare_library_change(fake)
        fake.intelligence_service.cancel_pending_autosaves.assert_called_once()
        fake.intelligence_service.recover.assert_called_once()
