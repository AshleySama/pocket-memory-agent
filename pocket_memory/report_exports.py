# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Durable, library-local report exports for the browser and desktop WebView.

The UI receives a path only after the HTML and its receipt have been written.
Requests identify receipts, never arbitrary local paths. Rendering is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import webbrowser
from urllib.parse import urlparse

from pocket_memory.report_workspace import export_html, now


PREFIX = '/api/report-exports'


class ReportExportStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()

    @property
    def directory(self):
        root = self.data_dir / 'report-exports'
        root.mkdir(exist_ok=True)
        if root.is_symlink() or root.resolve().parent != self.data_dir:
            raise ValueError('文稿导出目录无效，请检查资料目录')
        return root

    def _folder(self, export_id):
        if not isinstance(export_id, str) or not re.fullmatch(r'[a-f0-9]{32}', export_id):
            raise ValueError('无效的文稿导出编号')
        root = self.directory
        folder = root / export_id
        if folder.is_symlink() or folder.resolve().parent != root.resolve():
            raise ValueError('文稿导出路径无效')
        return folder

    @staticmethod
    def _filename(title, version):
        # A fixed prefix also avoids Windows device names such as CON/NUL.
        title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '-', str(title))[:70].strip(' .')
        return f'资料汇编-{title or "未命名"}-v{version}.html'

    def create(self, workspace, report_id, version):
        if type(version) is not int or version < 1:
            raise ValueError('请重新打开文稿后再导出')
        with workspace.lock:
            report = workspace.read(report_id)
            if report['version'] != version:
                raise ValueError('文稿已更新，请重新打开后再导出；本次未导出旧版本')
            payload = export_html(report).encode('utf-8')
        export_id = uuid.uuid4().hex
        folder = self._folder(export_id)
        folder.mkdir()
        filename = self._filename(report['title'], version)
        path = folder / filename
        temp = folder / '.report.tmp'
        receipt_path = folder / 'receipt.json'
        receipt = {'id': export_id, 'report_id': report_id, 'version': version,
                   'title': report['title'], 'filename': filename, 'created': now(),
                   'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        try:
            with temp.open('xb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            with receipt_path.open('x', encoding='utf-8') as stream:
                json.dump(receipt, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            # Never turn an attempted download into a false success notification.
            return self.describe(export_id)
        except Exception:
            # This directory was created solely for this export attempt.
            for item in (temp, receipt_path, path):
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                folder.rmdir()
            except OSError:
                pass
            raise

    def file(self, export_id):
        folder = self._folder(export_id)
        receipt_path = folder / 'receipt.json'
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise KeyError('导出文件不存在，请重新导出')
        receipt = json.loads(receipt_path.read_text('utf-8'))
        filename = receipt.get('filename', '')
        if (receipt.get('id') != export_id or not isinstance(filename, str)
                or not filename.endswith('.html') or filename != Path(filename).name
                or any(char in filename for char in '/\\:')):
            raise ValueError('文稿导出记录无效，请重新导出')
        path = folder / filename
        if path.is_symlink() or path.resolve().parent != folder.resolve() or not path.is_file():
            raise KeyError('导出文件已移动或删除，请重新导出')
        body = path.read_bytes()
        if len(body) != receipt.get('bytes') or hashlib.sha256(body).hexdigest() != receipt.get('sha256'):
            raise ValueError('导出文件已修改，请重新导出后再打开')
        return path, receipt

    def describe(self, export_id):
        path, receipt = self.file(export_id)
        return {**receipt, 'path': str(path), 'directory': str(path.parent),
                'view_url': f'{PREFIX}/{export_id}/view',
                'download_url': f'{PREFIX}/{export_id}/download'}

    def open(self, export_id, folder=False, view_url=None):
        path, _ = self.file(export_id)
        # HTTP browser associations can work even when .html is not associated
        # with an application. The API constructs this URL from its bound local
        # address; callers cannot supply an external URL or a file path.
        if not folder and view_url:
            try:
                if webbrowser.open(view_url, new=2):
                    return {'opened': True, 'id': export_id, 'method': 'browser'}
            except (OSError, webbrowser.Error):
                pass
        if os.name != 'nt' or not hasattr(os, 'startfile'):
            raise ValueError('当前环境不能打开本机文件，请使用浏览器查看或下载 HTML')
        try:
            os.startfile(str(path.parent if folder else path))
        except OSError as exc:
            message = ('无法打开文件夹；文稿仍保存在上述位置，可复制路径打开'
                       if folder else '无法启动默认应用；请使用浏览器查看或下载 HTML')
            raise ValueError(message) from exc
        return {'opened': True, 'id': export_id}


def handle_report_export_api(handler, method):
    parsed = urlparse(handler.path)
    if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + '/'):
        return False
    handler._validate_local_cancellation()
    parts = parsed.path[len(PREFIX):].strip('/').split('/') if parsed.path != PREFIX else []
    if method == 'POST':
        if handler.headers.get('X-PocketMemory-Report-Export') != '1':
            raise ValueError('请从资料汇编工作台导出或打开文稿')
        body = handler._read_json()
        if not isinstance(body, dict):
            raise ValueError('无效的文稿导出请求')
    else:
        body = {}
    # Switching libraries cannot redirect an in-flight export to another store.
    with handler.server.operation_lock:
        store = ReportExportStore(handler.server.store.data_dir)
        if method == 'POST' and not parts:
            if set(body) != {'report_id', 'version'}:
                raise ValueError('文稿导出不接受自选保存路径')
            try:
                exported = store.create(handler.server.report_workspace, body['report_id'], body['version'])
            except OSError as exc:
                raise ValueError('文稿未能保存，请检查资料目录的剩余空间与写入权限后重试') from exc
            handler._json(exported, 201)
        elif method == 'GET' and len(parts) == 2 and parts[1] == 'download':
            path, receipt = store.file(parts[0])
            handler._send_download_file(path, receipt['filename'], 'text/html; charset=utf-8')
        elif method == 'GET' and len(parts) == 2 and parts[1] == 'view':
            path, _ = store.file(parts[0])
            handler._send_report_preview(path.read_bytes())
        elif method == 'POST' and len(parts) == 2 and parts[1] in ('open', 'open-folder'):
            if body:
                raise ValueError('打开文稿不接受自选文件路径')
            host, port = handler.server.server_address[:2]
            if ':' in host:
                host = f'[{host}]'
            view_url = f'http://{host}:{port}{PREFIX}/{parts[0]}/view'
            handler._json(store.open(parts[0], folder=parts[1] == 'open-folder', view_url=view_url))
        else:
            raise KeyError('文稿导出不存在')
    return True
