# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from pocket_memory.embedding import EmbeddingService
from pocket_memory.storage import NoteStore


class ControlledEmbeddingEngine:
    """A local deterministic stub with an explicit, bounded inference barrier."""

    available = True
    unavailable_reason = ""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.block_next = False
        self.fail = False
        self.inputs: list[str] = []

    def block_once(self) -> None:
        self.started.clear()
        self.release.clear()
        self.block_next = True

    def embed(self, text: str) -> np.ndarray:
        self.inputs.append(text)
        if self.block_next:
            self.block_next = False
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("test embedding barrier was not released")
        if self.fail:
            raise RuntimeError("controlled embedding failure")
        return np.asarray([float(len(text)), 1.0], dtype=np.float32)


class EmbeddingLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = NoteStore(self.root / "library")
        self.engine = ControlledEmbeddingEngine()
        self.service = EmbeddingService(self.store, self.engine)

    def tearDown(self) -> None:
        self.engine.release.set()
        self.service.shutdown(wait=True)
        self.store.close()
        self.temp.cleanup()

    def index_note(self, note_id: int) -> None:
        self.service.enqueue(note_id)
        self.assertTrue(self.service.drain(timeout=3))

    def start_blocked_note(self, note_id: int) -> None:
        self.engine.block_once()
        self.service.enqueue(note_id)
        self.assertTrue(self.engine.started.wait(3), "embedding worker did not start")

    def test_source_revision_detects_same_second_writes_and_reconnects(self) -> None:
        with patch.object(NoteStore, "_now", return_value="2026-10-28T10:00:00+08:00"):
            note = self.store.create_note("Record", "original")
            before = self.store.source_revision()
            changed = self.store.update_note(note.id, content="changed")
            after = self.store.source_revision()
        self.assertEqual(note.updated_at, changed.updated_at)
        self.assertNotEqual(before, after)

        original_library = self.store.data_dir
        self.store.switch_data_dir(self.root / "another-library")
        switched = self.store.source_revision()
        self.assertNotEqual(after[0], switched[0])
        self.store.switch_data_dir(original_library)
        self.assertNotEqual(switched[0], self.store.source_revision()[0])
        self.assertNotEqual(after[0], self.store.source_revision()[0])

    def test_source_revision_detects_external_connection_commits(self) -> None:
        note = self.store.create_note("Record", "original")
        before = self.store.source_revision()
        external = sqlite3.connect(self.store.db_path)
        try:
            with external:
                external.execute("UPDATE notes SET content = ? WHERE id = ?", ("external edit", note.id))
        finally:
            external.close()
        after = self.store.source_revision()
        self.assertEqual(before[:2], after[:2])
        self.assertNotEqual(before[2], after[2])

    def test_same_second_edit_during_embedding_cannot_publish_old_source(self) -> None:
        with patch.object(NoteStore, "_now", return_value="2026-10-28T10:00:00+08:00"):
            note = self.store.create_note("Record", "original content", tags=["old-tag"])
            self.index_note(note.id)
            old_chunks = self.store.list_note_chunks(note.id)
            self.start_blocked_note(note.id)
            try:
                changed = self.store.update_note(note.id, content="current content", tags=["new-tag"])
                self.assertEqual(note.updated_at, changed.updated_at)
            finally:
                self.engine.release.set()
            self.assertTrue(self.service.drain(timeout=3))
            self.assertEqual(old_chunks, self.store.list_note_chunks(note.id))
            self.assertFalse(self.service.index_ready())

            self.index_note(note.id)
        self.assertTrue(self.service.index_ready())
        self.assertEqual([], self.service.index_errors())
        self.assertEqual("current content", self.store.list_note_chunks(note.id)[0].text)
        self.assertTrue(any("new-tag" in text for text in self.engine.inputs))

    def test_deletion_during_embedding_cannot_recreate_chunks(self) -> None:
        note = self.store.create_note("Record", "delete me")
        self.index_note(note.id)
        self.start_blocked_note(note.id)
        try:
            self.store.delete_note(note.id)
        finally:
            self.engine.release.set()
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual([], self.store.list_note_chunks(note.id))
        self.assertEqual(0, self.store.embedding_count())
        self.assertEqual([], self.service.index_errors())

    def test_running_and_queued_jobs_cannot_write_same_id_in_new_library(self) -> None:
        note = self.store.create_note("Old library", "old-library content")
        self.index_note(note.id)
        other_dir = self.root / "another-library"
        other = NoteStore(other_dir)
        try:
            replacement = other.create_note("New library", "new-library content")
            self.assertEqual(note.id, replacement.id)
        finally:
            other.close()

        self.start_blocked_note(note.id)
        self.service.enqueue(note.id)
        try:
            self.store.switch_data_dir(other_dir)
        finally:
            self.engine.release.set()
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual([], self.store.list_note_chunks(replacement.id))
        self.assertEqual(0, self.store.embedding_count())
        self.assertEqual([], self.service.index_errors())
        self.index_note(replacement.id)
        self.assertEqual("new-library content", self.store.list_note_chunks(replacement.id)[0].text)

    def test_drained_failed_note_index_is_not_ready_and_success_recovers(self) -> None:
        note = self.store.create_note("Record", "previous content")
        self.index_note(note.id)
        old_chunks = self.store.list_note_chunks(note.id)
        self.store.update_note(note.id, content="replacement content")
        self.engine.fail = True
        self.index_note(note.id)

        self.assertEqual(old_chunks, self.store.list_note_chunks(note.id))
        self.assertFalse(self.service.index_ready())
        errors = self.service.index_errors()
        self.assertEqual([("note", note.id)], [(item["source_kind"], item["source_id"]) for item in errors])
        self.assertIn("controlled embedding failure", errors[0]["error"])
        self.assertFalse(self.service.index_ready())

        self.engine.fail = False
        self.index_note(note.id)
        self.assertTrue(self.service.index_ready())
        self.assertEqual([], self.service.index_errors())
        self.assertEqual("replacement content", self.store.list_note_chunks(note.id)[0].text)

    def test_deleted_and_switched_library_failures_are_cleared(self) -> None:
        self.engine.fail = True
        note = self.store.create_note("Record", "will fail")
        self.index_note(note.id)
        self.assertTrue(self.service.index_errors())
        self.store.delete_note(note.id)
        self.assertEqual([], self.service.index_errors())

        another = self.store.create_note("Second record", "will also fail")
        self.index_note(another.id)
        self.assertTrue(self.service.index_errors())
        self.store.switch_data_dir(self.root / "fresh-library")
        self.assertEqual([], self.service.index_errors())
        self.assertTrue(self.service.index_ready())

    def test_document_failure_is_not_ready_and_success_recovers(self) -> None:
        data_url = "data:text/plain;base64," + base64.b64encode(b"document content").decode("ascii")
        document = self.store.create_document_from_data_url("record.txt", data_url)
        self.store.replace_document_chunks(document.id, [{"text": "document content", "location": "正文"}])
        self.engine.fail = True
        self.service.enqueue_document(document.id)
        self.assertTrue(self.service.drain(timeout=3))
        self.assertFalse(self.service.index_ready())
        self.assertEqual("failed", self.store.get_document(document.id).status)
        self.assertEqual("document", self.service.index_errors()[0]["source_kind"])

        self.engine.fail = False
        self.service.enqueue_document(document.id)
        self.assertTrue(self.service.drain(timeout=3))
        self.assertTrue(self.service.index_ready())
        self.assertEqual("ready", self.store.get_document(document.id).status)
        self.assertEqual([], self.service.index_errors())

    def test_drain_timeout_keeps_work_pending_until_completion(self) -> None:
        note = self.store.create_note("Record", "pending content")
        self.start_blocked_note(note.id)
        self.assertFalse(self.service.drain(timeout=0.01))
        self.assertFalse(self.service.index_ready())
        self.engine.release.set()
        self.assertTrue(self.service.drain(timeout=3))
        self.assertTrue(self.service.index_ready())
        self.assertTrue(self.store.list_note_chunks(note.id))

    def test_repeated_rebuild_preserves_chunk_ids_and_exact_model_inputs(self) -> None:
        note = self.store.create_note("Record", "first section\n\nsecond section")
        self.service.rebuild()
        self.assertTrue(self.service.drain(timeout=3))
        first_chunks = self.store.list_note_chunks(note.id)
        first_vectors = self.store.list_note_chunk_embeddings()
        first_inputs = list(self.engine.inputs)
        before_rebuild = self.store.source_revision()

        self.engine.inputs.clear()
        self.service.rebuild()
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual(first_chunks, self.store.list_note_chunks(note.id))
        self.assertEqual(first_inputs, self.engine.inputs)
        second_vectors = self.store.list_note_chunk_embeddings()
        self.assertEqual([item[0] for item in first_vectors], [item[0] for item in second_vectors])
        for (_, before), (_, after) in zip(first_vectors, second_vectors):
            np.testing.assert_array_equal(before, after)
        self.assertNotEqual(before_rebuild, self.store.source_revision())


if __name__ == "__main__":
    unittest.main()
