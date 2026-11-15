# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import copy
from types import SimpleNamespace
import unittest

from pocket_memory.rag.citation_display import display_claims, presentation
from pocket_memory.rag.retained.registry import EvidenceRegistry


class CitationFocusBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.sentences = [
            '条件一：离线设备需要先安装本地模型。',
            '条件二：文件完成索引后，可以查询保存的资料。',
            '条件三：资料变更后，引用必须对应当前版本。',
        ]
        self.body = '背景介绍。\n\n' + '\n\n'.join(self.sentences)
        self.note = SimpleNamespace(id=7, parent_note_id=3, tab_name='验证条件', content=self.body)
        self.store = SimpleNamespace(get_note=lambda _: self.note)
        registry = EvidenceRegistry({7: vars(self.note)}, {})
        self.sources = []
        for i, sentence in enumerate(self.sentences, 1):
            start = self.body.index(sentence)
            self.sources.append(registry.decorate(dict(
                index=i, source_type='note', source_id=7, source_key='note:7',
                kind='content', title='验证条件', evidence=sentence, excerpt=sentence,
                raw_evidence=[dict(field='content', char_start=start,
                    char_end_exclusive=start+len(sentence), quote=sentence)])))

    def render(self, answer):
        return [presentation(s, answer, store=self.store) for s in self.sources]

    def assert_all_focused(self, answer):
        before = copy.deepcopy(self.sources)
        results = self.render(answer)
        self.assertEqual([s['display_evidence_count'] for s in results], [1, 1, 1])
        self.assertEqual([s['display_evidence'] for s in results], self.sentences)
        self.assertEqual(before, self.sources)
        for result in results:
            self.assertFalse(result['display_partial_focus'])
            for span in result['display_spans']:
                self.assertEqual(span['source_id'], 7)
                self.assertEqual(span['parent_note_id'], 3)
                self.assertEqual(span['tab_name'], '验证条件')
                self.assertEqual(self.body[span['char_start']:span['char_end_exclusive']], span['quote'])

    def test_footer_does_not_erase_verified_sentences(self):
        answer = '\n'.join(s[:-1]+f'[{i}]。' for i,s in enumerate(self.sentences,1))
        for footer in ('支持资料编号：[1][2][3]', '**支持资料编号：** [1]，[2]，[3]'):
            with self.subTest(footer=footer):
                self.assert_all_focused(answer+'\n'+footer)

    def test_after_period_citations_and_list_decoration(self):
        for template in ('{i}、{s}[{i}]', '{i}. {s}[{i}]', '（{i}）{s}[{i}]', '**{i}. {s}**[{i}]'):
            with self.subTest(template=template):
                self.assert_all_focused('\n'.join(template.format(i=i,s=s) for i,s in enumerate(self.sentences,1)))

    def test_local_citations_are_not_cross_attributed(self):
        answer = '，'.join(s[:-1]+f'[{i}]' for i,s in enumerate(self.sentences,1))+'。'
        self.assert_all_focused(answer)
        self.assertEqual([c['citations'] for c in display_claims(answer)], [[1],[2],[3]])

    def test_citation_groups_attach_to_the_same_claim(self):
        claims=display_claims('文件必须完成索引。[1][2]\n材料必须先确认[2,3]。')
        self.assertEqual(claims, [dict(text='文件必须完成索引',citations=[1,2]),
                                  dict(text='材料必须先确认',citations=[2,3])])

    def test_partial_focus_preserves_context_and_marks_missing_claim(self):
        answer=self.sentences[0]+'[1]。本项目预算为九万元[1]。'
        result=self.render(answer)[0]
        self.assertEqual(result['display_evidence'],self.sentences[0])
        self.assertEqual(result['display_context'],self.sources[0]['evidence'])
        self.assertTrue(result['display_partial_focus'])

    def test_unassigned_claim_is_not_borrowed_and_unsupported_fact_stays_unfocused(self):
        self.assertEqual(self.render('离线设备需要先安装本地模型。')[0]['display_spans'],[])
        self.assertEqual(self.render('本项目预算为九万元[1]。')[0]['display_spans'],[])
        self.assertEqual(self.render('离线设备不需要先安装本地模型[1]。')[0]['display_spans'],[])

    def test_stale_revision_even_with_same_quote_does_not_emit_coordinates(self):
        self.note.content += '\n追加的新内容。'
        self.assertEqual(self.render(self.sentences[0]+'[1]')[0]['display_spans'],[])

    def test_wrong_note_tab_or_original_span_fails_closed(self):
        for change in ('source_id','tab_name','quote','offset'):
            with self.subTest(change=change):
                original=copy.deepcopy(self.sources[0])
                member=self.sources[0]['citation_members'][0]
                if change=='source_id': member['source_id']=8
                elif change=='tab_name': member['tab_name']='其他页签'
                elif change=='quote': member['spans'][0]['quote']='伪造原文'
                else: member['spans'][0]['char_start']+=1
                self.assertEqual(self.render(self.sentences[0]+'[1]')[0]['display_spans'],[])
                self.sources[0]=original

    def test_store_failure_does_not_fall_back_to_unchecked_spans(self):
        self.store.get_note=lambda _: None
        self.assertEqual(self.render(self.sentences[0]+'[1]')[0]['display_spans'],[])

    def test_arbitrary_paraphrase_is_not_certified(self):
        result=self.render('离线电脑加载模型后就能完成所有任务[1]。')[0]
        self.assertEqual(result['display_spans'],[])


if __name__ == '__main__':
    unittest.main()
