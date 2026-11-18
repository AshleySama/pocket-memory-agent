# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
import json
import unittest
from pocket_memory.report_source_frames import source_frames
from pocket_memory.report_grounded_digest import selection_units,ensure_topic_coverage,parse_selection,display_text

class GroundedReportTests(unittest.TestCase):
    def unit(self,text,i=1,**kw):
        return dict(unit_id=f'd1:{i}',body=text,kind='document',source_id=1,revision='r1',tab=f'第{i}页',title='来源',field='document_text',**kw)
    def test_initial_answer_is_not_final_factual_content(self):
        units=[self.unit('为什么选择这个方案？\n普通人\n只需一次初始化，没有任何前提。\n高手\n只有显式登记的对象才能使用该方案。\n面试点评\n这一问题应结合实际项目回答。')]
        frames,_=source_frames(units);e=selection_units(frames)
        self.assertTrue(any('只有显式登记' in f['text'] for f in e))
        self.assertFalse(any('没有任何前提' in f['text'] for f in e))
    def test_cross_page_conditional_pair_is_not_split(self):
        units=[self.unit('完整条件\n高手\n如果已有同名对象，则复用已有实例。'),self.unit('否则，创建一个新实例并登记身份。\n面试点评\n请结合上下文理解。',2)]
        f,_=source_frames(units);e=selection_units(f)
        found=[x for x in e if '否则' in x['text']]
        self.assertEqual(len(found),1);self.assertIn('如果已有同名对象',found[0]['text'])
        self.assertEqual({r['unit_id'] for r in found[0]['ranges']},{'d1:1','d1:2'})
    def test_complete_enumeration_cannot_be_selected_as_one_member(self):
        f,_=source_frames([self.unit('登记方式\n高手\n共有三种方式。\n第一，手工登记必要配置。\n第二，扫描声明的组件。\n第三，导入外部已有定义。\n面试点评\n结束。')]);e=selection_units(f)
        self.assertEqual(len(e),1)
        for word in ('第一','第二','第三'):self.assertIn(word,e[0]['text'])
    def test_model_cannot_write_new_fact_or_change_source_identity(self):
        frames=[dict(id='S1')]
        for obj in ({'selected':['S99']},{'selected':['S1','S1']},{'selected':['新解释']}):
            with self.assertRaises(ValueError):parse_selection(dict(text=json.dumps(obj),finish_reason='stop'),frames)
        with self.assertRaises(ValueError):parse_selection(dict(text='{}',finish_reason='length'),frames)
    def test_topic_and_subject_anchor_survive_selection(self):
        frames=[dict(id='S1',topic_hint='一',parent_frame='F1',anchor=None,text='已保存事实'),dict(id='S2',topic_hint='一',parent_frame='F1',anchor='S1',text='已保存事实'),dict(id='S3',topic_hint='二',parent_frame='F2',anchor=None,text='已保存事实')]
        selected,_=ensure_topic_coverage(frames,['S2'])
        self.assertEqual(selected,['S1','S2','S3'])
    def test_latin_and_cjk_wrapping_and_digits(self):
        self.assertEqual(display_text('发生在\n上下文中。'), '发生在上下文中。')
        self.assertEqual(display_text('Spring\nCloud'), 'Spring Cloud')
        self.assertEqual(display_text('192.168.1.1:8080'), '192.168.1.1:8080')

    def test_each_boundary_survives_not_just_one_per_role(self):
        texts=['方案允许异步同步。','如果不能接受短期差异，则不能采用该方案。',
               '当负载下降时，容量会逐步恢复。','不过该方法不能处理环形依赖。',
               '故障原因有两个：节点失效和同时到期。','只需要统计窗口内的次数。']
        frames=[dict(id=f'S{i}',topic_hint='方案',parent_frame='F1',anchor=None,text=t)
                for i,t in enumerate(texts)]
        selected,_=ensure_topic_coverage(frames,['S0'])
        self.assertEqual(selected,[f'S{i}' for i in range(len(texts))])

    def test_pdf_font_heading_ignores_repeated_narrative_title(self):
        import tempfile,pymupdf
        from pathlib import Path
        from pocket_memory.report_pdf_structure import enrich_pdf_units
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'source.pdf';pdf=pymupdf.open();page=pdf.new_page()
            page.insert_text((40,40),'Main Topic',fontsize=24)
            page.insert_text((40,80),'Discussion of Main Topic in ordinary prose.',fontsize=12)
            page.insert_text((40,110),'A longer paragraph establishes ordinary body typography for this test.',fontsize=12)
            pdf.save(path);pdf.close()
            u=self.unit('Main Topic\nDiscussion of Main Topic in ordinary prose.\nA longer paragraph establishes ordinary body typography for this test.')
            original=u['body'];receipt=enrich_pdf_units(path,[u])
            self.assertEqual(receipt['matched_headings'],1);self.assertEqual(u['structural_headings'][0]['text'],'Main Topic')
            self.assertEqual(u['body'],original);self.assertEqual(u['revision'],'r1')
    def test_pdf_role_and_topic_do_not_mix_following_chapters(self):
        text='Alpha Topic\n高手\n仅在此条件成立时使用甲机制。\nBeta Topic\n高手\n这里的乙机制属于另一个对象。'
        a=text.index('Alpha Topic');b=text.index('Beta Topic')
        u=self.unit(text,pdf_structure={'status':'complete'},structural_headings=[{'start':a,'end':a+11,'text':'Alpha Topic','revision':'r1'},{'start':b,'end':b+10,'text':'Beta Topic','revision':'r1'}])
        f,_=source_frames([u]);e=selection_units(f)
        self.assertTrue(any('乙机制' in x['text'] and x['topic_hint']=='Beta Topic' for x in e))
        self.assertFalse(any('乙机制' in x['text'] and x['topic_hint']=='Alpha Topic' for x in e))

    def test_selecting_definition_alone_restores_subject_specific_mechanism(self):
        frames=[dict(id='S1',topic_hint='QueueAlpha 的原理',parent_frame='F1',anchor=None,text='队列为空时获取者等待，满时写入者等待。'),dict(id='S2',topic_hint='QueueAlpha 的原理',parent_frame='F1',anchor='S1',text='QueueAlpha 基于循环数组保存元素，通过通知机制唤醒等待者。')]
        selected,reasons=ensure_topic_coverage(frames,['S1'])
        self.assertEqual(selected,['S1','S2'])
        self.assertTrue(any(x['reason']=='source_role_mechanism' for x in reasons))
    def test_intro_definition_survives_but_ordinary_answer_does_not(self):
        f,_=source_frames([self.unit('系统说明\nABC全称是Alpha Beta Controller，是一个组件。\n普通人\n它在所有情况下都没有约束。\n高手\n只有已登记的组件可以运行该功能。')]);e=selection_units(f)
        self.assertTrue(any('ABC全称' in x['text'] for x in e))
        self.assertFalse(any('所有情况下' in x['text'] for x in e))

if __name__=='__main__':unittest.main()
