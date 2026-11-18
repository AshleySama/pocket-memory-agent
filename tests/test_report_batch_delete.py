# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Batch deletion uses temporary report fixtures; no model or real library."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pocket_memory.report_workspace import ReportWorkspace


class ReportBatchDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.workspace = ReportWorkspace(SimpleNamespace(data_dir=self.data), None, threading.RLock())
        self.reports = []
        for index, letter in enumerate('abc', 1):
            report = {'id': letter * 32, 'title': f'夹具报告 {index}', 'version': index,
                      'template': 'project', 'updated': '2026-11-18T00:00:00+00:00',
                      'notes': {'summary': f'保留人工编辑 {index}'}}
            self.workspace.write(report)
            self.reports.append(report)
        self.originals = {r['id']: self.workspace.path(r['id']).read_bytes() for r in self.reports}
        self.note = self.data / 'fixture-note.md'
        self.note.write_text('源笔记保持不变', encoding='utf-8')
        (self.data / 'report-exports').mkdir()
        self.export = self.data / 'report-exports' / 'fixture.html'
        self.export.write_text('<h1>已导出的 HTML</h1>', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def selection(self, reports=None):
        return [{k: report[k] for k in ('id', 'version')} for report in
                (self.reports if reports is None else reports)]

    def assert_originals(self):
        for report_id, content in self.originals.items():
            self.assertEqual(self.workspace.path(report_id).read_bytes(), content)

    def test_selected_batch_roundtrip_preserves_edits_sources_and_exports(self):
        note, exported = self.note.read_bytes(), self.export.read_bytes()
        result = self.workspace.delete_many(self.selection(self.reports[:2]))
        self.assertEqual(result['deleted'], [r['id'] for r in self.reports[:2]])
        self.assertEqual(result['count'], 2)
        self.assertEqual([r['id'] for r in self.workspace.list_reports()], [self.reports[2]['id']])
        restarted = ReportWorkspace(self.workspace.store, None, threading.RLock())
        restored = restarted.restore_many(result['undo_token'])
        self.assertEqual(restored, {'restored': result['deleted'], 'count': 2})
        self.assert_originals()
        self.assertEqual(self.note.read_bytes(), note)
        self.assertEqual(self.export.read_bytes(), exported)
        with self.assertRaises(KeyError):
            restarted.restore_many(result['undo_token'])

    def test_clear_all_requires_the_exact_confirmed_catalogue(self):
        with self.assertRaisesRegex(ValueError, '列表已变化'):
            self.workspace.delete_many(self.selection(self.reports[:2]), all_reports=True)
        self.assert_originals()
        result = self.workspace.delete_many(self.selection(), all_reports=True)
        self.assertEqual(result['count'], 3)
        self.assertEqual(self.workspace.list_reports(), [])
        self.workspace.restore_many(result['undo_token'])
        self.assert_originals()

    def test_last_stale_version_leaves_every_report_in_place(self):
        selected = self.selection()
        selected[-1]['version'] += 1
        with self.assertRaisesRegex(ValueError, '已更新'):
            self.workspace.delete_many(selected)
        self.assert_originals()
        self.assertFalse((self.workspace.directory / '.trash').exists())

    def test_active_or_composing_report_blocks_the_entire_batch(self):
        for status in ('queued', 'running', 'composing'):
            with self.subTest(status=status):
                self.workspace.jobs = {}
                self.workspace.composing_report = None
                if status == 'composing':
                    self.workspace.composing_report = self.reports[-1]['id']
                else:
                    self.workspace.jobs['job'] = {'report_id': self.reports[-1]['id'], 'status': status}
                with self.assertRaisesRegex(ValueError, '生成或成稿'):
                    self.workspace.delete_many(self.selection())
                self.assert_originals()
        self.workspace.composing_report = None
        self.workspace.jobs = {'job': {'report_id': self.reports[-1]['id'], 'status': 'done'}}
        self.assertEqual(self.workspace.delete_many(self.selection())['count'], 3)

    def test_invalid_or_duplicate_selections_make_no_changes(self):
        cases = ([], {}, [self.selection()[0]] * 2,
                 [{'id': '../private', 'version': 1}],
                 [{'id': self.reports[0]['id'], 'version': True}],
                 [{'id': self.reports[0]['id'], 'version': 1, 'path': 'outside'}])
        for selected in cases:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.workspace.delete_many(selected)
            self.assert_originals()

    def test_move_failure_rolls_back_the_already_moved_report(self):
        original_replace, calls = os.replace, []

        def fail_second(source, target):
            calls.append((source, target))
            if len(calls) == 2:
                raise OSError('fixture move denied')
            return original_replace(source, target)

        with patch('pocket_memory.report_workspace.os.replace', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.workspace.delete_many(self.selection())
        self.assert_originals()
        self.assertEqual(list((self.workspace.directory / '.trash').iterdir()), [])

    def test_restore_collision_does_not_restore_any_other_report(self):
        result = self.workspace.delete_many(self.selection())
        replacement = dict(self.reports[-1], title='新报告不可被覆盖')
        self.workspace.write(replacement)
        with self.assertRaisesRegex(ValueError, '本批未恢复'):
            self.workspace.restore_many(result['undo_token'])
        self.assertFalse(self.workspace.path(self.reports[0]['id']).exists())
        self.assertFalse(self.workspace.path(self.reports[1]['id']).exists())
        self.assertEqual(self.workspace.read(replacement['id'])['title'], replacement['title'])

    def test_restore_move_failure_rolls_back_and_can_be_retried(self):
        result = self.workspace.delete_many(self.selection())
        original_replace, calls = os.replace, []

        def fail_second(source, target):
            calls.append((source, target))
            if len(calls) == 2:
                raise OSError('fixture restore denied')
            return original_replace(source, target)

        with patch('pocket_memory.report_workspace.os.replace', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.workspace.restore_many(result['undo_token'])
        self.assertEqual(self.workspace.list_reports(), [])
        self.workspace.restore_many(result['undo_token'])
        self.assert_originals()

    def test_incomplete_or_invalid_undo_records_never_restore_part_of_a_batch(self):
        with self.assertRaises(ValueError):
            self.workspace.restore_many('../private')
        result = self.workspace.delete_many(self.selection())
        folder = self.workspace._trash_path(result['undo_token']).with_suffix('')
        (folder / (self.reports[-1]['id'] + '.json')).unlink()
        with self.assertRaisesRegex(ValueError, '不完整'):
            self.workspace.restore_many(result['undo_token'])
        self.assertEqual(self.workspace.list_reports(), [])

    def test_api_batch_delete_and_restore_validate_local_caller_and_payload(self):
        from pocket_memory.server import PocketMemoryHandler

        handler = SimpleNamespace(path='/api/report-workspace/delete-batch',
            server=SimpleNamespace(report_workspace=self.workspace),
            _retired_table_analysis=lambda _: False, _validate_note_library_write=lambda _: True,
            _validate_local_cancellation=Mock(), _read_json=lambda: {'reports': self.selection(), 'all_reports': False},
            _json=Mock())
        with patch('pocket_memory.report_exports.handle_report_export_api', return_value=False), \
             patch('pocket_memory.office_api.handle_office_api', return_value=False):
            PocketMemoryHandler._handle_post(handler)
            result = handler._json.call_args.args[0]
            self.assertEqual(result['count'], 3)
            handler.path = '/api/report-workspace/restore-batch'
            handler._read_json = lambda: {'undo_token': result['undo_token']}
            PocketMemoryHandler._handle_post(handler)
            self.assertEqual(handler._json.call_args.args[0]['count'], 3)
            handler.path = '/api/report-workspace/delete-batch'
            handler._read_json = lambda: {'reports': self.selection(), 'all_reports': False, 'path': 'private'}
            with self.assertRaisesRegex(ValueError, '无效'):
                PocketMemoryHandler._handle_post(handler)
        self.assertEqual(handler._validate_local_cancellation.call_count, 3)
        self.assert_originals()


if __name__ == '__main__':
    unittest.main()
