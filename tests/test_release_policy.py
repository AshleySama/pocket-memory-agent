# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Protect release exclusions without removing working image/document paths."""
import runpy
from pathlib import Path
import unittest

exclude=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/release_policy.py'))['excluded_resource']

class ReleasePolicyTests(unittest.TestCase):
    def test_unused_resources_are_excluded(self):
        for p in ('_internal/cv2/opencv_videoio_ffmpeg500_64.dll',
                  'cv2/opencv_videoio_ffmpeg501_64.dll',
                  '_internal/hf_xet/hf_xet.pyd','nltk/test/tokenize.doctest',
                  'rapidocr/inference_engine/openvino/main.py',
                  'frontend/brand/suishouji-main-option-2.png',
                  'frontend/transition.html','frontend/qa-feedback.js'):
            with self.subTest(p=p): self.assertTrue(exclude(p))

    def test_required_resources_are_kept(self):
        for p in ('cv2/cv2.pyd','rapidocr/inference_engine/onnxruntime/main.py',
                  'rapidocr/default_models.yaml','nltk/tokenize/punkt.py',
                  'frontend/brand/pocket-memory-main-logo.png',
                  'frontend/brand/pocket-memory-secondary-logo.png',
                  'frontend/brand/pocket-memory-small-logo.png',
                  'onnxruntime/capi/onnxruntime.dll','pymupdf/mupdfcpp64.dll',
                  'numpy/_core/_multiarray_umath.pyd','licenses/LICENSE.txt'):
            with self.subTest(p=p): self.assertFalse(exclude(p))
