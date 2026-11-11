# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Prepare an offline layout from an already built application; never builds or zips.

Full: application files + external models + installed receipt.
Update: payload application files + exact manifest + a standalone updater.
Run only at an authorized release point. This module itself does not release anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import runpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pocket_memory.safe_update import is_program_path, validate_manifest
excluded_resource = runpy.run_path(str(Path(__file__).with_name('release_policy.py')))['excluded_resource']


# OCR uses the independently inventoried models/rapidocr resources. These are
# redundant weights collected from the Python package, not its key dictionaries.
DUPLICATE_RAPIDOCR_WEIGHTS = frozenset({
    '_internal/rapidocr/models/ch_PP-OCRv4_det_infer.onnx',
    '_internal/rapidocr/models/ch_PP-OCRv4_rec_infer.onnx',
    '_internal/rapidocr/models/ch_ppocr_mobile_v2.0_cls_infer.onnx',
})


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def regular_files(root: Path):
    """Reject links/junctions rather than following them into personal folders."""
    def check(path):
        st = path.lstat()
        if path.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'资源中不可包含链接或联接点：{path}')
    check(root)
    for folder, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            check(Path(folder) / name)
        for name in names:
            path = Path(folder) / name
            check(path)
            if not path.is_file():
                raise ValueError(f'不是普通文件：{path}')
            yield path


def record(path: Path, relative: str) -> dict:
    return {'path': relative, 'size': path.stat().st_size, 'sha256': digest(path)}


def prepare(built_app: Path, model_source: Path, destination: Path, *,
            version: str, mode: str, updater_exe: Path | None = None,
            schema_id: str) -> dict:
    if mode not in ('full', 'update') or not version.strip() or not schema_id.strip():
        raise ValueError('需要有效的交付类型、版本和数据兼容标识。')
    built_app, model_source, destination = (p.absolute() for p in
                                            (built_app, model_source, destination))
    for source in (built_app, model_source):
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError('交付输出不能与输入目录相交。')
    if destination.exists():
        raise ValueError('交付目录已存在；不覆盖已有交付或用户目录。')
    if not updater_exe or not updater_exe.is_file():
        raise ValueError('交付需要已构建的内部 PocketMemoryUpdateWorker.exe。')
    if updater_exe.is_symlink() or getattr(updater_exe.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('更新组件不可为链接。')
    programs = []
    omitted = []
    for path in regular_files(built_app):
        rel = path.relative_to(built_app).as_posix()
        if rel == '_internal/PocketMemoryUpdateWorker.exe':
            continue
        if rel.startswith(('_internal/models/', 'models/')):
            continue
        if rel in DUPLICATE_RAPIDOCR_WEIGHTS or excluded_resource(rel):
            omitted.append(rel)
            continue
        try:
            allowed = is_program_path(rel)
        except Exception:
            allowed = False
        if allowed:
            programs.append((path, record(path, rel)))
        else:
            omitted.append(rel)
    if not any(item['path'] == 'PocketMemory.exe' for _, item in programs):
        raise ValueError('构建输入缺少 PocketMemory.exe。')
    if not any(item['path'].startswith('_internal/') for _, item in programs):
        raise ValueError('构建输入缺少配套运行资源。')
    models = []
    for path in regular_files(model_source):
        rel = path.relative_to(model_source).as_posix()
        if path.suffix.lower() in ('.part', '.tmp'):
            raise ValueError(f'模型资源包含未完成下载：{rel}')
        models.append((path, record(path, rel)))
    if not models or not any(item['path'].lower().endswith('.gguf') for _, item in models):
        raise ValueError('完整资源清单缺少 GGUF；不能生成会缺模型的更新包。')
    programs.append((updater_exe, record(updater_exe, '_internal/PocketMemoryUpdateWorker.exe')))
    files = [item for _, item in sorted(programs, key=lambda pair: pair[1]['path'])]
    model_records = [item for _, item in sorted(models, key=lambda pair: pair[1]['path'])]
    manifest = dict(schema_version=1, app_id='pocket-memory', kind='program-update',
                    version=version, files=files, model_requirements=model_records,
                    data_compat=dict(accepted_schema_ids=[schema_id],
                                     target_schema_id=schema_id, migration='none'))
    validate_manifest(manifest)
    # All reads/hashes succeed before a fresh destination is created.
    destination.mkdir(parents=True)
    program_root = destination / 'payload' if mode == 'update' else destination
    for source, item in programs:
        target = program_root / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if digest(target) != item['sha256']:
            raise ValueError(f'交付复制校验失败：{item["path"]}')
    if mode == 'full':
        for source, item in models:
            target = destination / 'models' / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if digest(target) != item['sha256']:
                raise ValueError(f'模型复制校验失败：{item["path"]}')
        # Keep schema declaration separate from the application's user config.
        receipt = destination / '.pocket-memory-update' / 'installed.json'
        receipt.parent.mkdir()
        receipt.write_text(json.dumps({'schema_version': 1, 'app_id': 'pocket-memory',
                                      'version': version, 'data_schema_id': schema_id},
                                     ensure_ascii=False, indent=2), encoding='utf-8')
        (destination / 'model-resources.json').write_text(
            json.dumps(model_records, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        (destination / 'update-manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (destination / '更新说明.txt').write_text(
            '此程序包供 Pocket Memory 应用内软件更新使用。\n'
            '应用发现新版后，点击“更新并重启”，下载、检查、安装和启动检查自动完成。\n'
            '沿用本机模型、笔记和配置，启动检查失败自动回退。无需选择目录。\n'
            '请勿手工覆盖 _internal。本包不包含模型，不是首次安装包。\n',
            encoding='utf-8')
    return {'mode': mode, 'version': version, 'program_files': len(files),
            'model_files': len(models), 'model_bytes_omitted':
            sum(item['size'] for _, item in models) if mode == 'update' else 0,
            'omitted_non_program_files': omitted, 'destination': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--built-app', type=Path, required=True)
    parser.add_argument('--model-source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--mode', choices=['full', 'update'], required=True)
    parser.add_argument('--updater-exe', type=Path)
    parser.add_argument('--schema-id', required=True,
                        help='Explicit verified no-migration compatibility identifier; never guessed.')
    args = parser.parse_args()
    print(json.dumps(prepare(args.built_app, args.model_source, args.destination,
                            version=args.version, mode=args.mode,
                            updater_exe=args.updater_exe, schema_id=args.schema_id),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
