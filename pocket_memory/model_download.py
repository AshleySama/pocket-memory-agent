from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelDownloadSpec:
    """A single, reviewed model artifact that the application is allowed to fetch."""

    id: str
    label: str
    source_page: str
    download_url: str
    remote_filename: str
    destination_relative_path: str
    expected_size: int
    sha256: str


# The URL uses ModelScope's documented single-file download API.  The SHA-256
# pins the artifact even though the public repository's branch is named master.
DEFAULT_QWEN_4B = ModelDownloadSpec(
    id="qwen3-4b-instruct-2507-q4-k-m",
    label="Qwen3-4B-Instruct-2507 Q4_K_M",
    source_page="https://www.modelscope.cn/models/Voconly/Qwen3-4B-Instruct-2507-Q4_K_M-GGUF",
    download_url=(
        "https://www.modelscope.cn/api/v1/models/voconly/"
        "Qwen3-4B-Instruct-2507-Q4_K_M-GGUF/repo?Revision=master&"
        "FilePath=Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
    ),
    remote_filename="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    destination_relative_path="Qwen3-4B/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    expected_size=2_497_281_120,
    sha256="3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
)


class ModelDownloadService:
    """Downloads one approved LLM artifact without blocking the local web server."""

    CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        models_dir: Path,
        spec: ModelDownloadSpec = DEFAULT_QWEN_4B,
        bundled_model_path: Path | None = None,
    ) -> None:
        self.models_dir = models_dir.resolve()
        self.spec = spec
        self.target_path = self.models_dir / spec.destination_relative_path
        self.partial_path = self.target_path.with_name(self.target_path.name + ".part")
        # Complete releases put the immutable model under PyInstaller's
        # _internal directory; online-model releases use models/ beside the EXE.
        self.bundled_model_path = bundled_model_path.resolve() if bundled_model_path else None
        self._lock = threading.RLock()
        self._cancel_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "installed" if self._has_expected_size() else "idle"
        self._downloaded_bytes = 0
        self._total_bytes = spec.expected_size
        self._error = ""
        self._verified = False
        self._started_at = 0.0

    def status(self) -> dict:
        with self._lock:
            installed = self._has_expected_size()
            local_model_size = self._local_model_size()
            installation_source = self._installation_source()
            state = self._state
            if installed and state == "idle":
                state = "installed"
            return {
                "id": self.spec.id,
                "label": self.spec.label,
                "source_page": self.spec.source_page,
                "filename": self.target_path.name,
                "expected_size": self.spec.expected_size,
                "downloaded_bytes": self._downloaded_bytes,
                "total_bytes": self._total_bytes,
                "state": state,
                "installed": installed,
                "installation_source": installation_source,
                "local_model_detected": local_model_size >= 10 * 1024 * 1024,
                "local_model_size": local_model_size,
                "verified": self._verified,
                "can_cancel": bool(self._thread and self._thread.is_alive()),
                "error": self._error,
            }

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            if self._has_expected_size():
                self._state = "installed"
                return self.status()
            self._cancel_requested.clear()
            self._error = ""
            self._state = "downloading"
            self._downloaded_bytes = self._partial_size()
            self._total_bytes = self.spec.expected_size
            self._started_at = time.monotonic()
            self._thread = threading.Thread(target=self._download, name="pocket-memory-model-download", daemon=True)
            self._thread.start()
            return self.status()

    def cancel(self) -> dict:
        self._cancel_requested.set()
        return self.status()

    def _download(self) -> None:
        try:
            self.target_path.parent.mkdir(parents=True, exist_ok=True)
            offset = self._partial_size()
            headers = {"User-Agent": "PocketMemory/1.1"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(self.spec.download_url, headers=headers)
            with urlopen(request, timeout=30) as response:
                status_code = getattr(response, "status", response.getcode())
                if offset and status_code != 206:
                    # A mirror that ignores Range must not append a full response.
                    self.partial_path.unlink(missing_ok=True)
                    offset = 0
                content_length = response.headers.get("Content-Length")
                received_total = int(content_length) + offset if content_length else self.spec.expected_size
                if received_total != self.spec.expected_size:
                    raise ValueError(
                        f"下载源文件大小异常：期望 {self.spec.expected_size} 字节，收到 {received_total} 字节"
                    )
                mode = "ab" if offset else "wb"
                with self.partial_path.open(mode) as target:
                    self._set_progress("downloading", offset, received_total)
                    while not self._cancel_requested.is_set():
                        block = response.read(self.CHUNK_SIZE)
                        if not block:
                            break
                        target.write(block)
                        offset += len(block)
                        self._set_progress("downloading", offset, received_total)
            if self._cancel_requested.is_set():
                self._set_state("paused")
                return
            if self._partial_size() != self.spec.expected_size:
                raise ValueError("下载未完成，请检查网络后继续")
            self._set_state("verifying")
            digest = self._sha256(self.partial_path)
            if digest.lower() != self.spec.sha256.lower():
                self.partial_path.unlink(missing_ok=True)
                raise ValueError("模型文件校验失败，已删除损坏下载，请重新下载")
            os.replace(self.partial_path, self.target_path)
            with self._lock:
                self._downloaded_bytes = self.spec.expected_size
                self._total_bytes = self.spec.expected_size
                self._verified = True
                self._state = "installed"
        except (HTTPError, URLError, OSError, ValueError) as exc:
            logger.warning("模型下载失败: %s", exc)
            self._set_error(str(exc))
        except Exception as exc:  # Keep the UI responsive even for an unexpected downloader failure.
            logger.exception("模型下载出现未预期错误")
            self._set_error(f"下载器异常：{exc}")

    def _set_progress(self, state: str, downloaded: int, total: int) -> None:
        with self._lock:
            self._state = state
            self._downloaded_bytes = downloaded
            self._total_bytes = total

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def _set_error(self, error: str) -> None:
        with self._lock:
            self._state = "failed"
            self._error = error

    def _partial_size(self) -> int:
        try:
            return self.partial_path.stat().st_size
        except OSError:
            return 0

    def _has_expected_size(self) -> bool:
        return self._installation_source() is not None

    def _installation_source(self) -> str | None:
        if self._target_model_size() == self.spec.expected_size:
            return "downloaded"
        # The complete package owns its immutable _internal payload.  Different
        # trusted GGUF publishers may append a small metadata trailer, so exact
        # byte count is only required for a file the in-app downloader fetched.
        if self._bundled_model_size() > 0:
            return "bundled"
        return None

    def _local_model_size(self) -> int:
        return max(self._target_model_size(), self._bundled_model_size())

    def _target_model_size(self) -> int:
        try:
            return self.target_path.stat().st_size
        except OSError:
            return 0

    def _bundled_model_size(self) -> int:
        if self.bundled_model_path is None or self.bundled_model_path == self.target_path:
            return 0
        try:
            return self.bundled_model_path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()
