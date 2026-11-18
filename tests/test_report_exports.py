# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Export correctness checks; no model, browser, or user library required."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pocket_memory.report_exports import ReportExportStore, handle_report_export_api
from pocket_memory.report_workspace import ReportWorkspace, TEMPLATES, digest, export_html


class NoModel:
    def generate_report_claims(self, *args):
        return []


class ReportExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.workspace = ReportWorkspace(SimpleNamespace(data_dir=self.data), NoModel(), threading.RLock())
        body = '负责人：林女士。\n\n| 任务 | 状态 |\n| --- | --- |\n| 核对资料 | 已完成 |\n| 跟进反馈 | 待办 |'
        self.unit = {'unit_id': 'n1:content', 'kind': 'note', 'source_id': 1, 'note_id': 1,
                     'title': '验收资料', 'tab': '正文', 'field': 'content',
                     'revision': digest(body), 'body': body}
        self.report = self.workspace.build('复盘：中文 / 导出', 'project',
            [{'kind': 'note', 'id': 1, 'title': '验收资料'}], [self.unit])
        self.workspace.write(self.report)
        self.exports = ReportExportStore(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def test_success_means_real_utf8_file_and_unchanged_report(self):
        before = self.workspace.path(self.report['id']).read_bytes()
        result = self.exports.create(self.workspace, self.report['id'], 1)
        path = Path(result['path'])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text('utf-8'), export_html(self.report))
        self.assertEqual(path.stat().st_size, result['bytes'])
        self.assertEqual(before, self.workspace.path(self.report['id']).read_bytes())
        self.assertIn('<svg', path.read_text('utf-8'))
        self.assertIn('id="source-1"', path.read_text('utf-8'))
        self.assertIn('负责人：林女士。', path.read_text('utf-8'))
        self.assertEqual(self.exports.describe(result['id']), result)
        self.assertEqual(result['view_url'], f"/api/report-exports/{result['id']}/view")
        self.assertEqual(result['download_url'], f"/api/report-exports/{result['id']}/download")

    def test_all_six_templates_keep_original_export_renderer(self):
        for template in (k for k in TEMPLATES if k!='auto'):
            with self.subTest(template=template):
                report = copy.deepcopy(self.report)
                report.update(template=template, template_label=TEMPLATES[template][0])
                self.workspace.write(report)
                saved = self.exports.create(self.workspace, report['id'], 1)
                self.assertEqual(Path(saved['path']).read_text('utf-8'), export_html(report))

    def test_stale_version_does_not_export(self):
        with self.assertRaisesRegex(ValueError, '文稿已更新'):
            self.exports.create(self.workspace, self.report['id'], 2)
        self.assertFalse((self.data / 'report-exports').exists())

    def test_failed_write_has_no_success_receipt(self):
        with patch('pocket_memory.report_exports.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.exports.create(self.workspace, self.report['id'], 1)
        self.assertEqual(list((self.data / 'report-exports').iterdir()), [])

    def test_only_managed_receipt_id_can_open_or_download(self):
        for invalid in ('../notes', 'C:\\Windows', '', '0' * 31, '0' * 32 + '.html'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.exports.file(invalid)
        result = self.exports.create(self.workspace, self.report['id'], 1)
        receipt = Path(result['directory']) / 'receipt.json'
        tampered = json.loads(receipt.read_text('utf-8'))
        tampered['filename'] = '../../private.html'
        receipt.write_text(json.dumps(tampered), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '记录无效'):
            self.exports.file(result['id'])

    def test_changed_or_deleted_file_is_not_reported_as_available(self):
        result = self.exports.create(self.workspace, self.report['id'], 1)
        path = Path(result['path'])
        path.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已修改'):
            self.exports.file(result['id'])
        path.unlink()
        with self.assertRaises(KeyError):
            self.exports.file(result['id'])

    def test_api_returns_real_file_and_downloads_identical_bytes(self):
        handler = ExportHandler(self.workspace, '/api/report-exports',
            {'report_id': self.report['id'], 'version': 1})
        self.assertTrue(handle_report_export_api(handler, 'POST'))
        self.assertTrue(handler.local_validated)
        result, code = handler.response
        self.assertEqual(code, 201)
        download = ExportHandler(self.workspace, result['download_url'])
        self.assertTrue(handle_report_export_api(download, 'GET'))
        self.assertEqual(download.download[0], Path(result['path']).read_bytes())
        self.assertEqual(download.download[1], result['filename'])
        self.assertEqual(download.download[2], 'text/html; charset=utf-8')

    def test_browser_preview_uses_the_saved_bytes_without_a_desktop_app(self):
        result = self.exports.create(self.workspace, self.report['id'], 1)
        handler = ExportHandler(self.workspace, result['view_url'])
        with patch('pocket_memory.report_exports.webbrowser.open') as browser, \
             patch('pocket_memory.report_exports.os.startfile', create=True) as desktop:
            self.assertTrue(handle_report_export_api(handler, 'GET'))
        self.assertTrue(handler.local_validated)
        self.assertEqual(handler.preview, Path(result['path']).read_bytes())
        browser.assert_not_called()
        desktop.assert_not_called()

    def test_preview_rejects_modified_exports_and_arbitrary_paths(self):
        result = self.exports.create(self.workspace, self.report['id'], 1)
        Path(result['path']).write_text('<script>changed()</script>', encoding='utf-8')
        handler = ExportHandler(self.workspace, result['view_url'])
        with self.assertRaisesRegex(ValueError, '已修改'):
            handle_report_export_api(handler, 'GET')
        self.assertFalse(hasattr(handler, 'preview'))
        handler = ExportHandler(self.workspace, '/api/report-exports/..%2Fprivate/view')
        with self.assertRaisesRegex(ValueError, '无效'):
            handle_report_export_api(handler, 'GET')

    def test_open_uses_the_bound_browser_url_when_html_association_is_broken(self):
        result = self.exports.create(self.workspace, self.report['id'], 1)
        handler = ExportHandler(self.workspace, f"/api/report-exports/{result['id']}/open", {})
        handler.headers['Host'] = 'localhost:1234'
        with patch('pocket_memory.report_exports.webbrowser.open', return_value=True) as browser, \
             patch('pocket_memory.report_exports.os.startfile', create=True,
                   side_effect=OSError('no HTML association')) as desktop:
            self.assertTrue(handle_report_export_api(handler, 'POST'))
        browser.assert_called_once_with('http://127.0.0.1:2751' + result['view_url'], new=2)
        desktop.assert_not_called()
        self.assertEqual(handler.response[0]['method'], 'browser')

    def test_browser_failure_preserves_the_desktop_file_fallback(self):
        result = self.exports.create(self.workspace, self.report['id'], 1)
        path = Path(result['path'])
        with patch.object(self.exports, 'file', return_value=(path, result)), \
             patch('pocket_memory.report_exports.os.name', 'nt'), \
             patch('pocket_memory.report_exports.webbrowser.open', return_value=False), \
             patch('pocket_memory.report_exports.os.startfile', create=True) as desktop:
            opened = self.exports.open(result['id'], view_url='http://127.0.0.1:2751' + result['view_url'])
        self.assertTrue(opened['opened'])
        desktop.assert_called_once_with(str(path))

    def test_open_folder_keeps_the_native_folder_action(self):
        result = self.exports.create(self.workspace, self.report['id'], 1)
        path = Path(result['path'])
        with patch.object(self.exports, 'file', return_value=(path, result)), \
             patch('pocket_memory.report_exports.os.name', 'nt'), \
             patch('pocket_memory.report_exports.webbrowser.open') as browser, \
             patch('pocket_memory.report_exports.os.startfile', create=True) as desktop:
            opened = self.exports.open(result['id'], folder=True, view_url='http://127.0.0.1:2751' + result['view_url'])
        self.assertTrue(opened['opened'])
        browser.assert_not_called()
        desktop.assert_called_once_with(str(path.parent))

    def test_open_failure_points_to_the_browser_and_download_actions(self):
        result = self.exports.create(self.workspace, self.report['id'], 1)
        path = Path(result['path'])
        with patch.object(self.exports, 'file', return_value=(path, result)), \
             patch('pocket_memory.report_exports.os.name', 'nt'), \
             patch('pocket_memory.report_exports.webbrowser.open', side_effect=OSError('no browser')), \
             patch('pocket_memory.report_exports.os.startfile', create=True, side_effect=OSError('no HTML association')):
            with self.assertRaisesRegex(ValueError, '浏览器查看或下载 HTML'):
                self.exports.open(result['id'], view_url='http://127.0.0.1:2751' + result['view_url'])

    def test_preview_response_is_inline_utf8_and_isolated_from_app_origin(self):
        from pocket_memory.server import PocketMemoryHandler

        handler = SimpleNamespace(send_response=Mock(), send_header=Mock(),
                                  end_headers=Mock(), wfile=io.BytesIO())
        payload = '<!doctype html><h1>中文报告</h1>'.encode('utf-8')
        PocketMemoryHandler._send_report_preview(handler, payload)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertEqual(handler.wfile.getvalue(), payload)
        self.assertEqual(headers['Content-Type'], 'text/html; charset=utf-8')
        self.assertEqual(headers['Content-Disposition'], 'inline')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Content-Length'], str(len(payload)))
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox allow-scripts;', headers['Content-Security-Policy'])
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin', headers['Content-Security-Policy'])
        self.assertIn("connect-src 'none'", headers['Content-Security-Policy'])

    def test_api_refuses_untrusted_action_or_caller_supplied_path(self):
        handler = ExportHandler(self.workspace, '/api/report-exports',
            {'report_id': self.report['id'], 'version': 1})
        handler.headers = {}
        with self.assertRaisesRegex(ValueError, '工作台'):
            handle_report_export_api(handler, 'POST')
        handler.headers = {'X-PocketMemory-Report-Export': '1'}
        handler.body['path'] = str(self.data / 'not-allowed.html')
        with self.assertRaisesRegex(ValueError, '不接受'):
            handle_report_export_api(handler, 'POST')


class ExportHandler:
    def __init__(self, workspace, path, body=None):
        self.path, self.body = path, body
        self.headers = {'X-PocketMemory-Report-Export': '1'}
        self.server = SimpleNamespace(store=workspace.store, report_workspace=workspace,
                                      operation_lock=threading.RLock(), server_address=('127.0.0.1', 2751))
        self.local_validated = False

    def _validate_local_cancellation(self):
        self.local_validated = True

    def _read_json(self):
        return self.body

    def _json(self, value, status=200):
        self.response = value, status

    def _send_download_file(self, path, filename, content_type):
        self.download = path.read_bytes(), filename, content_type

    def _send_report_preview(self, body):
        self.preview = body


if __name__ == '__main__':
    unittest.main()
