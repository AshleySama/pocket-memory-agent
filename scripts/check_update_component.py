# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Check the mandatory update worker in an EXE, full/update layout or ZIP.

This verifies a regular PE file and byte identity, not a publisher signature.
ZIP members are read into a private temporary file, never extracted by name.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from zipfile import BadZipFile, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pocket_memory.update_component import WORKER_RELATIVE_PATH, require_worker


def _match(actual: dict, expected: dict | None) -> None:
    if expected is not None and any(actual[key] != expected[key] for key in ('size', 'sha256')):
        raise ValueError('Update worker byte identity does not match the built worker.')


def check_layout(root: Path, expected_worker: Path | None = None) -> dict:
    """Accept a full installation or an update layout containing payload/."""
    root = Path(root).absolute()
    expected = require_worker(expected_worker) if expected_worker is not None else None
    if not root.is_dir():
        raise ValueError(f'Application layout is not a directory: {root}')
    program_root = root / 'payload' if (root / 'payload').is_dir() else root
    if program_root != root and (root / WORKER_RELATIVE_PATH).exists():
        raise ValueError('Ambiguous update worker: present in both root and payload layouts.')
    actual = require_worker(program_root / WORKER_RELATIVE_PATH)
    _match(actual, expected)
    return actual


def _worker_member(name: str) -> bool:
    """Allow no wrapper, or the one package-name wrapper used by create_zip64."""
    worker = WORKER_RELATIVE_PATH.split('/')
    parts = name.split('/')
    return (parts == worker or parts == ['payload', *worker]
            or len(parts) == 3 and parts[1:] == worker
            or len(parts) == 4 and parts[1:] == ['payload', *worker])


def check_archive(archive_path: Path, expected_worker: Path | None = None) -> dict:
    expected = require_worker(expected_worker) if expected_worker is not None else None
    with ZipFile(archive_path) as archive:
        members = archive.infolist()
        seen = set()
        worker_members = []
        for member in members:
            name = member.filename.rstrip('/')
            parts = name.split('/')
            if (not name or any(part in ('', '.', '..') for part in parts)
                    or any(char in name for char in ('\\', ':', '\x00'))
                    or PurePosixPath(name).is_absolute()):
                raise ValueError(f'Unsafe ZIP member path: {member.filename!r}')
            key = name.casefold()
            if key in seen:
                raise ValueError(f'Duplicate ZIP member: {member.filename}')
            seen.add(key)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f'ZIP links are not allowed: {member.filename}')
            if parts[-1].casefold() == Path(WORKER_RELATIVE_PATH).name.casefold():
                worker_members.append(member)
        if len(worker_members) != 1:
            raise ValueError('ZIP must contain exactly one update worker, without same-name alternatives.')
        member = worker_members[0]
        if not _worker_member(member.filename) or member.is_dir():
            raise ValueError(f'Update worker has an incorrect ZIP path: {member.filename}')
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise ValueError('ZIP update worker is not a regular file.')
        if expected is not None and member.file_size != expected['size']:
            raise ValueError('ZIP update worker size does not match the built worker.')
        with tempfile.TemporaryDirectory(prefix='pm-worker-check-') as temporary:
            worker = Path(temporary) / Path(WORKER_RELATIVE_PATH).name
            with archive.open(member) as source, worker.open('wb') as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            actual = require_worker(worker)
            _match(actual, expected)
        return dict(actual, path=f'{archive_path}!/{member.filename}')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path, help='Standalone worker EXE, application directory, or ZIP.')
    parser.add_argument('--expected-worker', type=Path, help='Built worker whose bytes must match.')
    args = parser.parse_args(argv)
    try:
        if args.target.is_dir():
            result = check_layout(args.target, args.expected_worker)
        elif args.target.suffix.casefold() == '.zip':
            result = check_archive(args.target, args.expected_worker)
        else:
            result = require_worker(args.target)
            _match(result, require_worker(args.expected_worker) if args.expected_worker is not None else None)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, BadZipFile, RuntimeError) as exc:
        print(f'Update component check failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
