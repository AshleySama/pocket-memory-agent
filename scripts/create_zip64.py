"""Create a standards-compatible ZIP64 archive for large Pocket Memory releases."""
from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: create_zip64.py <source-directory> <archive.zip>")
        return 2

    source = Path(sys.argv[1]).resolve()
    archive_path = Path(sys.argv[2]).resolve()
    if not source.is_dir():
        print(f"Source directory not found: {source}")
        return 2
    if archive_path.parent == source or source in archive_path.parents:
        print("Archive path must not be inside the source directory.")
        return 2

    files = [path for path in source.rglob("*") if path.is_file()]
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for index, path in enumerate(files, start=1):
            archive_name = (Path(source.name) / path.relative_to(source)).as_posix()
            # GGUF weights are already compressed-like binary data and can exceed
            # the 2 GB legacy ZIP threshold, so retain them as ZIP64 stored entries.
            compression = ZIP_STORED if path.suffix.lower() == ".gguf" else ZIP_DEFLATED
            archive.write(path, archive_name, compress_type=compression)
            if index % 100 == 0 or index == len(files):
                print(f"Archived {index}/{len(files)} files", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
