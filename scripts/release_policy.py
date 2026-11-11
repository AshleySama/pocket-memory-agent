# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Build-time exclusions; runtime image/OCR/document libraries remain required."""
from pathlib import PurePosixPath

BRAND_FILES = frozenset({
    'pocket-memory-main-logo.png', 'pocket-memory-secondary-logo.png',
    'pocket-memory-small-logo.png',
})
UNUSED_OCR_ENGINES = ('mnn', 'openvino', 'paddle', 'pytorch', 'tensorrt')

def excluded_resource(path: str) -> bool:
    path = path.replace('\\', '/').removeprefix('_internal/').lower()
    name = PurePosixPath(path).name
    if path.startswith('cv2/') and name.startswith('opencv_videoio_ffmpeg') and name.endswith('.dll'):
        return True
    if path.startswith(('hf_xet/', 'nltk/test/')):
        return True
    if any(path.startswith('rapidocr/inference_engine/' + e + '/') for e in UNUSED_OCR_ENGINES):
        return True
    if path.startswith('frontend/brand/') and name not in BRAND_FILES:
        return True
    return path in ('frontend/transition.html', 'frontend/qa-feedback.js',
                    'frontend/polish-review.html', 'frontend/tool-review.html')
