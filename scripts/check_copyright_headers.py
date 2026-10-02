# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Verify project-owned source files retain the Pocket Memory attribution."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORIES = (".github", "frontend", "pocket_memory", "scripts", "site", "tests", "website")
SOURCE_SUFFIXES = {".py", ".js", ".css", ".html", ".ps1", ".bat", ".yml", ".yaml"}
REQUIRED_MARKERS = ("Copyright 2026 AshleySama", "SPDX-License-Identifier: Apache-2.0")


def is_project_source(path: Path) -> bool:
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    relative = path.relative_to(PROJECT_ROOT)
    return bool(
        (relative.parts and relative.parts[0] in SOURCE_DIRECTORIES)
        or len(relative.parts) == 1
    )


def main() -> int:
    missing: list[str] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or not is_project_source(path):
            continue
        prefix = path.read_text(encoding="utf-8-sig", errors="replace")[:1200]
        if not all(marker in prefix for marker in REQUIRED_MARKERS):
            missing.append(path.relative_to(PROJECT_ROOT).as_posix())

    if missing:
        print("Missing copyright/SPDX header:")
        print("\\n".join(f" - {path}" for path in missing))
        return 1

    print("Copyright/SPDX header check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
