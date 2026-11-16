# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from pocket_memory import intelligence
from pocket_memory.intelligence import IntelligenceService
from pocket_memory.storage import NoteStore


class ManualExecutor:
    def __init__(self, **_kwargs):
        self.tasks = []
        self.closed = False

    def submit(self, function, *args):
        if self.closed:
            raise RuntimeError("executor is closed")
        future = Future()
        self.tasks.append((future, function, args))
        return future

    def run_next(self):
        future, function, args = self.tasks.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function(*args))
            except BaseException as exc:
                future.set_exception(exc)
                raise

    def shutdown(self, *, wait=False, cancel_futures=False):
        self.closed = True
        if cancel_futures:
            for future, _, _ in self.tasks:
                future.cancel()
            self.tasks.clear()


class ManualTimer:
    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.cancelled = False
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self, *, already_started=False):
        if not self.cancelled or already_started:
            self.function(*self.args)


class RecordingEngine:
    available = True
    unavailable_reason = ""

    def __init__(self):
        self.calls = []
        self.during_classify = None

    def classify(self, text, existing_tags=None, excluded_tags=None):
        self.calls.append(text)
        if self.during_classify is not None:
            callback, self.during_classify = self.during_classify, None
            callback()
        return {"category": "工作", "subcategory": "项目", "tags": ["项目进展"]}


class IntelligenceAutosaveTests(unittest.TestCase):
    """Exercise scheduling and source guards without waits or model execution."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / "data")
        self.now = 0.0
        self.timers = []
        self.services = []
        self.executor_patch = patch.object(intelligence, "ThreadPoolExecutor", ManualExecutor)
        self.timer_patch = patch.object(intelligence.threading, "Timer", self.make_timer)
        self.clock_patch = patch.object(intelligence.time, "monotonic", lambda: self.now)
        for patcher in (self.executor_patch, self.timer_patch, self.clock_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = RecordingEngine()
        self.on_complete = Mock()
        self.service = self.make_service()
        self.note = self.store.create_note("项目资料", "初始正文")
        self.tab = self.store.create_note(
            "补充资料", "初始补充", parent_note_id=self.note.id, tab_name="补充",
        )

    def tearDown(self):
        for service in self.services:
            service.shutdown()
        self.store.close()
        self.temp.cleanup()

    def make_timer(self, interval, function, args=()):
        timer = ManualTimer(interval, function, args)
        self.timers.append(timer)
        return timer

    def make_service(self):
        service = IntelligenceService(self.store, self.engine, on_complete=self.on_complete)
        self.services.append(service)
        return service

    def test_group_edits_restart_quiet_period_and_use_latest_text(self):
        self.service.enqueue(self.note.id, delay=15)
        first_timer = self.timers[-1]
        self.now = 5
        self.store.update_note(self.tab.id, content="最新补充资料")
        self.service.enqueue(self.tab.id, delay=15)
        self.assertTrue(first_timer.cancelled)
        self.assertEqual(self.timers[-1].interval, 15)

        # Even a cancellation racing an already-entered callback cannot run early.
        self.now = 15
        first_timer.fire(already_started=True)
        self.assertEqual(len(self.service.executor.tasks), 0)
        self.now = 20
        self.timers[-1].fire()
        self.service.executor.run_next()

        self.assertEqual(len(self.engine.calls), 1)
        self.assertIn("最新补充资料", self.engine.calls[0])
        self.assertIn("初始正文", self.engine.calls[0])
        self.on_complete.assert_called_once_with(self.tab.id)
        self.assertEqual(self.service._autosave_jobs, {})

    def test_edit_while_queued_waits_for_new_quiet_period(self):
        self.service.enqueue(self.note.id, delay=15)
        self.now = 15
        self.timers[-1].fire()
        self.now = 16
        self.store.update_note(self.note.id, content="队列等待期间的新内容")
        self.service.enqueue(self.note.id, delay=15)
        self.assertEqual(len(self.service.executor.tasks), 1)
        self.service.executor.run_next()
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.timers[-1].interval, 15)

        self.now = 31
        self.timers[-1].fire()
        self.service.executor.run_next()
        self.assertEqual(len(self.engine.calls), 1)
        self.assertIn("队列等待期间的新内容", self.engine.calls[0])

    def test_edits_during_inference_reject_old_result_and_run_only_latest(self):
        def edit_during_inference():
            self.now = 16
            self.store.update_note(self.note.id, content="中间版本")
            self.service.enqueue(self.note.id, delay=15)
            self.now = 17
            self.store.update_note(self.tab.id, content="最终补充正文")
            self.service.enqueue(self.tab.id, delay=15)

        self.engine.during_classify = edit_during_inference
        self.service.enqueue(self.note.id, delay=15)
        self.now = 15
        self.timers[-1].fire()
        self.service.executor.run_next()

        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(len(self.service.executor.tasks), 0)
        self.on_complete.assert_not_called()
        self.assertNotIn("项目进展", self.store.get_note(self.note.id).tags)
        self.assertEqual(self.store.get_note(self.tab.id).ai_status, "pending")
        self.assertEqual(self.timers[-1].interval, 15)

        self.now = 32
        self.timers[-1].fire()
        self.service.executor.run_next()
        self.assertEqual(len(self.engine.calls), 2)
        self.assertIn("最终补充正文", self.engine.calls[-1])
        self.assertIn("中间版本", self.engine.calls[-1])
        self.on_complete.assert_called_once_with(self.tab.id)
        self.assertEqual(self.service._autosave_jobs, {})

    def test_explicit_requests_keep_independent_immediate_execution(self):
        self.service.enqueue(self.note.id, apply=True)
        self.service.enqueue(self.note.id, apply=True)
        self.assertEqual(len(self.service.executor.tasks), 2)
        self.assertEqual(self.timers, [])
        self.service.executor.run_next()
        self.service.executor.run_next()
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.on_complete.call_count, 2)

    def test_sibling_edit_also_rejects_stale_explicit_result_without_retry(self):
        self.engine.during_classify = lambda: self.store.update_note(
            self.tab.id, content="推理期间发生的页签修改",
        )
        self.service.enqueue(self.note.id, apply=True)
        self.service.executor.run_next()
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.service.executor.tasks, [])
        self.assertEqual(self.timers, [])
        self.on_complete.assert_not_called()
        self.assertEqual(self.store.get_note(self.note.id).ai_status, "pending")
        self.assertNotIn("项目进展", self.store.get_note(self.note.id).tags)

    def test_shutdown_cancels_delayed_work_and_keeps_it_recoverable(self):
        # Only the requested note should be recovered in this scenario.
        self.store.update_ai_status(self.tab.id, "done")
        self.service.enqueue(self.note.id, delay=15)
        timer = self.timers[-1]
        self.service.shutdown()
        self.assertTrue(timer.cancelled)
        self.now = 15
        timer.fire(already_started=True)
        self.assertEqual(self.service.executor.tasks, [])
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.get_note(self.note.id).ai_status, "pending")

        recovered = self.make_service()
        recovered.recover()
        self.assertEqual(len(recovered.executor.tasks), 1)
        recovered.executor.run_next()
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.store.get_note(self.note.id).ai_status, "done")

    def test_library_change_invalidates_old_timers_and_queued_autosaves(self):
        other = self.store.create_note("另一笔记", "原资料库内容")
        self.service.enqueue(self.note.id, delay=15)
        queued_timer = self.timers[-1]
        self.service.enqueue(other.id, delay=15)
        waiting_timer = self.timers[-1]
        self.now = 15
        queued_timer.fire()
        self.assertEqual(len(self.service.executor.tasks), 1)

        self.service.cancel_pending_autosaves()
        self.assertTrue(waiting_timer.cancelled)
        self.assertEqual(self.store.get_note(self.note.id).ai_status, "pending")
        self.assertEqual(self.store.get_note(other.id).ai_status, "pending")

        with tempfile.TemporaryDirectory() as alternate:
            new_store = NoteStore(Path(alternate) / "data")
            try:
                replacement = new_store.create_note("新资料库笔记", "内容不可被旧任务触碰")
                new_store.create_note("新页签", "新内容", parent_note_id=replacement.id)
                replacement_other = new_store.create_note("新资料库另一笔记", "新内容")
                self.assertEqual(replacement.id, self.note.id)
                self.assertEqual(replacement_other.id, other.id)
                new_store.update_ai_status(replacement.id, "done")
                new_store.update_ai_status(replacement_other.id, "done")
                before = (new_store.get_note(replacement.id), new_store.get_note(replacement_other.id))
                self.service.store = new_store

                waiting_timer.fire(already_started=True)
                self.service.executor.run_next()

                self.assertEqual(self.engine.calls, [])
                self.on_complete.assert_not_called()
                self.assertEqual(self.service.executor.tasks, [])
                self.assertEqual(
                    (new_store.get_note(replacement.id), new_store.get_note(replacement_other.id)), before,
                )
            finally:
                self.service.store = self.store
                new_store.close()


if __name__ == "__main__":
    unittest.main()
