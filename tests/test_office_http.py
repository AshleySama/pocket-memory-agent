# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pocket_memory.config import AppConfig
from pocket_memory.embedding import EmbeddingService
from pocket_memory.intelligence import IntelligenceService
from pocket_memory.ocr import OcrService
from pocket_memory.server import PocketMemoryServer
from pocket_memory.storage import NoteStore


class OfflineEngine:
    available = False
    unavailable_reason = 'test offline'


class TextEngine(OfflineEngine):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def _chat(self, *args, **kwargs):
        self.entered.set()
        self.release.wait(5)
        return json.dumps({'items': [{'task': '交初稿', 'owner': '小王', 'due_text': '周五', 'evidence': '小王周五交初稿。'}]}, ensure_ascii=False)


class OfficeHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = NoteStore(root / 'data')
        self.engine = TextEngine()
        config = AppConfig(root / 'config.json', self.store.data_dir, ai_auto_apply=False, ai_warmup_on_start=False)
        self.server = PocketMemoryServer(('127.0.0.1', 0), self.store, config, lambda p: None,
            OcrService(self.store, OfflineEngine()), EmbeddingService(self.store, OfflineEngine()),
            IntelligenceService(self.store, self.engine, auto_apply=False))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        _, state = self.req('/api/office-tools')
        self.library = state['library_id']

    def tearDown(self):
        self.engine.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def req(self, path, body=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def post(self, path, **body):
        return self.req('/api/office-tools/' + path, {'library_id': self.library, **body})

    def wait_job(self, job):
        for _ in range(100):
            code, result = self.req('/api/office-tools/jobs/' + job['job_id'] + '?library_id=' + self.library)
            self.assertEqual(code, 200)
            if result['status'] in ('completed', 'failed'):
                return result
            time.sleep(.02)
        self.fail('job did not finish')

    def test_generation_does_not_save_and_source_is_canonical(self):
        note = self.store.create_note('原笔记', '小王周五交初稿。', ai_status='not_requested')
        code, job = self.post('jobs', kind='todos', text=note.content, source={'note_id': note.id, 'title': '伪标题'})
        self.assertEqual(code, 202)
        done = self.wait_job(job)
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['source']['title'], '原笔记')
        _, state = self.req('/api/office-tools')
        self.assertEqual(state['todos'], [])
        self.assertEqual(self.store.get_note(note.id).content, note.content)

    def test_stale_library_and_cross_origin_cannot_write(self):
        code, _ = self.post('save-note', library_id='old', title='不能保存', content='abc', request_id='stale')
        self.assertEqual(code, 409)
        code, _ = self.req('/api/office-tools/jobs', {'library_id': self.library, 'kind': 'todos', 'text': 'abc'}, origin='https://example.com')
        self.assertEqual(code, 400)
        self.assertEqual(self.store.list_notes(), [])

    def test_todo_save_retry_edit_and_remind(self):
        items = [{'task': '交初稿', 'owner': '', 'due_text': '', 'evidence': '交初稿'}]
        code, first = self.post('todos', items=items, source={}, request_id='todo-test')
        self.assertEqual(code, 201, first)
        _, again = self.post('todos', items=items, source={}, request_id='todo-test')
        self.assertEqual(first, again)
        todo = first['todos'][0]
        code, _ = self.post(f'todos/{todo["id"]}', completed=True)
        self.assertEqual(code, 200)
        when = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        self.assertEqual(self.post(f'todos/{todo["id"]}/remind', datetime_str=when)[0], 400)
        self.post(f'todos/{todo["id"]}', completed=False, task='审核初稿')
        _, a = self.post(f'todos/{todo["id"]}/remind', datetime_str=when)
        _, b = self.post(f'todos/{todo["id"]}/remind', datetime_str=when)
        self.assertEqual(a, b)
        self.assertTrue(a['result']['ok'])
        self.assertEqual(a['result']['message'], '审核初稿')
        self.assertEqual(self.post(f'todos/{todo["id"]}/remind', datetime_str='2020-01-01 01:00')[0], 400)

    def test_note_save_is_separate_and_retry_safe(self):
        body = dict(title='工具成果', content='保留的结果', request_id='note-test')
        code, a = self.post('save-note', **body)
        self.assertEqual(code, 201)
        _, b = self.post('save-note', **body)
        self.assertEqual(a['note']['id'], b['note']['id'])
        self.assertEqual(len(self.store.list_notes()), 1)
        self.assertEqual(self.post('save-note', **{**body, 'content': '修改后'})[0], 400)

    def test_generation_does_not_block_saving_and_switch_is_guarded(self):
        self.engine.release.clear()
        code, job = self.post('jobs', kind='todos', text='小王周五交初稿。')
        self.assertEqual(code, 202)
        self.assertTrue(self.engine.entered.wait(2))
        code, _ = self.req('/api/notes', {'title': '可保存', 'content': '生成期间保存'})
        self.assertEqual(code, 201)
        with self.assertRaises(ValueError):
            self.server._prepare_library_change()
        self.engine.release.set()
        self.assertEqual(self.wait_job(job)['status'], 'completed')

    def test_bad_input_not_queued(self):
        for body in ({'kind': 'todos', 'text': ''}, {'kind': 'todos', 'text': '字' * 1001},
                     {'kind': 'table', 'text': '测试', 'columns': []}, {'kind': 'shell', 'text': 'abc'}):
            self.assertEqual(self.post('jobs', **body)[0], 400)

    def test_todos_follow_library_and_are_in_backup(self):
        items = [{'task': '交初稿', 'owner': '', 'due_text': '', 'evidence': '交初稿'}]
        self.post('todos', items=items, source={}, request_id='library-test')
        original = self.store.data_dir
        self.server.switch_data_dir(Path(self.tmp.name) / 'other-library')
        _, current = self.req('/api/office-tools')
        self.assertNotEqual(current['library_id'], self.library)
        self.assertEqual(current['todos'], [])
        self.assertEqual(self.post('todos', items=items, source={}, request_id='late-write')[0], 409)
        self.server.switch_data_dir(original)
        _, restored = self.req('/api/office-tools')
        self.assertEqual(len(restored['todos']), 1)
        backup = Path(self.tmp.name) / 'backup.zip'
        self.store.create_backup(backup)
        with zipfile.ZipFile(backup) as archive:
            self.assertIn('office-tools.db', archive.namelist())


if __name__ == '__main__':
    unittest.main()
