# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import copy
import hashlib
from types import SimpleNamespace
import unittest
from pocket_memory.rag.citation_display import presentation


class DocumentCitationDisplayTests(unittest.TestCase):
    def setUp(self):
        self.text = '前言：欢迎阅读。\nAQS 是多线\n程同步器。\n如果申请失败，则进入等待队列。\n关注作者获取更多资料。'
        self.chunk = SimpleNamespace(id=7, document_id=3, text=self.text)
        self.store = SimpleNamespace(get_document_chunk=lambda _: self.chunk)
        self.source = dict(index=1, source_type='document', source_id=3, kind='document', chunk_id=7,
                           evidence=self.text, excerpt=self.text)

    def test_pdf_visual_wrap_is_not_a_sentence_break(self):
        before = copy.deepcopy(self.source)
        result = presentation(self.source, 'AQS 是多线程同步器[1]。', store=self.store)
        self.assertEqual(result['display_evidence'], 'AQS 是多线\n程同步器。')
        self.assertEqual(result['display_context'], self.text)
        self.assertEqual(before, self.source)
        for s in result['display_document_spans']:
            self.assertEqual(self.text[s['char_start']:s['char_end_exclusive']], s['quote'])
            self.assertEqual(s['source_revision'], hashlib.sha256(self.text.encode()).hexdigest())

    def test_no_focus_means_no_whole_chunk_highlight(self):
        for answer in ('其他资料支持这一点[2]。', '这段文档证明同步器性能最好[1]。'):
            out = presentation(self.source, answer, store=self.store)
            self.assertEqual(out['display_evidence'], '')
            self.assertEqual(out['display_context'], self.text)
            self.assertEqual(out['display_document_spans'], [])

    def test_wrong_identity_or_changed_saved_chunk_never_focuses(self):
        for change in ({'source_id': 4}, {'evidence': self.text+'伪造'}):
            out = presentation({**self.source, **change}, 'AQS 是多线程同步器[1]。', store=self.store)
            self.assertEqual(out['display_document_spans'], [])

    def test_condition_is_kept_and_incomplete_focus_is_not_full_support(self):
        answer = '如果申请失败，则进入等待队列[1]。它保证没有任何死锁[1]。'
        result = presentation(self.source, answer, store=self.store)
        self.assertEqual(result['display_evidence'], '如果申请失败，则进入等待队列。')
        self.assertTrue(result['display_partial_focus'])
        self.assertEqual(result['display_context'], self.text)

    def test_compacted_evidence_maps_back_to_saved_quote(self):
        self.source['evidence'] = 'AQS 是多线 程同步器。'
        out = presentation(self.source, 'AQS 是多线程同步器[1]。', store=self.store)
        self.assertEqual(out['display_evidence'], 'AQS 是多线\n程同步器。')

    def test_long_answer_clause_locates_wrapped_sentence_not_author_footer(self):
        self.chunk.text = '前言。共享锁也称为读锁，就是在同一时刻允许多个线程同时获得锁资源，比如\nCountDownLatch 和Semaphore 都是用到了AQS 中的共享锁功能。\n关注作者获取更多资料。'
        self.source['evidence'] = self.chunk.text
        answer = '共享锁则允许多个线程同时访问资源，例如 CountDownLatch 和 Semaphore 中的共享锁功能[1]。'
        out = presentation(self.source, answer, store=self.store)
        self.assertIn('共享锁也称为读锁', out['display_evidence'])
        self.assertNotIn('关注作者', out['display_evidence'])
        self.assertEqual(out['display_context'], self.chunk.text)

    def test_long_anchor_does_not_override_identifier_or_negative_mismatch(self):
        for answer in ('其他同步器 Q99 用于实现多线程同步器[1]。', 'AQS 不是多线程同步器[1]。'):
            out = presentation(self.source, answer, store=self.store)
            self.assertEqual(out['display_document_spans'], [])

    def test_structured_and_complete_ranges_remain_unchanged(self):
        for extra in ({'coverage_complete': True}, {'table_headers': ['名称', '值']}, {'evidence': '| 名称 | 值 |'}):
            self.assertEqual(presentation({**self.source, **extra}, '答案[1]', store=self.store), {})
