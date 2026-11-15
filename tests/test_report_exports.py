# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Export correctness checks; no model, browser, or user library required."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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

    def test_all_six_templates_keep_original_export_renderer(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                report = copy.deepcopy(self.report)
                report.update(template=template, template_label=TEMPLATES[template][0])
                self.workspace.write(report)
                saved = self.exports.create(self.workspace, report['id'], 1)
                self.assertEqual(Path(saved['path']).read_text('utf-8'), export_html(report))

    def test_stale_version_does_not_export(self):
        with self.assertRaisesRegex(ValueError, '报告已更新'):
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
                                      operation_lock=threading.RLock())
        self.local_validated = False

    def _validate_local_cancellation(self):
        self.local_validated = True

    def _read_json(self):
        return self.body

    def _json(self, value, status=200):
        self.response = value, status

    def _send_download_file(self, path, filename, content_type):
        self.download = path.read_bytes(), filename, content_type


if __name__ == '__main__':
    unittest.main()
