# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Verify a declared update executor and its exact release inventory.

Manifest v2 requires an embedded executor and a complete receipt for full layouts.
Legacy layouts without metadata still require their intact standalone worker.
These checks establish file identity, not a publisher signature; no code is run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from zipfile import BadZipFile, ZipFile, ZIP_STORED, ZIP_DEFLATED

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pocket_memory.update_component import WORKER_RELATIVE_PATH, require_worker
from pocket_memory.safe_update import (MANIFEST_NAME, _no_links, _read_json, _relative,
                                       _verify, _verify_components, validate_manifest)
from pocket_memory.embedded_update import CAPABILITY_PATH, validate_capability

RECEIPT_PATH = '.pocket-memory-update/installed.json'


def _receipt_manifest(receipt: dict) -> dict:
    if (not isinstance(receipt, dict)
            or set(receipt) != {'schema_version', 'app_id', 'version', 'data_schema_id', 'manifest'}
            or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or receipt['app_id'] != 'pocket-memory'):
        raise ValueError('Full installation receipt is incomplete or unsupported.')
    manifest = validate_manifest(receipt['manifest'])
    if (receipt['version'] != manifest['version']
            or receipt['data_schema_id'] != manifest['data_compat']['target_schema_id']):
        raise ValueError('Full installation receipt identity does not match its manifest.')
    return manifest


def _manifest_result(manifest: dict, layout: str, path) -> dict:
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False).encode('utf-8')
    return dict(status='ok', path=str(path), verification='manifest_inventory',
                executor=manifest.get('update_executor', 'standalone'),
                manifest_schema=manifest['schema_version'], layout=layout,
                manifest_sha256=hashlib.sha256(encoded).hexdigest(),
                verified_files=len(manifest['files']))


def _inventory(root: Path) -> dict[str, Path]:
    _no_links(root)
    if not root.is_dir():
        raise ValueError(f'Application layout is not a directory: {root}')
    result, seen = {}, set()
    def enumeration_error(error):
        raise error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=enumeration_error):
        for name in [*dirs, *files]:
            path = Path(folder) / name
            _no_links(path)
            relative = _relative(path.relative_to(root).as_posix())
            if relative.casefold() in seen:
                raise ValueError(f'Case-insensitive layout collision: {relative}')
            seen.add(relative.casefold())
            if path.is_file():
                result[relative] = path
            elif not path.is_dir():
                raise ValueError(f'Unsupported layout file: {relative}')
    return result


def _expected_layout_files(manifest: dict, layout: str, actual: set[str]) -> set[str]:
    if layout == 'update':
        expected = {'payload/' + e['path'] for e in manifest['files']} | {MANIFEST_NAME}
        optional = {'README.txt', '更新说明.txt'}
        if manifest['schema_version'] == 1:
            optional.add('PocketMemoryUpdate.exe')
    else:
        expected = ({e['path'] for e in manifest['files']}
                    | {'models/' + e['path'] for e in manifest['model_requirements']}
                    | {RECEIPT_PATH})
        optional = {'model-resources.json'}
    return expected | (actual & optional)


def _check_exact(actual: set[str], expected: set[str]) -> None:
    if actual != expected:
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        raise ValueError(f'Release differs from its exact manifest inventory: '
                         f'missing={missing[:8]}, extra={extra[:8]}')


def check_layout(root: Path, expected_worker: Path | None = None) -> dict:
    """Validate declared full/update layouts, or an intact legacy worker layout."""
    root = Path(root).absolute()
    _no_links(root)
    manifest_path, receipt_path = root / MANIFEST_NAME, root / RECEIPT_PATH
    _no_links(manifest_path); _no_links(receipt_path)
    if manifest_path.exists() and receipt_path.exists():
        raise ValueError('Release contains both an update manifest and full receipt.')
    if manifest_path.exists():
        manifest, layout = validate_manifest(_read_json(manifest_path)), 'update'
    elif receipt_path.exists():
        manifest, layout = _receipt_manifest(_read_json(receipt_path)), 'full'
    else:
        return _check_legacy_layout(root, expected_worker)
    if manifest['schema_version'] == 2 and expected_worker is not None:
        raise ValueError('Embedded releases do not accept an expected standalone worker.')
    inventory = _inventory(root)
    _check_exact(set(inventory), _expected_layout_files(manifest, layout, set(inventory)))
    program_root = root / 'payload' if layout == 'update' else root
    for entry in manifest['files']:
        _verify(program_root / entry['path'], entry)
    if layout == 'full':
        for entry in manifest['model_requirements']:
            _verify(root / 'models' / entry['path'], entry)
        if 'model-resources.json' in inventory:
            resources = _decode_json(inventory['model-resources.json'].read_bytes())
            if resources != manifest['model_requirements']:
                raise ValueError('Model resource inventory differs from the installed receipt.')
    _verify_components(program_root, manifest)
    if manifest['schema_version'] == 1:
        _match(require_worker(program_root / WORKER_RELATIVE_PATH),
               require_worker(expected_worker) if expected_worker is not None else None)
    return _manifest_result(manifest, layout, root)


def _match(actual: dict, expected: dict | None) -> None:
    if expected is not None and any(actual[key] != expected[key] for key in ('size', 'sha256')):
        raise ValueError('Update worker byte identity does not match the built worker.')


def _check_legacy_layout(root: Path, expected_worker: Path | None = None) -> dict:
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
    return dict(actual, verification='legacy_worker', executor='standalone')


def _worker_member(name: str) -> bool:
    """Allow no wrapper, or the one package-name wrapper used by create_zip64."""
    worker = WORKER_RELATIVE_PATH.split('/')
    parts = name.split('/')
    return (parts == worker or parts == ['payload', *worker]
            or len(parts) == 3 and parts[1:] == worker
            or len(parts) == 4 and parts[1:] == ['payload', *worker])


def _decode_json(raw: bytes):
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Release JSON exceeds the safety size limit.')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(f'Duplicate JSON field: {key}')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs)


def _archive_members(archive: ZipFile) -> dict:
    files, seen, spellings = {}, set(), {}
    for member in archive.infolist():
        if member.orig_filename != member.filename:
            raise ValueError('Truncated ZIP member path.')
        try:
            name = _relative(member.filename.rstrip('/'))
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        key = name.casefold()
        if key in seen:
            raise ValueError(f'Duplicate ZIP member: {member.filename}')
        seen.add(key)
        parts = name.split('/')
        for length in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:length])
            previous = spellings.setdefault(prefix.casefold(), prefix)
            if previous != prefix or (length < len(parts) and prefix in files):
                raise ValueError(f'ZIP parent spelling or file/directory collision: {name}')
        mode = stat.S_IFMT(member.external_attr >> 16)
        if (mode not in (0, stat.S_IFREG, stat.S_IFDIR) or member.flag_bits & 1
                or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
            raise ValueError(f'ZIP link, encryption or unsupported member: {name}')
        if not member.is_dir():
            if mode == stat.S_IFDIR or any(k.startswith(key + '/') for k in spellings):
                raise ValueError(f'ZIP file/directory collision: {name}')
            files[name] = member
    return files


def _zip_json(archive: ZipFile, member, *, maximum=16 * 1024 * 1024):
    if member.file_size > maximum:
        raise ValueError('Release JSON exceeds the safety size limit.')
    return _decode_json(archive.read(member))


def _verify_archive_entry(archive: ZipFile, member, entry: dict) -> None:
    if member.file_size != entry['size']:
        raise ValueError(f'ZIP file size differs from manifest: {member.filename}')
    digest, count = hashlib.sha256(), 0
    with archive.open(member) as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            count += len(block)
            if count > entry['size']:
                raise ValueError(f'ZIP content exceeds manifest size: {member.filename}')
            digest.update(block)
    if count != entry['size'] or digest.hexdigest() != entry['sha256']:
        raise ValueError(f'ZIP file differs from manifest: {member.filename}')


def check_archive(archive_path: Path, expected_worker: Path | None = None) -> dict:
    _no_links(Path(archive_path).absolute())
    with ZipFile(archive_path) as archive:
        files = _archive_members(archive)
        descriptors = []
        for name in files:
            if name == MANIFEST_NAME or (name.count('/') == 1 and name.endswith('/' + MANIFEST_NAME)):
                descriptors.append((name[:-len(MANIFEST_NAME)], name, 'update'))
            if name == RECEIPT_PATH or (name.count('/') == 2 and name.endswith('/' + RECEIPT_PATH)):
                descriptors.append((name[:-len(RECEIPT_PATH)], name, 'full'))
        if not descriptors:
            # Compatibility is explicit in the presence of an intact worker;
            # a missing worker never creates an implicit embedded release.
            return _check_legacy_archive(archive_path, expected_worker)
        if len(descriptors) != 1:
            raise ValueError('ZIP must contain one unambiguous release manifest or receipt.')
        prefix, metadata_name, layout = descriptors[0]
        if prefix and any(not name.startswith(prefix) for name in files):
            raise ValueError('ZIP contains mixed release roots.')
        relative = {name[len(prefix):]: member for name, member in files.items()}
        document = _zip_json(archive, files[metadata_name],
                             maximum=2 * 1024 * 1024 if layout == 'update' else 16 * 1024 * 1024)
        manifest = validate_manifest(document) if layout == 'update' else _receipt_manifest(document)
        if manifest['schema_version'] == 2 and expected_worker is not None:
            raise ValueError('Embedded releases do not accept an expected standalone worker.')
        _check_exact(set(relative), _expected_layout_files(manifest, layout, set(relative)))
        program_prefix = 'payload/' if layout == 'update' else ''
        for entry in manifest['files']:
            _verify_archive_entry(archive, relative[program_prefix + entry['path']], entry)
        if layout == 'full':
            for entry in manifest['model_requirements']:
                _verify_archive_entry(archive, relative['models/' + entry['path']], entry)
            if ('model-resources.json' in relative
                    and _zip_json(archive, relative['model-resources.json']) != manifest['model_requirements']):
                raise ValueError('ZIP model inventory differs from the installed receipt.')
        entries = {entry['path']: entry for entry in manifest['files']}
        # Only known fixed names are materialized, never member-supplied paths.
        # Every declared byte was checked above; this validates capability and PE
        # structure without extracting the complete runtime or any model files.
        with tempfile.TemporaryDirectory(prefix='pm-executor-check-') as temporary:
            temporary_root = Path(temporary)
            required = ['PocketMemory.exe'] if manifest['schema_version'] == 2 else [WORKER_RELATIVE_PATH]
            if CAPABILITY_PATH in entries:
                required.append(CAPABILITY_PATH)
            for relative_path in required:
                target = temporary_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(relative[program_prefix + relative_path]) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            if CAPABILITY_PATH in entries:
                validate_capability(temporary_root, manifest)
            executable = temporary_root / ('PocketMemory.exe' if manifest['schema_version'] == 2 else WORKER_RELATIVE_PATH)
            actual = require_worker(executable)
            if manifest['schema_version'] == 1:
                _match(actual, require_worker(expected_worker) if expected_worker is not None else None)
        return _manifest_result(manifest, layout, archive_path)


def _check_legacy_archive(archive_path: Path, expected_worker: Path | None = None) -> dict:
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
        return dict(actual, path=f'{archive_path}!/{member.filename}',
                    verification='legacy_worker', executor='standalone')


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
