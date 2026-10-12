# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Literal evaluation labels with numeric boundaries, not semantic judgment."""
from __future__ import annotations

import re


METRIC_VERSION = "literal-numeric-boundaries-v1"


def _normalise_label(value: str) -> str:
    # Layout whitespace may separate two actual numbers (including adjacent
    # evidence fields). Do not turn "8\n8" into the different value "88".
    value = re.sub(r"(?<=\d)\s+(?=\d)", "\x00", str(value or ""))
    return re.sub(r"\s+", "", value).replace("\x00", " ").casefold()


def contains_eval_term(text: str, term: str) -> bool:
    """Ignore spacing/case, but never count 0% inside 20% or 8 inside 18.

    Non-numeric labels remain literal substring proxies. This deliberately does
    not infer synonyms, numeric equivalence, negation, or factual correctness.
    """
    haystack = _normalise_label(text)
    needle = _normalise_label(term)
    if not needle:
        return False
    parts = re.split(r"(\d+(?:\.\d+)?)", needle)
    pattern = "".join(
        rf"(?<![\d.]){re.escape(part)}(?!\d|\.\d)" if index % 2 else re.escape(part)
        for index, part in enumerate(parts)
    )
    return re.search(pattern, haystack) is not None
