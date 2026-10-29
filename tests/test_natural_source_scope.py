# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import unittest
from types import SimpleNamespace
from pocket_memory.rag.retained.source_scope import natural_source_question, ScopedRetrieval


class NaturalSourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.notes = {
            1: dict(id=1, title='发布复盘', parent_note_id=None, content='背景。\n风险一：输入过杂。\n风险二：文件难读。\n风险三：首次加载慢。'),
            2: dict(id=2, title='交付手册', parent_note_id=None, content='说明'),
            3: dict(id=3, title='页签2', tab_name='页签2', parent_note_id=2, content='操作步骤'),
            4: dict(id=4, title='知识图谱', parent_note_id=None, content='应用场景说明。'),
        }

    def test_explicit_phrases_and_narrow_bare_list(self):
        for q in ('发布复盘项目有哪些风险', '发布复盘里提到的风险是什么', '请问发布复盘有哪些风险？',
                  '请根据发布复盘说明风险', '发布复盘中只列文件相关风险，不要加载风险'):
            canonical, trace = natural_source_question(q, self.notes)
            self.assertTrue(canonical.startswith('《发布复盘》'), q)
            self.assertEqual(trace['source_id'], 1)
        self.assertIn('不要加载风险', natural_source_question('发布复盘中只列文件相关风险，不要加载风险',self.notes)[0])

    def test_existing_quotes_and_global_topics_unchanged(self):
        for q in ('根据《发布复盘》列风险', '知识图谱有哪些应用', '项目有哪些风险', '别根据发布复盘回答',
                  '发布复盘和交付手册有哪些差异', '发布复盘里结合其他资料说风险'):
            self.assertEqual(natural_source_question(q, self.notes), (q, None))

    def test_duplicate_titles_and_misspellings_never_guessed(self):
        self.notes[5] = dict(self.notes[1],id=5)
        q = '发布复盘里提到的风险是什么'
        self.assertEqual(natural_source_question(q, self.notes), (q,None))
        del self.notes[5]
        q = '布复盘有哪些风险'
        self.assertEqual(natural_source_question(q, self.notes), (q,None))
        q = '发布复盘里有哪些风险'
        self.assertEqual(natural_source_question(q,self.notes,{9:dict(title='发布复盘')}),(q,None))

    def test_longest_title_does_not_fall_back_to_short_title(self):
        self.notes[5] = dict(id=5,title='发布复盘项目',parent_note_id=None,content='没有枚举。')
        q = '发布复盘项目有哪些风险'
        self.assertEqual(natural_source_question(q,self.notes),(q,None))

    def test_scope_normalization_reaches_plain_fallback(self):
        seen = []
        owner = SimpleNamespace(notes=self.notes,nodes=[])
        def prior(q):
            seen.append(q)
            return [],[dict(quoted_title='发布复盘',matched_sources=['note:1'])]
        wrapped = ScopedRetrieval(owner,prior)
        _, scope=wrapped('发布复盘项目有哪些风险')
        self.assertEqual(seen,['《发布复盘》项目有哪些风险'])
        self.assertEqual(scope[0]['source_expression']['source_id'],1)

    def test_tab_and_missing_tab_use_existing_resolver(self):
        owner = SimpleNamespace(notes=self.notes,nodes=[SimpleNamespace(metadata={'source_key':f'note:{i}'}) for i in self.notes])
        wrapped = ScopedRetrieval(owner, lambda q: self.fail('Member expression must use the existing resolver'))
        nodes,scope = wrapped('根据交付手册页签2说明步骤')
        self.assertEqual([n.metadata['source_key'] for n in nodes],['note:3'])
        nodes,scope = wrapped('根据交付手册页签9说明步骤')
        self.assertEqual(nodes,[])
        self.assertEqual(scope[0]['resolution'],'missing')

    def test_long_bare_title_topic_does_not_activate_short_list_rule(self):
        self.notes[1]['content'] += '\n背景'*300
        q='发布复盘有哪些风险'
        self.assertEqual(natural_source_question(q,self.notes),(q,None))


if __name__ == '__main__':unittest.main()
