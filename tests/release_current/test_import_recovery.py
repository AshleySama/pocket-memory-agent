# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import base64, concurrent.futures, importlib.util, json, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / '.'
sys.path.insert(0, str(CANDIDATE))
from pocket_memory.storage import NoteStore
from pocket_memory.documents import DocumentImportService
from pocket_memory.embedding import EmbeddingService
from pocket_memory import index_state

def data(raw): return 'data:application/octet-stream;base64,' + base64.b64encode(raw).decode()

class NoEmbedding:
    available = False
    unavailable_reason = 'test: keyword only'

class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name))
        self.embedding = EmbeddingService(self.store, NoEmbedding())
        self.service = DocumentImportService(self.store, self.embedding)
    def tearDown(self):
        self.service.executor.shutdown(wait=True)
        self.embedding.shutdown()
        self.store.close(); self.temp.cleanup()
    def make(self, raw=b'Original document for import recovery.', name='original.txt'):
        return self.store.import_document_from_data_url(name, data(raw))
    def wait(self): self.service.executor.submit(lambda:None).result(10)

    def test_duplicate_concurrent_and_different_content(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda n:self.make(name=f'copy{n}.txt'), range(8)))
        self.assertEqual(len({x[0].id for x in results}), 1)
        self.assertEqual(sum(not x[1] for x in results), 1)
        changed, reused = self.make(b'changed', 'copy0.txt')
        self.assertFalse(reused); self.assertEqual(len(self.store.list_documents()), 2)
        self.assertEqual(len(list((self.store.data_dir/'attachments/documents').glob('*'))), 2)

    def test_lost_reply_after_commit_preserves_original_and_retry_id(self):
        with patch.object(self.store, 'get_document', side_effect=RuntimeError('lost post-commit reply')):
            with self.assertRaises(RuntimeError): self.make()
        first = self.store.list_documents()[0]
        self.assertTrue((self.store.data_dir/first.stored_path).is_file())
        retry, reused = self.make()
        self.assertTrue(reused); self.assertEqual(first.id, retry.id)

    def test_locked_previous_original_does_not_delete_replacement(self):
        old,_ = self.make(); old_path = self.store.data_dir/old.stored_path
        original_unlink = Path.unlink
        def unlink(path, *a, **kw):
            if path == old_path: raise PermissionError('Windows file is open')
            return original_unlink(path, *a, **kw)
        with patch.object(Path, 'unlink', unlink):
            new = self.store.replace_document_from_data_url(old.id,'revised.txt',data(b'Revised original'))
        self.assertEqual((self.store.data_dir/new.stored_path).read_bytes(),b'Revised original')
        self.assertTrue(old_path.exists())

    def test_failed_database_insert_does_not_publish_orphan(self):
        self.store.connection.execute("CREATE TRIGGER reject_test BEFORE INSERT ON documents BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception): self.make()
        self.assertEqual(self.store.list_documents(), [])
        self.assertEqual(list((self.store.data_dir/'attachments/documents').glob('*')), [])

    def test_parse_failure_preserves_original_and_survives_restart_index(self):
        doc,_ = self.make(b'broken Word zip','broken.docx')
        self.service.enqueue(doc.id); self.wait()
        self.assertEqual(self.store.get_document(doc.id).status,'failed')
        self.assertEqual((self.store.data_dir/doc.stored_path).read_bytes(),b'broken Word zip')
        self.service.recover(); self.embedding.enqueue_document(doc.id)
        self.assertEqual(self.store.get_document(doc.id).status,'failed')
        with self.assertRaises(ValueError): index_state.publish_document(self.store,doc.id,[],[],{},'x',lambda:True)

    def test_retry_recovers_transient_parse_error_without_reupload(self):
        doc,_ = self.make()
        with patch('pocket_memory.documents.DocumentExtractor.extract', side_effect=PermissionError('busy file')):
            self.service.enqueue(doc.id); self.wait()
        self.assertEqual(self.store.get_document(doc.id).status,'failed')
        self.service.reindex(doc.id); self.wait()
        self.assertEqual(self.store.get_document(doc.id).status,'ready')
        self.assertEqual(len(self.store.list_documents()),1)
        self.assertTrue(self.store.list_document_chunks(doc.id))

    def test_queue_submit_failure_is_saved_failed_record(self):
        doc,_ = self.make()
        with patch.object(self.service.executor,'submit',side_effect=RuntimeError('worker closed')):
            self.assertFalse(self.service.enqueue(doc.id))
        failed=self.store.get_document(doc.id)
        self.assertEqual(failed.status,'failed'); self.assertIn('原文件已保存',failed.error)
        self.assertTrue((self.store.data_dir/failed.stored_path).exists())

    def test_same_version_coalesced_replacement_not_swallowed(self):
        doc,_ = self.make(); entered=threading.Event(); release=threading.Event()
        def delayed(*args,**kwargs):
            entered.set(); release.wait(5); raise ValueError('old version failed')
        with patch('pocket_memory.documents.DocumentExtractor.extract', side_effect=delayed):
            self.assertTrue(self.service.enqueue(doc.id)); self.assertTrue(entered.wait(3))
            self.assertFalse(self.service.enqueue(doc.id))
            new=self.store.replace_document_from_data_url(doc.id,'new.txt',data(b'New file version'))
            release.set(); self.wait()
        self.assertEqual(self.store.get_document(doc.id).status,'pending')
        self.service.enqueue(doc.id); self.wait()
        self.assertEqual(self.store.get_document(doc.id).status,'ready')
        self.assertEqual(self.store.get_document(doc.id).sha256,new.sha256)

    def test_restart_incomplete_marked_retryable_and_nonempty_index_resumes(self):
        a,_=self.make(b'a'); b,_=self.make(b'b')
        self.store.set_document_status(a.id,'extracting')
        self.store.replace_document_chunks(b.id,[{'text':'saved parsed content','location':'正文','title':'b'}])
        self.service.recover(); self.embedding.rebuild()
        self.assertEqual(self.store.get_document(a.id).status,'failed')
        self.assertEqual(self.store.get_document(b.id).status,'ready')

    def test_missing_original_restored_by_identical_upload(self):
        doc,_=self.make(); (self.store.data_dir/doc.stored_path).unlink()
        same,reused=self.make()
        self.assertTrue(reused); self.assertEqual(same.id,doc.id)
        self.assertTrue((self.store.data_dir/same.stored_path).exists())

if __name__ == '__main__': unittest.main(verbosity=2)
