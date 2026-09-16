from __future__ import annotations

import base64
import tempfile
import time
import unittest
from pathlib import Path

from pocket_memory.ocr import OcrService
from pocket_memory.storage import NoteStore


class StubOcrEngine:
    available = True
    unavailable_reason = ""

    def recognize(self, image_path: Path) -> str:
        self.last_path = image_path
        return "供应商异常\n采购风险监控"


class OcrServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / "data")
        self.engine = StubOcrEngine()
        self.service = OcrService(self.store, self.engine)

    def tearDown(self) -> None:
        self.service.shutdown()
        self.store.close()
        self.temp.cleanup()

    def test_ocr_text_is_written_to_markdown_and_searchable(self) -> None:
        image = base64.b64encode(b"fake-png").decode("ascii")
        note = self.store.create_note("截图", "采购截图", f"data:image/png;base64,{image}", "clipboard.png")

        self.service.enqueue(note.id)
        for _ in range(200):
            note = self.store.get_note(note.id)
            if note.ocr_status == "done":
                break
            time.sleep(0.01)

        self.assertEqual(note.ocr_status, "done", note.ocr_error)
        self.assertIn("供应商异常", note.ocr_text)
        self.assertEqual([item.id for item in self.store.search_notes("风险监控")], [note.id])
        markdown = (self.store.data_dir / note.markdown_path).read_text(encoding="utf-8")
        self.assertIn("## OCR 识别文本", markdown)
        self.assertIn("采购风险监控", markdown)


if __name__ == "__main__":
    unittest.main()
