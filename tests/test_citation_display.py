# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import copy
import hashlib
import unittest
from pocket_memory.rag.citation_display import presentation


def source(text, index=1, kind='content', field='content'):
    return dict(index=index,kind=kind,evidence=text,excerpt=text,
        citation_members=[dict(verification='exact_saved_original',spans=[dict(field=field,
            char_start=10,char_end_exclusive=10+len(text),quote=text,
            source_revision=hashlib.sha256(text.encode()).hexdigest())])])


class CitationDisplayTests(unittest.TestCase):
    def test_venue_focus_keeps_full_sentence_and_original_context(self):
        quote='办公协调员负责会场安排，与材料整理是两项不同的工作。'
        text='培训材料由培训负责人整理。'+quote+'会议邀请在时间确定后发送。'
        s=source(text); before=copy.deepcopy(s)
        result=presentation(s,'办公协调员负责安排会场[1]。')
        self.assertEqual(result['display_evidence'],quote)
        self.assertEqual(result['display_context'],text)
        self.assertEqual(s,before)
        span=result['display_spans'][0]
        self.assertEqual(text[span['char_start']-10:span['char_end_exclusive']-10],quote)

    def test_all_cited_claims_remain_visible(self):
        s=source('培训材料由培训负责人整理。办公协调员负责会场安排。背景是内部交流。')
        out=presentation(s,'培训材料由培训负责人整理[1]。办公协调员负责会场安排[1]。')
        self.assertEqual(out['display_evidence_count'],2)
        self.assertNotIn('背景',out['display_evidence'])

    def test_no_matching_claim_does_not_highlight_entire_context(self):
        out=presentation(source('培训材料由培训负责人整理。'),'项目负责人负责会场安排[1]。')
        self.assertEqual(out['display_evidence'],'')
        self.assertEqual(out['display_evidence_kind'],'context_only')

    def test_partial_matching_claim_preserves_context_without_false_focus(self):
        out=presentation(source('培训材料由培训负责人整理。'),'培训材料由培训负责人整理[1]。项目预算为九万元[1]。')
        self.assertEqual(out['display_evidence'],'')

    def test_do_not_borrow_from_another_citation(self):
        out=presentation(source('培训材料由培训负责人整理。',2),'培训材料由培训负责人整理[1]。')
        self.assertEqual(out['display_evidence'],'')

    def test_structure_and_complete_range_are_not_trimmed(self):
        for s in (source('表头及完整表格',kind='table_row'),source('type string',kind='schema_view'),
                  dict(source('全部十行缺项证明'),coverage_complete=True)):
            self.assertEqual(presentation(s,'资料未包含该字段[1]。'),{})
        self.assertEqual(presentation(source('完整清单。'),'完整清单[1]。',direct=True),{})

    def test_unverified_and_ocr_spans_are_not_guessed(self):
        s=source('办公协调员负责会场安排。');s['citation_members'][0]['verification']='supplied_fragment_only'
        self.assertEqual(presentation(s,'办公协调员负责会场安排[1]。')['display_evidence'],'')
        self.assertEqual(presentation(source('办公协调员负责会场安排。',field='ocr_text'),'办公协调员负责会场安排[1]。')['display_evidence'],'')

    def test_condition_in_sentence_is_not_removed(self):
        quote='材料确认后，办公协调员负责会场安排；未经确认不能开始。'
        self.assertEqual(presentation(source(quote),'办公协调员负责会场安排[1]。')['display_evidence'],quote)

    def test_negative_or_numeric_mismatch_does_not_focus(self):
        self.assertEqual(presentation(source('办公协调员负责会场安排。'),'办公协调员不负责会场安排[1]。')['display_evidence'],'')
        self.assertEqual(presentation(source('本项目预计预算为12万元。'),'本项目预计预算为21万元[1]。')['display_evidence'],'')

if __name__=='__main__': unittest.main()
