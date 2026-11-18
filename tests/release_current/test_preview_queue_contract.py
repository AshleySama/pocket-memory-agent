# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Request queue accounting and saved-source preview provenance, no model."""
from pathlib import Path
import copy
import hashlib
import sys
import threading
import time
import types
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pocket_memory.rag.performance import capture, snapshot, started_at
from pocket_memory.rag.source_preview import source_preview
from pocket_memory.request_cancel import CancellationRegistry, RequestCancelled, request_scope, cancellable_lock
from pocket_memory.server import PocketMemoryHandler


class PreviewQueueContract(unittest.TestCase):
    def test_preview_keeps_exact_tab_revision_and_ranges_without_mutation(self):
        body = '风险一：未确认；负责人：演示人。\n风险二：已确认；负责人：演示人。'
        start = body.index('风险二')
        span = dict(source_id=12, parent_note_id=7, tab_name='风险', field='content',
                    source_revision=hashlib.sha256(body.encode()).hexdigest(),
                    char_start=start, char_end_exclusive=len(body), quote=body[start:])
        member = dict(id='original:12:1', verification='exact_saved_original', source_type='note',
                      source_id=12, parent_note_id=7, tab_name='风险', spans=[span])
        sources = [dict(title='复盘', citation_members=[member])]
        old = copy.deepcopy(sources)
        server = types.SimpleNamespace(preview_epoch='test',
             store=types.SimpleNamespace(source_revision=lambda: (1, 2, 3)),
             rag_pipeline=types.SimpleNamespace(local=types.SimpleNamespace(revision=(1, 2, 3))))
        item = source_preview(server, sources)['items'][0]
        self.assertEqual(item['url'], '/api/notes/7?tab_id=12')
        self.assertEqual(item['display_spans'], [span])
        self.assertEqual(item['display_evidence'], body[start:])
        self.assertEqual(item['source_type'], 'note')
        self.assertEqual(sources, old)
        item['display_spans'][0]['char_start'] = 0
        self.assertEqual(sources, old)
        server.rag_pipeline.local.revision = (1, 2, 4)
        self.assertIsNone(source_preview(server, sources))

    def test_queued_time_is_in_total_but_not_processing(self):
        lock = threading.Lock()
        lock.acquire()
        timer = threading.Timer(0.08, lock.release)
        timer.start()
        ticket = CancellationRegistry().begin(str(uuid.uuid4()))
        with capture(), request_scope(ticket):
            began = started_at()
            with cancellable_lock(lock):
                time.sleep(0.015)
                timing = PocketMemoryHandler._ask_timing(began, retrieval_seconds=0.01)
            self.assertGreaterEqual(timing['queue_seconds'], 0.05)
            self.assertGreaterEqual(timing['total_seconds'], timing['queue_seconds'])
            self.assertLess(timing['processing_seconds'], timing['queue_seconds'])
            self.assertAlmostEqual(timing['total_seconds'], timing['queue_seconds'] + timing['processing_seconds'], places=2)
        timer.join()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_cancelled_queue_is_measured_and_never_enters_body(self):
        lock = threading.Lock()
        lock.acquire()
        ticket = CancellationRegistry().begin(str(uuid.uuid4()))
        timer = threading.Timer(0.06, ticket.cancelled.set)
        timer.start()
        entered = False
        with capture(), request_scope(ticket):
            with self.assertRaises(RequestCancelled):
                with cancellable_lock(lock):
                    entered = True
            self.assertGreater(snapshot()['stages']['request.lock_wait']['inclusive_seconds'], 0.04)
        self.assertFalse(entered)
        self.assertTrue(lock.locked())
        lock.release()
        timer.join()

    def test_without_capture_and_unqueued_lock_remain_usable(self):
        lock = threading.Lock()
        with cancellable_lock(lock):
            self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())
        timing = PocketMemoryHandler._ask_timing(started_at(), retrieval_seconds=0)
        self.assertGreaterEqual(timing['total_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
