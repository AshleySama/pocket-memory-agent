# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import concurrent.futures, importlib.util, json, tempfile, unittest, uuid
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('drafts',ROOT/'pocket_memory/drafts.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=mod.DraftStore(Path(self.tmp.name)/'a')
    def record(self,**kw):
        return {'library_id':self.store.library_id,'draft_id':str(uuid.uuid4()),'revision':1,'kind':'new','title':'标题','content':'中文正文','base_content':'',**kw}
    def test_reopen_and_unicode(self):
        r=self.record();self.store.save(r);other=mod.DraftStore(Path(self.tmp.name)/'a');self.assertEqual(other.list()['drafts'][0]['content'],'中文正文')
    def test_library_isolation_and_old_write(self):
        r=self.record();self.store.save(r);other=mod.DraftStore(Path(self.tmp.name)/'b')
        with self.assertRaises(mod.DraftConflict):other.save(r)
        self.assertEqual(other.list()['drafts'],[])
    def test_same_note_different_tabs_and_windows(self):
        a=self.record(kind='note',note_id=4,parent_note_id=1);b=self.record(kind='note',note_id=5,parent_note_id=1);c=self.record(kind='note',note_id=4,parent_note_id=1)
        for r in[a,b,c]:self.store.save(r)
        self.assertEqual(len(self.store.list()['drafts']),3)
    def test_duplicate_same_revision_is_idempotent(self):
        r=self.record();a=self.store.save(r);self.assertEqual(a,self.store.save(r))
    def test_older_and_conflicting_revision_rejected(self):
        r=self.record(revision=2);self.store.save(r)
        for changed in[dict(r,revision=1),dict(r,content='别的文字')]:
            with self.assertRaises(mod.DraftConflict):self.store.save(changed)
        self.assertEqual(self.store.list()['drafts'][0]['content'],'中文正文')
    def test_closed_session_never_resurrects(self):
        r=self.record();self.store.close(r['draft_id'],r['library_id']);new=mod.DraftStore(Path(self.tmp.name)/'a')
        with self.assertRaises(mod.DraftConflict):new.save(dict(r,revision=100))
        self.assertEqual(new.list()['drafts'],[])
    def test_atomic_failure_keeps_previous_bytes(self):
        r=self.record();self.store.save(r)
        with patch.object(mod.os,'replace',side_effect=OSError('disk full')):
            with self.assertRaises(mod.DraftStorageError):self.store.save(dict(r,revision=2,content='不能提交'))
        self.assertEqual(self.store.list()['drafts'][0]['content'],'中文正文')
    def test_corruption_not_overwritten(self):
        r=self.record();self.store.save(r);file=self.store.directory/(r['draft_id']+'.json');file.write_text('{broken','utf-8')
        with self.assertRaises(mod.DraftStorageError):self.store.save(dict(r,revision=2))
        self.assertEqual(file.read_text('utf-8'),'{broken')
    def test_path_and_field_validation(self):
        for overrides in[{'draft_id':'../outside'},{'revision':True},{'kind':'note','note_id':None},{'image_data_url':'https://external/image'},{'edit_field':'ocr_text'}]:
            with self.assertRaises(ValueError):self.store.save(self.record(**overrides))
    def test_parallel_versions_keep_highest(self):
        r=self.record();
        def save(v):
            try:self.store.save(dict(r,revision=v,content=str(v)))
            except mod.DraftConflict:pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(save,range(1,21)))
        self.assertEqual(self.store.list()['drafts'][0]['revision'],20)
    def test_rename_and_image_are_not_note_writes(self):
        self.store.save(self.record(kind='note',note_id=4,edit_field='title',content='改过的标题'))
        self.store.save(self.record(image_data_url='data:image/png;base64,YWJj',image_name='截图.png'))
        self.assertEqual(len(self.store.list()['drafts']),2)
        self.assertFalse((Path(self.tmp.name)/'a'/'notes.db').exists())

if __name__=='__main__':
    unittest.main(verbosity=2)
