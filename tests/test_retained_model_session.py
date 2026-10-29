# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from pocket_memory.config import AppConfig
from pocket_memory.intelligence import IntelligenceService, LlamaCppTextEngine


class RetainedModelSessionTests(unittest.TestCase):
    def test_default_retention_survives_long_idle_and_manual_unload_works(self):
        engine = LlamaCppTextEngine()
        handle = object()
        engine._llm = handle
        engine._last_used_at = time.monotonic() - 3600
        self.assertFalse(engine.unload_if_idle(IntelligenceService.IDLE_UNLOAD_SECONDS))
        self.assertIs(engine._llm, handle)
        engine.unload()
        self.assertIsNone(engine._llm)

    def test_runtime_resources_use_exact_bundled_asset_fallback(self):
        from pocket_memory.rag.retained.context import RuntimeContext
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # External LLM folder must not shadow bundled reranker resources.
            (root/'models').mkdir()
            bundled = root/'_internal'
            with patch('sys.executable', str(root/'PocketMemory.exe')), patch('sys._MEIPASS', str(bundled), create=True):
                context = RuntimeContext(SimpleNamespace(data_dir=root/'library'), None, None)
            self.assertEqual(context.reranker_model_dir, (bundled/'models/reranker-models/ms-marco-MultiBERT-L-12').resolve())
            self.assertEqual(context.reranker_manifest, (bundled/'models/reranker-manifest.json').resolve())
            context.close()

    def test_idle_unload_rechecks_after_inflight_session(self):
        engine = LlamaCppTextEngine()
        handle = object()
        engine._llm = handle
        engine._last_used_at = time.monotonic() - 1000
        started = threading.Event()
        done = threading.Event()
        outcome = []

        def attempt_unload():
            started.set()
            outcome.append(engine.unload_if_idle(60))
            done.set()

        with engine.session():
            thread = threading.Thread(target=attempt_unload)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(done.wait(.03))
            self.assertIs(engine._llm, handle)
        thread.join(2)
        self.assertEqual(outcome, [False])
        self.assertIs(engine._llm, handle)
        engine.unload()
        self.assertIsNone(engine._llm)

    def test_config_preserves_explicit_legacy_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            self.assertEqual(AppConfig.load(path).rag_backend, 'retained')
            config = AppConfig(path, rag_backend='legacy')
            config._save()
            self.assertEqual(AppConfig.load(path).rag_backend, 'legacy')
            path.write_text('{"rag_backend":null}', encoding='utf8')
            self.assertEqual(AppConfig.load(path).rag_backend, 'retained')
