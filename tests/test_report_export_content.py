# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Check legacy report presentation and standalone citation controls with fixtures."""
import copy
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest

from pocket_memory.report_workspace import ReportWorkspace, digest, export_html, make_ref, render_report


class ReportExportContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = ReportWorkspace(SimpleNamespace(data_dir=Path(self.temp.name)),
            SimpleNamespace(generate_report_claims=lambda *args: []), threading.RLock())
        body = '服务启用前必须审批，并由专员核对申请。\n\n| 任务 | 状态 |\n| --- | --- |\n| 审批 | 待办 |\n| 复核 | 已完成 |'
        unit = dict(unit_id='n1:content', kind='note', source_id=1, note_id=1,
                    title='夹具资料', tab='正文', field='content', revision=digest(body), body=body)
        self.report = self.workspace.build('项目报告', 'project', [{'kind': 'note', 'id': 1, 'title': '夹具资料'}], [unit])

    def tearDown(self):
        self.temp.cleanup()

    def test_presentation_hides_engineering_appendices_but_preserves_business_content(self):
        self.report['notes']['gaps'] = '需人工确认审批负责人。'
        self.report['limitations'] = ['内部生成过程说明，仅保存在记录中。']
        before = copy.deepcopy(self.report)
        for output in (render_report(self.report), export_html(self.report)):
            self.assertIn('服务启用前必须审批', output)
            self.assertIn('需人工确认审批负责人。', output)
            self.assertIn('<svg', output)
            self.assertIn('审批</td>', output)
            self.assertNotIn('<summary>编制说明</summary>', output)
            self.assertNotIn('内部生成过程说明，仅保存在记录中。', output)
            self.assertNotIn('<footer class="report-footer">', output)
            self.assertNotIn('<summary>参考资料</summary>', output)
        self.assertEqual(self.report, before)

    def test_offline_appendix_only_has_cited_passages_and_starts_collapsed(self):
        unit = self.report['units'][0]
        unused = make_ref(unit, 0, len('服务启用前必须审批'))
        unused['id'] = len(self.report['refs']) + 1
        self.report['refs'].append(unused)
        output = export_html(self.report)
        self.assertIn('id="report-source-appendix"><summary>引用核对</summary>', output)
        self.assertNotIn('id="report-source-appendix" open', output)
        self.assertNotIn(f'id="source-{unused["id"]}"', output)
        used = set(re.findall(r'data-ref="(\d+)"', output))
        anchors = set(re.findall(r'id="source-(\d+)"', output))
        self.assertEqual(used, anchors)
        self.assertTrue(used)
        self.assertNotIn(' · 修订 ', output)
        self.assertEqual(output.count('<script'), 1)
        self.assertIn('<script data-report-source-links>', output)
        self.assertNotIn('<script src=', output)


if __name__ == '__main__':
    unittest.main()
