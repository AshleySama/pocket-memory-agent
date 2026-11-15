# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Focused load/retry contracts. All native constructors are fakes; no inference."""
import hashlib
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pocket_memory.intelligence import IntelligenceService, LlamaCppTextEngine
from pocket_memory.model_health import ModelFileError, diagnose_load_failure, inspect_model_file


class ModelLoadHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "models" / "Qwen3-4B" / "model.gguf"
        self.path.parent.mkdir(parents=True)
        self.original = b"GGUF" + bytes(28)
        self.path.write_bytes(self.original)

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, payload=None):
        payload = payload if payload is not None else self.original
        (self.root / "model-resources.json").write_text(json.dumps([{
            "path": "Qwen3-4B/model.gguf", "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }]), encoding="utf-8")

    def module(self, constructor):
        native = types.ModuleType("llama_cpp")
        native.Llama = constructor
        return patch.dict(sys.modules, {"llama_cpp": native})

    def test_missing_and_invalid_header_do_not_enter_native_loader(self):
        calls = []
        with self.module(lambda **kwargs: calls.append(kwargs)):
            self.path.unlink()
            missing = LlamaCppTextEngine(self.path)
            self.assertFalse(missing.available)
            self.assertIn("未找到", missing.unavailable_reason)
            self.path.write_bytes(b"not a GGUF" + bytes(32))
            broken = LlamaCppTextEngine(self.path)
            self.assertFalse(broken.available)
            self.assertIn("格式不完整", broken.unavailable_reason)
        self.assertEqual(calls, [])

    def test_known_release_size_and_same_size_corruption_are_distinguished(self):
        self.manifest()
        self.path.write_bytes(self.original + b"x")
        with self.assertRaisesRegex(ModelFileError, "大小"):
            inspect_model_file(self.path)
        self.path.write_bytes(b"GGUF" + b"x" * 28)
        before = inspect_model_file(self.path)
        message, diagnosis = diagnose_load_failure(self.path, before, ["native failed"])
        self.assertIn("校验未通过", message)
        self.assertEqual(diagnosis["integrity"], "mismatch")

    def test_package_inventory_not_download_mirror_is_integrity_reference(self):
        self.manifest()
        before = inspect_model_file(self.path)
        message, diagnosis = diagnose_load_failure(self.path, before, ["native failed"])
        self.assertEqual(diagnosis["integrity"], "matches_release_inventory")
        self.assertEqual(diagnosis["cause"], "unknown")
        self.assertNotIn("损坏", message)

    def test_custom_gguf_without_inventory_not_misclassified_as_corrupt(self):
        before = inspect_model_file(self.path)
        self.assertIsNone(before["manifest"])
        _, diagnosis = diagnose_load_failure(self.path, before, ["native failed"])
        self.assertEqual(diagnosis["integrity"], "not_verified")
        self.assertEqual(diagnosis["cause"], "unknown")

    def test_no_full_model_hash_on_successful_load(self):
        self.manifest()
        with self.module(lambda **kwargs: types.SimpleNamespace(verbose=True)), patch(
                "pocket_memory.intelligence.diagnose_load_failure", side_effect=AssertionError("success must not hash")):
            engine = LlamaCppTextEngine(self.path)
            self.assertTrue(engine.available)
            self.assertFalse(engine._llm.verbose)

    def test_read_permission_failure_is_not_a_memory_claim(self):
        with patch.object(Path, "open", side_effect=PermissionError("access denied")):
            with self.assertRaisesRegex(ModelFileError, "访问权限") as caught:
                inspect_model_file(self.path)
        self.assertIn("PermissionError", caught.exception.details["read_error"])

    def test_failed_load_is_cached_until_explicit_retry_and_parameters_preserved(self):
        calls = []
        def load(**kwargs):
            calls.append(kwargs)
            if len(calls) <= 3:
                raise ValueError("native failed")
            return types.SimpleNamespace(verbose=True)
        with self.module(load):
            engine = LlamaCppTextEngine(self.path)
            self.assertFalse(engine.available)
            self.assertFalse(engine.available)
            self.assertEqual(len(calls), 3)
            self.assertTrue(engine.runtime_status()["can_retry"])
            self.assertEqual([(call["use_mlock"], call["use_mmap"]) for call in calls],
                             [(True, True), (False, True), (False, False)])
            self.assertTrue(engine.retry_load())
            self.assertEqual(len(calls), 4)
            self.assertTrue(engine.retry_load())
            self.assertEqual(len(calls), 4)
            self.assertFalse(engine.runtime_status()["can_retry"])
            self.assertEqual(calls[-1]["n_ctx"], 3072)
            self.assertEqual(calls[-1]["n_threads"], 4)
            self.assertEqual(calls[-1]["n_gpu_layers"], 0)

    def test_preflight_failure_can_recover_after_file_restored(self):
        self.path.unlink()
        with self.module(lambda **kwargs: types.SimpleNamespace(verbose=True)):
            engine = LlamaCppTextEngine(self.path)
            self.assertFalse(engine.available)
            self.path.write_bytes(self.original)
            self.assertFalse(engine.available)
            self.assertTrue(engine.retry_load())

    def test_retry_clicks_coalesce_with_pending_warmup_single_native_instance(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def load(**kwargs):
            calls.append(kwargs)
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("test constructor wait timed out")
            return types.SimpleNamespace(verbose=True)
        with self.module(load):
            engine = LlamaCppTextEngine(self.path)
            service = IntelligenceService(types.SimpleNamespace(), engine=engine)
            try:
                self.assertTrue(service.warm_up())
                self.assertTrue(entered.wait(timeout=2))
                self.assertFalse(service.retry_model())
                self.assertFalse(service.retry_model())
                release.set()
                service._warmup_future.result(timeout=2)
                self.assertEqual(len(calls), 1)
                self.assertFalse(service.retry_model())
                self.assertTrue(engine.model_loaded)
            finally:
                release.set()
                service.shutdown()

    def test_service_retries_a_failed_warmup_only_after_explicit_action(self):
        calls = []
        def load(**kwargs):
            calls.append(kwargs)
            if len(calls) <= 3:
                raise ValueError("transient failure")
            return types.SimpleNamespace(verbose=True)
        with self.module(load):
            engine = LlamaCppTextEngine(self.path)
            service = IntelligenceService(types.SimpleNamespace(), engine=engine)
            try:
                self.assertTrue(service.warm_up())
                service._warmup_future.result(timeout=2)
                self.assertFalse(engine.available)
                self.assertEqual(len(calls), 3)
                self.assertTrue(service.retry_model())
                service._warmup_future.result(timeout=2)
                self.assertTrue(engine.model_loaded)
                self.assertEqual(len(calls), 4)
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
