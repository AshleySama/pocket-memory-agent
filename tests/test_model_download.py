from __future__ import annotations

import hashlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pocket_memory.model_download import ModelDownloadService, ModelDownloadSpec


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._stream = io.BytesIO(payload)
        self.status = status
        self.headers = {"Content-Length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stream.close()


class ModelDownloadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self.temp.name) / "models"
        self.payload = b"Pocket Memory test model payload"
        self.spec = ModelDownloadSpec(
            id="test-model",
            label="Test model",
            source_page="https://example.invalid/model",
            download_url="https://example.invalid/model.gguf",
            remote_filename="model.gguf",
            destination_relative_path="Qwen3-4B/model.gguf",
            expected_size=len(self.payload),
            sha256=hashlib.sha256(self.payload).hexdigest(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wait_for_download(self, service: ModelDownloadService) -> dict:
        for _ in range(100):
            status = service.status()
            if status["state"] not in {"downloading", "verifying"}:
                return status
            time.sleep(0.01)
        self.fail("model downloader did not finish")

    def test_downloads_and_verifies_before_exposing_model(self) -> None:
        service = ModelDownloadService(self.models_dir, self.spec)
        with patch("pocket_memory.model_download.urlopen", return_value=FakeResponse(self.payload)):
            service.start()
            status = self.wait_for_download(service)

        self.assertEqual(status["state"], "installed")
        self.assertTrue(status["verified"])
        self.assertTrue(status["installed"])
        self.assertEqual(service.target_path.read_bytes(), self.payload)
        self.assertFalse(service.partial_path.exists())

    def test_resumes_a_partial_file_when_source_accepts_range(self) -> None:
        service = ModelDownloadService(self.models_dir, self.spec)
        service.partial_path.parent.mkdir(parents=True)
        service.partial_path.write_bytes(self.payload[:8])
        requests = []

        def respond(request, timeout):
            requests.append(request)
            return FakeResponse(self.payload[8:], status=206)

        with patch("pocket_memory.model_download.urlopen", side_effect=respond):
            service.start()
            status = self.wait_for_download(service)

        self.assertEqual(status["state"], "installed")
        self.assertEqual(requests[0].get_header("Range"), "bytes=8-")
        self.assertEqual(service.target_path.read_bytes(), self.payload)

    def test_rejects_a_download_with_the_wrong_hash(self) -> None:
        service = ModelDownloadService(self.models_dir, self.spec)
        with patch("pocket_memory.model_download.urlopen", return_value=FakeResponse(b"wrong model content")):
            service.start()
            status = self.wait_for_download(service)

        self.assertEqual(status["state"], "failed")
        self.assertIn("大小异常", status["error"])
        self.assertFalse(service.target_path.exists())

    def test_recognizes_a_matching_bundled_model_without_downloading(self) -> None:
        bundled = Path(self.temp.name) / "_internal" / "models" / self.spec.destination_relative_path
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(self.payload)
        service = ModelDownloadService(self.models_dir, self.spec, bundled_model_path=bundled)

        status = service.status()

        self.assertTrue(status["installed"])
        self.assertEqual(status["installation_source"], "bundled")
        self.assertEqual(status["local_model_size"], len(self.payload))

    def test_recognizes_a_bundled_model_without_requiring_downloader_byte_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundled_path = root / "_internal" / "models" / self.spec.destination_relative_path
            bundled_path.parent.mkdir(parents=True)
            bundled_path.write_bytes(self.payload + b"publisher-metadata")

            service = ModelDownloadService(root / "models", spec=self.spec, bundled_model_path=bundled_path)

            status = service.status()
            self.assertTrue(status["installed"])
            self.assertEqual(status["installation_source"], "bundled")
            self.assertFalse(status["verified"])
