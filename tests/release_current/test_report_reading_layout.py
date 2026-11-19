# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import copy
import unittest
import tempfile
from pathlib import Path

from pocket_memory.report_workspace import digest
from pocket_memory.report_reading_view import text_layout, project, chapter_text, numbered_headings
from pocket_memory.report_source_markup import render_body


def unit(uid, body):
    return dict(unit_id=uid, body=body, revision=digest(body), kind='document', source_id=1, field='document_text')


def span(u):
    return dict(unit_id=u['unit_id'],start=0,end=len(u['body']),text=u['body'])


class ReadingLayoutTests(unittest.TestCase):
    def test_restore_original_decimal_and_paragraphs_not_guessing(self):
        original='总金额为3.5万元。审批通过后才可支付。\n\n仍需核验合同。'
        u=unit('a','总金额为3.\n5万元。\n审批通过后才可支付。\n\n仍需核验合同。')
        before=copy.deepcopy(u); layout=text_layout(original,[u])
        self.assertEqual(project(span(u),u,layout),original)
        self.assertEqual(u,before)
        self.assertEqual(project(span(u),u,text_layout('总金额为3.6万元。',[u])),u['body'])

    def test_adjacent_chunks_restore_cross_chunk_sentence(self):
        us=[unit('a','必须审\n批'),unit('b','通过后\n才能执行。')]
        layout=text_layout('必须审批通过后才能执行。',us)
        ch=dict(segments=[span(u) for u in us])
        self.assertEqual(chapter_text(ch,{u['unit_id']:u for u in us},layout),'必须审批通过后才能执行。')
        separated=text_layout('必须审批\n\n通过后才能执行。',us)
        self.assertEqual(chapter_text(ch,{u['unit_id']:u for u in us},separated),'必须审批\n\n通过后才能执行。')

    def test_ambiguous_repeat_or_stale_revision_preserves_source(self):
        u=unit('a','审批\n完成。')
        self.assertEqual(project(span(u),u,text_layout('审批完成。审批完成。',[u])),u['body'])
        layout=text_layout('审批完成。',[u]);layout['units'][0]['revision']='stale'
        self.assertEqual(project(span(u),u,layout),u['body'])

    def test_sequential_chinese_hierarchy_and_code_protection(self):
        body='一、准备工作\n准备原件。\n（一）材料核验。\n核验甲。\n（二）审批流程。\n未审批不执行。\n二、实施要求\n实施乙。'
        html=render_body(numbered_headings(body))
        self.assertEqual(html.count('<h3>'),2);self.assertEqual(html.count('<h4>'),2)
        self.assertIn('未审批不执行',html)
        code='```text\n一、准备工作\n二、实施要求\n```'
        self.assertEqual(numbered_headings(code),code)
        self.assertNotIn('<h3>',render_body(numbered_headings('一、孤立标题\n说明。')))
        self.assertNotIn('<h3>',render_body(numbered_headings('一、甲已批准。乙尚未执行。\n二、丙未批准。')))

    def test_display_receipt_cannot_rewrite_facts(self):
        u=unit('a','金额3万元。')
        layout=dict(units=[dict(unit_id='a',revision=u['revision'])],edits=[dict(
            unit_id='a',start=2,end=3,replacement='5',reason='original_text_whitespace')])
        self.assertEqual(project(span(u),u,layout),u['body'])

    def test_pdf_mismatch_and_missing_original_never_authorize_edits(self):
        import pymupdf
        from pocket_memory.report_pdf_reading_layout import build_layout
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.pdf'
            doc=pymupdf.open();page=doc.new_page()
            page.insert_text((90,100),'Original condition: approval is required.',fontsize=16)
            doc.save(path);doc.close()
            u=unit('a','Original condition: approval is NOT required.')
            u['tab']='第 1 页'
            result=build_layout(path,[u])
            self.assertEqual(result['edits'],[]);self.assertEqual(result['continuations'],[])
            self.assertEqual(result['units'][0]['status'],'unavailable')
            self.assertEqual(build_layout(Path(directory)/'missing.pdf',[u])['edits'],[])

    def test_pdf_page_number_proof_does_not_delete_body_numbers(self):
        import pymupdf
        from pocket_memory.report_pdf_reading_layout import build_layout
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.pdf';doc=pymupdf.open()
            for i in range(2):
                page=doc.new_page(width=595,height=842)
                for j in range(6):
                    page.insert_text((90,100+j*30),f'Page {i+1} line {j+1}: approval is needed before payment.',fontsize=16)
                page.insert_text((294,795),str(i+1),fontsize=12)
            doc.save(path)
            us=[]
            for i,page in enumerate(doc):
                u=unit(str(i),page.get_text());u['tab']=f'第 {i+1} 页';us.append(u)
            doc.close()
            result=build_layout(path,us)
            removals=[e for e in result['edits'] if e['reason']=='proven_pdf_page_number']
            self.assertEqual(len(removals),2)
            for u in us:
                projected=project(span(u),u,result)
                self.assertIn('line 1:',projected);self.assertIn('line 6:',projected)
            bad=copy.deepcopy(us);bad[0]['body']+='unexpected content';bad[0]['revision']=digest(bad[0]['body'])
            self.assertFalse([e for e in build_layout(path,bad)['edits'] if e['unit_id']=='0'])


if __name__=='__main__':unittest.main()
