# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Read-only startup preflight. Never construct NoteStore or start AI workers.

This proves imports/assets/config/database readability, not a model evaluation or
a guarantee that every future business operation succeeds.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from pocket_memory.version import APP_VERSION


def check_inputs(config, data_dir, frontend):
    data_dir = Path(data_dir).resolve()
    database = data_dir / 'pocket_memory.db'
    if not database.is_file():
        raise ValueError('更新启动检查未找到已有资料库')
    # immutable prevents SQLite from writing even -shm files; the application
    # was cleanly stopped before this preflight. Never run schema cleanup here.
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'").fetchone():
            raise ValueError('资料库结构未能确认')
    if config.path.exists():
        value = json.loads(config.path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('配置不是对象')
    for name in ('index.html', 'app.js', 'app-update.js', 'app-update.css'):
        if not (Path(frontend) / name).is_file():
            raise ValueError('更新缺少界面资源：' + name)
    return {'ok': True, 'app_version': APP_VERSION, 'pid': os.getpid(),
            'data_dir': str(data_dir), 'config_path': str(config.path.resolve())}


def run_probe(config, data_dir, frontend, host, port):
    if host != '127.0.0.1':
        raise ValueError('启动检查仅允许本机地址')
    token = os.environ.get('PM_UPDATE_HEALTH_TOKEN', '')
    if not token:
        raise ValueError('启动检查缺少一次性令牌')
    result = check_inputs(config, data_dir, frontend)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def authorized(self):
            return secrets.compare_digest(self.headers.get('X-Pocket-Update-Token', ''), token)
        def reply(self, status, value):
            payload = json.dumps(value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def do_GET(self):
            if self.path != '/api/update-health' or not self.authorized():
                self.reply(403, {'error': '启动检查期间不可访问资料'})
                return
            self.reply(200, dict(result, token=token))
        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = -1
            if 0 <= length <= 4096:
                self.rfile.read(length)
            else:
                self.reply(413, {'error': '启动检查不接收资料上传'})
                return
            if self.path != '/api/shutdown' or not self.authorized():
                self.reply(403, {'error': '启动检查期间不可修改资料'})
                return
            self.reply(200, {'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        do_PUT = do_POST
        do_DELETE = do_POST
    with ThreadingHTTPServer((host, port), Handler) as server:
        server.serve_forever()
    return 0
