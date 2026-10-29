# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Retained, source-backed QA protocol promoted from the accepted candidate.

The host owns service lifetimes and request serialization. Import Candidate
explicitly when its optional retrieval dependencies have been configured.
"""

import os
from pocket_memory.embedding import resource_path

# The tokenizer assets ship with the local runtime; never download at startup.
os.environ.setdefault('NLTK_DATA', str(resource_path('models/nltk-data')))

