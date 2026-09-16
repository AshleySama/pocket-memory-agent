from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from pocket_memory.embedding import EmbeddingService
from pocket_memory.rag.pipeline import RagPipeline
from pocket_memory.storage import NoteStore


class StubEmbeddingEngine:
    available = True
    unavailable_reason = ""
    dimension = 3

    def embed(self, text: str) -> np.ndarray:
        if "procurement" in text or "vendor" in text or "采购" in text or "供应商" in text:
            return np.asarray([1, 0, 0], dtype=np.float32)
        if "Python" in text or "接口" in text:
            return np.asarray([0, 1, 0], dtype=np.float32)
        return np.asarray([0, 0, 1], dtype=np.float32)


class EmbeddingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / "data")
        self.service = EmbeddingService(self.store, StubEmbeddingEngine())

    def tearDown(self) -> None:
        self.service.shutdown()
        self.store.close()
        self.temp.cleanup()

    def test_semantic_search_finds_related_note_without_exact_words(self) -> None:
        risk = self.store.create_note("外部风险", "需要识别存在风险的合作方")
        tech = self.store.create_note("技术备忘", "Python 接口调用方式")
        self.service._process(risk.id)
        self.service._process(tech.id)

        results = self.service.semantic_search("供应商异常")

        self.assertEqual(results[0][0].id, risk.id)
        self.assertEqual(self.store.embedding_count(), 2)

    def test_hybrid_search_falls_back_to_keyword_results(self) -> None:
        note = self.store.create_note("PR PO GR IR", "采购流程检查")
        self.service._process(note.id)

        results = self.service.hybrid_search("PR PO")

        self.assertEqual([item.id for item in results], [note.id])

    def test_embedding_includes_category_subcategory_and_tags(self) -> None:
        note = self.store.create_note(
            "untitled",
            "short memo",
            category="work",
            subcategory="risk",
            tags=["procurement"],
        )
        self.service._process(note.id)

        results = self.service.hybrid_search("vendor risk")

        self.assertEqual([item.id for item in results], [note.id])

    def test_semantic_search_can_filter_low_scores(self) -> None:
        related = self.store.create_note("procurement", "vendor risk")
        unrelated = self.store.create_note("random", "misc")
        self.service._process(related.id)
        self.service._process(unrelated.id)

        results = self.service.semantic_search("vendor", min_score=0.5)

        self.assertEqual([note.id for note, _ in results], [related.id])

    def test_index_and_query_paths_use_the_same_note_splitter(self) -> None:
        text = (
            "编号：21；Agent名称：需求污染检测Agent；核心功能：识别需求偏离；适用场景：产品研发。\n"
            "编号：26；Agent名称：AI影子团队Agent；核心功能：创建数字孪生团队；适用场景：大型项目。"
        ) * 3

        self.assertEqual(
            EmbeddingService._split_text(text),
            RagPipeline.split_text(text),
        )

    def test_note_chunk_uses_a_leading_section_title_for_display(self) -> None:
        title = EmbeddingService._chunk_title("李白诗词", "《望庐山瀑布》李白\n飞流直下三千尺。")

        self.assertEqual(title, "《望庐山瀑布》李白")


if __name__ == "__main__":
    unittest.main()
