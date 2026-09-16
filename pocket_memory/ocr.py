from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from pocket_memory.storage import NoteStore

logger = logging.getLogger(__name__)


def resource_path(relative_path: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        external = Path(sys.executable).resolve().parent / relative_path
        external_root = Path(sys.executable).resolve().parent / Path(relative_path).parts[0]
        if external.exists() or external_root.exists():
            return external
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parent.parent / relative_path


class RapidOcrEngine:
    def __init__(self, model_dir: Path | None = None) -> None:
        self._engine = None
        self._error: str | None = None
        self.model_dir = (model_dir or resource_path("models/rapidocr")).resolve()

    @property
    def available(self) -> bool:
        self._load()
        return self._engine is not None

    @property
    def unavailable_reason(self) -> str:
        self._load()
        return self._error or ""

    def recognize(self, image_path: Path) -> str:
        self._load()
        if self._engine is None:
            raise RuntimeError(self._error or "RapidOCR 未安装")
        result = self._engine(str(image_path))
        texts = getattr(result, "txts", None)
        if texts is None and isinstance(result, tuple):
            texts = [line[1] for line in (result[0] or [])]
        return "\n".join(text.strip() for text in (texts or []) if text and text.strip())

    def _load(self) -> None:
        if self._engine is not None or self._error is not None:
            return
        try:
            from rapidocr import RapidOCR

            files = {
                "Det.model_path": self.model_dir / "ch_PP-OCRv4_det_mobile.onnx",
                "Cls.model_path": self.model_dir / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                "Rec.model_path": self.model_dir / "ch_PP-OCRv4_rec_mobile.onnx",
                "Rec.rec_keys_path": self.model_dir / "ppocr_keys_v1.txt",
            }
            missing = [str(path) for path in files.values() if not path.is_file()]
            if missing:
                raise FileNotFoundError("OCR 模型文件缺失: " + ", ".join(missing))
            self._engine = RapidOCR(params={key: str(path) for key, path in files.items()})
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            logger.info("RapidOCR unavailable: %s", self._error)


class OcrService:
    def __init__(
        self,
        store: NoteStore,
        engine: RapidOcrEngine | None = None,
        on_complete: Callable[[int], None] | None = None,
    ) -> None:
        self.store = store
        self.engine = engine or RapidOcrEngine()
        self.on_complete = on_complete
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pocket-memory-ocr")

    def status(self) -> dict:
        return {
            "available": self.engine.available,
            "reason": self.engine.unavailable_reason,
        }

    def enqueue(self, note_id: int) -> None:
        if not self.engine.available:
            self.store.update_ocr(note_id, "unavailable", "", self.engine.unavailable_reason)
            return
        self.store.update_ocr(note_id, "pending", "", "")
        self.executor.submit(self._process, note_id)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _process(self, note_id: int) -> None:
        try:
            note = self.store.get_note(note_id)
            if not note.attachment_path:
                return
            self.store.update_ocr(note_id, "processing", "", "")
            text = self.engine.recognize(self.store.data_dir / note.attachment_path)
            self.store.update_ocr(note_id, "done", text, "")
            if self.on_complete:
                self.on_complete(note_id)
        except KeyError:
            return
        except Exception as exc:
            logger.exception("OCR failed for note %s", note_id)
            self.store.update_ocr(note_id, "failed", "", f"{type(exc).__name__}: {exc}")
