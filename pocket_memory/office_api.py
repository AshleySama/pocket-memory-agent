# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Small, library-scoped adapters for the local office tools."""
from __future__ import annotations

from datetime import datetime
from contextlib import closing
import hashlib
import json
import sqlite3
from urllib.parse import parse_qs, urlparse

from pocket_memory.drafts import library_identity
from pocket_memory.office_tools import TodoStore, LIMITS


def _text(value, label, maximum, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f'{label}不能为空且最多 {maximum} 字' if required else f'{label}最多 {maximum} 字')
    return value.strip()


def _source(server, value):
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict):
        raise ValueError('来源格式不正确')
    note_id = value.get('note_id')
    if note_id is None:
        return {'title': _text(value.get('title', '粘贴文字'), '来源标题', 200)}
    if isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 1:
        raise ValueError('来源笔记编号不正确')
    note = server.store.get_note(note_id)
    return {'note_id': note.id, 'title': note.title, 'tab_name': note.tab_name}


def _receipt(server, action, key, payload, create):
    """Persist retries; an interrupted write is never silently repeated."""
    key = _text(key, '请求编号', 100)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = server.store.data_dir / 'office-receipts.db'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS receipts(action TEXT, key TEXT, digest TEXT, result TEXT, PRIMARY KEY(action,key))')
        row = db.execute('SELECT digest,result FROM receipts WHERE action=? AND key=?', (action, key)).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError('这次保存的内容已改变，请重新保存')
            if row[1] is None:
                raise ValueError('上次保存结果尚未确认，请先查看笔记或提醒，避免重复保存')
            return json.loads(row[1])
        db.execute('INSERT INTO receipts VALUES(?,?,?,NULL)', (action, key, digest))
        db.commit()
        result = create()
        db.execute('UPDATE receipts SET result=? WHERE action=? AND key=?', (json.dumps(result, ensure_ascii=False), action, key))
        return result


def handle_office_api(handler, method):
    parsed = urlparse(handler.path)
    if parsed.path != '/api/office-tools' and not parsed.path.startswith('/api/office-tools/'):
        return False
    handler._validate_local_cancellation()
    server = handler.server
    parts = parsed.path.strip('/').split('/')[2:]
    body = handler._read_json() if method == 'POST' else {}
    if not isinstance(body, dict):
        raise ValueError('请求必须为对象')
    with server.operation_lock:
        library = library_identity(server.store.data_dir)
        supplied = body.get('library_id') if method == 'POST' else parse_qs(parsed.query).get('library_id', [None])[0]
        if not (method == 'GET' and not parts) and supplied != library:
            handler._json({'error': '资料库已改变，请重新打开工具；本次内容没有写入其他资料库', 'code': 'library_mismatch'}, 409)
            return True
        todos = TodoStore(server.store.data_dir)
        if method == 'GET' and not parts:
            handler._json({'library_id': library, 'limits': LIMITS, 'todos': todos.list()})
        elif method == 'GET' and len(parts) == 2 and parts[0] == 'jobs':
            handler._json(server.office_tools.get_job(parts[1], library))
        elif method == 'POST' and parts == ['jobs']:
            body['source'] = _source(server, body.get('source'))
            handler._json(server.office_tools.create_job(body, library), 202)
        elif method == 'POST' and parts == ['todos']:
            source = _source(server, body.get('source'))
            todos.add(body.get('items'), source, body.get('request_id'))
            handler._json({'todos': todos.list()}, 201)
        elif method == 'POST' and len(parts) == 2 and parts[0] == 'todos':
            handler._json({'todo': todos.update(parts[1], {k: v for k, v in body.items() if k != 'library_id'})})
        elif method == 'POST' and len(parts) == 3 and parts[0] == 'todos' and parts[2] == 'remind':
            todo = next((item for item in todos.list() if str(item['id']) == parts[1]), None)
            if todo is None:
                raise KeyError(parts[1])
            if todo['completed']:
                raise ValueError('该待办已完成，请重新打开后设置提醒')
            when = _text(body.get('datetime_str'), '提醒时间', 16)
            try:
                due = datetime.strptime(when, '%Y-%m-%d %H:%M')
            except ValueError as exc:
                raise ValueError('请选择有效的提醒日期和时间') from exc
            if due <= datetime.now():
                raise ValueError('提醒时间需要晚于当前时间')
            payload = {'datetime_str': when, 'message': todo['task']}
            result = _receipt(server, 'reminder', f'{todo["id"]}:{when}', payload,
                              lambda: server.tool_registry.schedule(**payload))
            handler._json({'result': result})
        elif method == 'POST' and parts == ['save-note']:
            title = _text(body.get('title'), '笔记标题', 200)
            content = _text(body.get('content'), '笔记内容', 20000)
            def create():
                note = server.store.create_note(title, content, tags=['工具成果'], ai_status='not_requested')
                server.embedding_service.enqueue(note.id)
                return note.as_dict()
            note = _receipt(server, 'note', body.get('request_id'), {'title': title, 'content': content}, create)
            handler._json({'note': note}, 201)
        else:
            raise KeyError(parsed.path)
    return True
