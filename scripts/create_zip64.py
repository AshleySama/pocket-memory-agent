# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Create a standards-compatible ZIP64 archive for large Pocket Memory releases."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_update_component import check_archive, check_layout, _inventory
from pocket_memory.safe_update import _no_links


def create_archive(source: Path, archive_path: Path, *, expected_worker: Path | None = None) -> None:
    # Do not resolve input links away before the shared executor/inventory checks.
    source = Path(source).absolute()
    archive_path = Path(archive_path).absolute()
    if not source.is_dir():
        raise ValueError(f'Source directory not found: {source}')
    if source.resolve() in archive_path.resolve().parents:
        raise ValueError('Archive path must not be inside the source directory.')
    before = check_layout(source, expected_worker)
    if archive_path.exists():
        raise ValueError(f'Archive already exists: {archive_path}')

    files = [path for _, path in sorted(_inventory(source).items())]
    output_parent = archive_path.parent.resolve()
    descriptor, temporary_name = tempfile.mkstemp(prefix=archive_path.name + '.', suffix='.part',
                                                 dir=output_parent)
    os.close(descriptor)
    partial = Path(temporary_name).absolute()
    # Only this uniquely created file can be removed on failure. Never clean up
    # a caller's archive or recursively remove an output/build directory.
    if partial.parent != output_parent:
        raise ValueError('Temporary archive escaped its output directory.')
    try:
        with ZipFile(partial, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for index, path in enumerate(files, start=1):
                _no_links(path)
                archive_name = (Path(source.name) / path.relative_to(source)).as_posix()
                # GGUF weights are already compressed-like binary data and can exceed
                # the 2 GB legacy ZIP threshold, so retain them as ZIP64 stored entries.
                compression = ZIP_STORED if path.suffix.lower() == ".gguf" else ZIP_DEFLATED
                archive.write(path, archive_name, compress_type=compression)
                if index % 100 == 0 or index == len(files):
                    print(f"Archived {index}/{len(files)} files", flush=True)
        after = check_archive(partial, expected_worker)
        if before['verification'] == 'manifest_inventory':
            identity_keys = ('verification', 'executor', 'manifest_schema', 'layout',
                             'manifest_sha256', 'verified_files')
        else:
            identity_keys = ('verification', 'executor', 'size', 'sha256')
        if any(after.get(key) != before.get(key) for key in identity_keys):
            raise ValueError('ZIP update executor or inventory changed after the pre-archive check.')
        if os.name == 'nt':
            # Windows rename is atomic and fails if the destination exists.
            os.rename(partial, archive_path)
        else:
            # POSIX rename would overwrite; atomic link creation keeps the same
            # no-overwrite contract, even if another writer won the race.
            os.link(partial, archive_path)
    finally:
        partial.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--expected-worker', type=Path, help='Legacy standalone releases only.')
    args = parser.parse_args(argv)
    try:
        create_archive(args.source, args.archive, expected_worker=args.expected_worker)
    except (OSError, ValueError, RuntimeError, BadZipFile) as exc:
        print(f'ZIP64 packaging failed: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
