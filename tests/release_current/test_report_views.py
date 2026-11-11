# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import copy
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import test_report_workspace as old
from pocket_memory.report_workspace import render_report,export_html
from pocket_memory.report_sections import groups,flow_groups,readable,quote_span,focus_view

class ReportViews(old.WorkspaceTests):
    def test_comparison_excludes_user_advice_from_options(self):
        records=[{'field':'content','ref':1,'title':'说明','text':'用户可以先查原文。这套设计尤其适合记录。'},
                 {'field':'content','ref':2,'title':'对比','text':'方案甲擅长找字词；方案乙擅长找意思；方案丙用于组织答案。'}]
        _,body=focus_view({'template':'compare','records':records},str,str,lambda *x:'')
        self.assertNotIn('<h3>用户</h3>',body)
        self.assertEqual(body.count('<article class="panel">'),3)
    def test_new_report_summary_does_not_publish_unproven_paraphrase(self):
        r=self.build('learning')
        r['claims'][0]['text']='本系统已实现自动审批并保证成功'
        r['claims'][0]['topic']='无需人工审批'
        out=render_report(r)
        self.assertNotIn('本系统已实现自动审批并保证成功',out)
        self.assertNotIn('无需人工审批',out)
        self.assertIn(readable(r['refs'][r['claims'][0]['ref']-1]['quote']),out)
    def test_quote_repair_preserves_original_identity(self):
        block='当前实现可打开 `index.html`，不依赖真实邮件服务。'
        s=quote_span(block,'当前实现可打开 index.html，不依赖真实邮件服务。','这是静态原型')
        self.assertEqual(s[:2],(0,len(block)))
        self.assertIsNone(quote_span(block,'当前实现可打开 index.html，依赖真实邮件服务。','支持实际邮件发送'))
        self.assertIsNone(quote_span('状态是30，金额100。','状态是50，金额100。','状态是50，金额100。'))
        text='全文搜索擅长找完全出现过的字词。'
        self.assertEqual(quote_span(text,'E3',text),(0,len(text),'claim_is_verbatim'))
    def test_nonstatus_table_is_visible_in_every_template(self):
        self.store.connection.execute('UPDATE notes SET content=? WHERE id=?',('| 检查项 | 通过标准 |\n| --- | --- |\n| 资料来源 | 回到原文件 |\n| 人工确认 | 不替代签字 |',self.table.id));self.store.connection.commit()
        for template in ('project','research','compare','proposal','meeting','learning'):
            result=self.build(template);rendered=render_report(result)
            self.assertIn('回到原文件',rendered);self.assertIn('不替代签字',rendered)
            self.assertIn('<table>',rendered);self.assertIn('不替代签字',export_html(result))
    def test_explicit_list_preserves_negative_capability(self):
        records=[{'text':'暂未包含：','ref':1,'title':'原型','field':'content'},
                 {'text':'- 真实邮件发送\n- 数据库存储','ref':2,'title':'原型','field':'content'}]
        result=groups(records)
        self.assertEqual(result[0]['title'],'暂未包含');self.assertEqual(len(result[0]['items']),2)
    def test_direction_requires_explicit_chain(self):
        make=lambda text:{'text':text,'ref':1,'title':'追溯','field':'content'}
        self.assertEqual(flow_groups([make('甲系统和乙系统同时可用。')]),[])
        result=flow_groups([make('a.ID\n -> b.PARENT_ID\n -> c.RELATION_ID')])
        self.assertEqual(result[0]['items'],['a.ID','b.PARENT_ID','c.RELATION_ID'])
    def test_model_content_is_not_a_execution_status(self):
        out=render_report(self.build('meeting'))
        self.assertIn('非会议实录',out);self.assertIn('会议时间：未确认',out)
        self.assertIn('<dt>执行状态</dt><dd>资料未提供',out)
    def test_markup_display_projection_does_not_change_source(self):
        raw='资料<strong>来源</strong>：不能替代签字。'
        self.assertEqual(readable(raw),'资料来源：不能替代签字。')
        self.assertNotIn('alert',readable('<script>alert(1)</script>有效正文'))
        r=self.build('research');before=copy.deepcopy(r['refs']);render_report(r)
        self.assertEqual(r['refs'],before)
    def test_learning_retains_revision_negation(self):
        self.store.connection.execute('UPDATE notes SET content=? WHERE id=?',('原要求：状态只可为30/50。\n\n后续修改：增加开关，关闭后不再过滤；上游资料不受约束。',self.note.id));self.store.connection.commit()
        result=self.build('learning');out=render_report(result);exported=export_html(result)
        # The body no longer repeats complete originals. Both revisions must
        # remain reachable in the source snapshot and the offline appendix.
        self.assertIn('report-source-index',out)
        self.ws.write(result)
        ref=next(ref for ref in result['refs'] if '状态只可为30/50' in ref['quote'])
        self.assertIn(f'data-ref="{ref["id"]}"',out)
        original=self.ws.source(result['id'],ref['id'])['snapshot']
        for text in ('状态只可为30/50','关闭后不再过滤','上游资料不受约束'):
            self.assertIn(text,original);self.assertIn(text,exported)

if __name__=='__main__': unittest.main(verbosity=2)
