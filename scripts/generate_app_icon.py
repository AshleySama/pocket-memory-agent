# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Build square Windows icons from the existing text-free Pocket Memory mark.

Keep large brand artwork untouched. Small shell icons use almost the full square
and are each resampled directly from the source, not from another small icon.
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend/brand/pocket-memory-secondary-logo.png"
DESTINATION = ROOT / "frontend/app-icon.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def make_frames():
    source = Image.open(SOURCE).convert("RGBA")
    alpha = source.getchannel("A")
    bounds = alpha.point(lambda value: 255 if value >= 8 else 0).getbbox()
    if bounds is None:
        raise ValueError("The brand mark is empty")
    # Ignore isolated near-transparent export noise when measuring padding.
    left, top, right, bottom = bounds
    bounds = (max(0, left - 2), max(0, top - 2), min(source.width, right + 2), min(source.height, bottom + 2))
    mark = source.crop(bounds)
    frames = []
    for size in SIZES:
        inset = 0 if size <= 32 else max(1, round(size / 64))
        available = size - 2 * inset
        scale = available / max(mark.size)
        resized = mark.resize((max(1, round(mark.width * scale)), max(1, round(mark.height * scale))), Image.Resampling.LANCZOS)
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.alpha_composite(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
        frames.append(frame)
    return frames


if __name__ == "__main__":
    frames = make_frames()
    frames[-1].save(DESTINATION, format="ICO", sizes=[(size, size) for size in SIZES], append_images=frames[:-1])
    with Image.open(DESTINATION) as check:
        assert check.ico.sizes() == {(size, size) for size in SIZES}
    print(f"Generated {DESTINATION} with {len(frames)} square resolutions")
