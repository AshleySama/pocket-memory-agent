# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Loopback HTTP integration without loading models or changing a user library."""
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

HERE = Path(__file__).resolve().parent
CANDIDATE = HERE.parents[1]
sys.path.insert(0, str(CANDIDATE))
from pocket_memory import app_updates, update_probe
from pocket_memory.server import PocketMemoryHandler

def http(port, path, *, method='GET', body=None, headers=None):
    request = Request(f'http://127.0.0.1:{port}{path}', data=body, method=method, headers=headers or {})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=3) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, e.read()

class UpdateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=HERE, prefix='update-http-')
        self.root = Path(self.tmp.name)
        self.config = SimpleNamespace(path=self.root/'config.json')
        self.config.path.write_text('{}', encoding='utf-8')
        self.s = ThreadingHTTPServer(('127.0.0.1', 0), PocketMemoryHandler)
        self.s.operation_lock = threading.RLock()
        self.s._closing = False
        self.s.store = SimpleNamespace(data_dir=self.root)
        self.s.config = self.config
        self.s.app_updates = app_updates.AppUpdateService(self.config, self.root,
            server_port=self.s.server_port, exit_callback=lambda: self.fail('preview cannot close app'))
        self.s.update_blocks_writes = lambda: False
        self.s.ask_cancellations = SimpleNamespace(finish=lambda _:None)
        self.t = threading.Thread(target=self.s.serve_forever, daemon=True)
        self.t.start()
    def tearDown(self):
        self.s.shutdown(); self.s.server_close(); self.t.join(3); self.tmp.cleanup()
    def test_source_preview_status_and_start_cannot_mutate(self):
        code, data = http(self.s.server_port, '/api/app-update')
        self.assertEqual(code, 200); self.assertTrue(data['preview'])
        code, _ = http(self.s.server_port, '/api/app-update/start', method='POST', body=b'{}',
                       headers={'X-PocketMemory-Update':'1','Content-Type':'application/json'})
        self.assertEqual(code, 409)
    def test_only_same_origin_empty_requests_are_accepted(self):
        for headers, body in [({}, b'{}'), ({'X-PocketMemory-Update':'1','Origin':'https://evil.test'}, b'{}'),
                              ({'X-PocketMemory-Update':'1'}, b'{"source":"C:/user"}')]:
            code, _ = http(self.s.server_port, '/api/app-update/start', method='POST', body=body, headers=headers)
            self.assertEqual(code, 400)
    def test_unpublished_is_not_latest(self):
        with patch.object(app_updates, 'fetch_feed', return_value={'schema_version':1,'app_id':'pocket-memory','published':False}):
            code, _ = http(self.s.server_port, '/api/app-update/check', method='POST', body=b'{}', headers={'X-PocketMemory-Update':'1'})
            self.assertEqual(code, 200)
            for _ in range(100):
                if self.s.app_updates.status()['state'] == 'unpublished': break
                time.sleep(.01)
            self.assertEqual(self.s.app_updates.status()['state'], 'unpublished')

class ProbeTests(unittest.TestCase):
    def test_probe_rejects_write_and_preserves_entire_library(self):
        with tempfile.TemporaryDirectory(dir=HERE, prefix='probe-readonly-') as folder:
            root = Path(folder); data = root/'data'; data.mkdir()
            with sqlite3.connect(data/'pocket_memory.db') as db:
                db.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY, content TEXT)')
                db.execute("INSERT INTO notes(content) VALUES('unchanged')")
                db.execute('CREATE TABLE note_tags(id INTEGER, tag TEXT)')
                db.execute("INSERT INTO note_tags VALUES(1, 'sensitive-cleanup-sentinel')")
            db.close()
            config = SimpleNamespace(path=root/'config.json')
            config.path.write_text('{}', encoding='utf-8')
            frontend = root/'frontend'; frontend.mkdir()
            for name in ('index.html','app.js','app-update.js','app-update.css'):
                (frontend/name).write_text('fixture', encoding='utf-8')
            def snapshot():
                return {str(p.relative_to(data)):hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}
            before = snapshot()
            with socket.socket() as available:
                available.bind(('127.0.0.1',0)); port=available.getsockname()[1]
            token = 'a'*64
            with patch.dict(os.environ, PM_UPDATE_HEALTH_TOKEN=token):
                t=threading.Thread(target=update_probe.run_probe,args=(config,data,frontend,'127.0.0.1',port),daemon=True); t.start()
                for _ in range(100):
                    try:
                        code, payload=http(port,'/api/update-health',headers={'X-Pocket-Update-Token':token}); break
                    except OSError: time.sleep(.01)
                self.assertEqual(code,200); self.assertEqual(payload['data_dir'],str(data.resolve()))
                self.assertEqual(http(port,'/api/update-health')[0],403)
                for verb in ('POST','PUT','DELETE'):
                    self.assertEqual(http(port,'/api/notes',method=verb,body=b'{}',headers={'X-Pocket-Update-Token':token})[0],403)
                self.assertEqual(http(port,'/api/shutdown',method='POST',body=b'{}')[0],403)
                self.assertEqual(http(port,'/api/shutdown',method='POST',body=b'{}',headers={'X-Pocket-Update-Token':token})[0],200)
                t.join(4); self.assertFalse(t.is_alive())
            self.assertEqual(snapshot(),before)

if __name__ == '__main__': unittest.main()
