# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Focused checks for calculation and provenance, no model calls.
XLSX fixtures are minimal test inputs, never presented as user business data.
"""
import base64
from datetime import date
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('sheet_analysis', ROOT/'pocket_memory/sheet_analysis.py')
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def fixture(rows, hidden=None):
    """Tiny OOXML generator; numeric strings retain their raw test values."""
    from openpyxl.utils import get_column_letter
    content = []
    for number, values in enumerate(rows, 1):
        cells = []
        for col, value in enumerate(values, 1):
            ref = get_column_letter(col)+str(number)
            if isinstance(value, dict):
                kind = value['type']
                style = ' s="1"' if kind == 'date_serial' else ''
                xtype = {'number':'n','date_serial':'n','error':'e','bool':'b'}[kind]
                cells.append(f'<c r="{ref}" t="{xtype}"{style}><v>{escape(str(value["value"]))}</v></c>')
            elif isinstance(value, tuple):
                cells.append(f'<c r="{ref}"><f>{escape(value[0])}</f><v>{escape(str(value[1]))}</v></c>')
            elif value is not None:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
        content.append(f'<row r="{number}"'+(' hidden="1"' if number == hidden else '')+'>'+''.join(cells)+'</row>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="任务" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/styles.xml', '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        z.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:'+get_column_letter(max(map(len, rows)))+str(len(rows))+'"/><sheetData>'+''.join(content)+'</sheetData></worksheet>')
    return buffer.getvalue()


class Verification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = mod.SheetAnalysis(self.tmp.name)
        self.rows = [['名称','状态','负责人','期限','金额'],
                     ['甲','完成','张','2026-11-05','0.1'],
                     ['乙','进行中','李','2026/11/06','0.2'],
                     ['丙','进行中','张','2026年11月07日','1'],
                     ['丁','进行中','','录制前一天',('SUM(E2:E4)',999)],
                     ['', '完成','李','2026-11-01','100'],
                     ['乙','进行中','李','2026/11/06','0.2']]
        self.dataset = self.import_(self.rows)
        self.config = {'dataset_id': self.dataset['id'], 'sheet':'任务', 'header_row':1,
                       'as_of':'2026-11-07', 'columns': {'title':'A','status':'B','owner':'C','due':'D','number':'E'},
                       'completed_values':['完成'], 'numeric_unit':'元'}

    def tearDown(self):
        self.tmp.cleanup()

    def import_(self, rows, **kwargs):
        return self.service.import_file({'filename':'测试.xlsx', 'data_url':'data:application/octet-stream;base64,'+base64.b64encode(fixture(rows, **kwargs)).decode()})

    def test_decimal_dates_denominator(self):
        r = self.service.analyze(self.config); s = r['summary']
        self.assertEqual((s['total_rows'],s['eligible_rows'],s['excluded_rows']), (6,5,1))
        self.assertEqual((s['completed'],s['open'],s['overdue'],s['unknown_due']), (1,4,2,1))
        self.assertEqual(s['numeric'], {'sum':'1.5','valid':4,'invalid':1,'missing':0,'unit':'元'})
        self.assertEqual(next(g for g in r['groups']['status'] if g['label']=='进行中')['rows'], [3,4,5,7])
        self.assertEqual(next(i for i in r['issues'] if i['code']=='duplicate')['rows'],[3,7])
        self.assertEqual(r['timing']['model_calls'],0)

    def test_no_completed_inference(self):
        r = self.service.analyze({**self.config,'completed_values':[]})
        self.assertIsNone(r['summary']['completed']); self.assertIsNone(r['summary']['overdue'])
        self.assertIn('未指定完成状态',mod.render_report(r))

    def test_relative_dates_are_unknown_not_zero(self):
        d = self.import_([['名称','状态','负责人','期限'],['甲','进行中','张','录制前一天']])
        c = {**self.config,'dataset_id':d['id'],'columns':{'title':'A','status':'B','due':'D'}}
        r = self.service.analyze(c)
        self.assertIsNone(r['summary']['overdue']); self.assertEqual(r['summary']['unknown_due'],1)

    def test_formula_cache_never_calculated(self):
        r = self.service.analyze(self.config)
        source = self.service.source(r['id'],5)
        c = source['cells'][4]
        self.assertEqual(c['formula'],'=SUM(E2:E4)'); self.assertEqual(c['cached'],'999')
        self.assertEqual(r['summary']['numeric']['sum'],'1.5')

    def test_range_excludes_footer(self):
        r = self.service.analyze({**self.config,'start_row':2,'end_row':4})
        self.assertEqual(r['summary']['numeric']['sum'],'1.3')
        self.assertEqual([r['row'] for r in r['rows']],[2,3,4])
        self.assertEqual(r['lineage']['range'],'A2:E4')
        with self.assertRaises(ValueError): self.service.analyze({**self.config,'end_row':100})

    def test_source_revision_edit_and_export(self):
        r = self.service.analyze(self.config)
        saved = self.service.save(r['id'],{'version':1,'title':'复盘 <script>','notes':'人工说明 <img src=x>'})
        self.assertEqual(saved['summary'],r['summary'])
        with self.assertRaises(ValueError): self.service.save(r['id'],{'version':1})
        html = mod.export_html(saved)
        self.assertNotIn('<script>',html); self.assertIn('&lt;img src=x&gt;',html)
        self.assertEqual(html.count('id="source-'),6)
        self.assertIn('href="#source-7"',html)
        self.assertNotIn('http://',html); self.assertNotIn('https://',html)
        path = self.service.datasets/(self.dataset['id']+'.xlsx')
        path.write_bytes(path.read_bytes()+b'tamper')
        with self.assertRaises(ValueError): self.service.source(r['id'],2)

    def test_duplicate_headers_and_hidden_rows(self):
        d = self.import_([['名称','状态','状态'],['甲','试用','已验证']],hidden=2)
        p = self.service.preview({'dataset_id':d['id'],'sheet':'任务','header_row':1})
        self.assertEqual(p['row_count'],1); self.assertIn('同名表头',p['issues'][0])
        r = self.service.analyze({**self.config,'dataset_id':d['id'],'columns':{'title':'A','status':'C'},'completed_values':[]})
        self.assertEqual(r['groups']['status'][0]['label'],'已验证')

    def test_invalid_mapping_and_limit(self):
        with self.assertRaises(ValueError): self.service.analyze({**self.config,'columns':{'title':'A','status':'A'}})
        with self.assertRaises(ValueError): self.service.analyze({**self.config,'numeric_unit':''})
        d = self.import_([['C'+str(i) for i in range(65)], ['v']*65])
        with self.assertRaises(ValueError): self.service.preview({'dataset_id':d['id'],'sheet':'任务','header_row':1})
        with self.assertRaises(ValueError): self.service.dataset('../escape')

    def test_empty_status_does_not_become_open(self):
        d = self.import_([self.rows[0],['甲','','张','2026-11-01','0']])
        r = self.service.analyze({**self.config,'dataset_id':d['id']})
        self.assertEqual(r['summary']['unknown_status'],1); self.assertEqual(r['summary']['open'],0)
        self.assertIsNone(r['summary']['overdue'])
        self.assertEqual(r['summary']['numeric']['sum'],'0')

    def test_native_number_date_serial_and_error(self):
        from openpyxl.utils.datetime import to_excel
        d = self.import_([self.rows[0],
            ['甲','完成','张',{'type':'date_serial','value':to_excel(date(2026,11,5))},{'type':'number','value':0.1}],
            ['乙','进行中','李',{'type':'date_serial','value':to_excel(date(2026,11,6))},{'type':'number','value':0.2}],
            ['丙',{'type':'error','value':'#N/A'},'李',{'type':'date_serial','value':to_excel(date(2026,11,6))},{'type':'error','value':'#VALUE!'}]])
        r = self.service.analyze({**self.config,'dataset_id':d['id']})
        self.assertEqual(r['summary']['numeric']['sum'],'0.3')
        self.assertEqual(r['summary']['overdue'],1)
        self.assertEqual(r['summary']['numeric']['invalid'],1)
        self.assertEqual(r['rows'][1]['due']['parsed'],'2026-11-06')
        self.assertEqual(self.service.source(r['id'],3)['cells'][3]['type'],'date')
        self.assertEqual(self.service.list()['reports'][0]['id'],r['id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
