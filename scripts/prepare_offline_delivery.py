# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Prepare an offline layout from an already built application; never builds or zips.

Full: application files + external models + installed receipt.
Update: payload application files + exact manifest declaring the installed executor.
Legacy bridge: an explicit update-only v1 envelope for a verified v2 build/worker.
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
from pocket_memory.safe_update import is_program_path, validate_manifest, _verify_components
from pocket_memory.update_component import WORKER_RELATIVE_PATH, require_worker
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
    def enumeration_error(error):
        raise error
    for folder, dirs, names in os.walk(root, followlinks=False, onerror=enumeration_error):
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


def verify_bridge_worker_receipt(path: Path, *, version: str, main_sha256: str,
                                 worker: dict) -> dict:
    """Bind a release runner's frozen-worker probe to these exact build bytes.

    This is a release validation receipt, not a signature or an independent
    proof of executable behavior. The release runner must obtain capabilities
    from the compiled worker and still test the actual legacy update path.
    """
    from pocket_memory.safe_update import _no_links, _read_json
    path = Path(path).absolute()
    _no_links(path)
    if path.stat().st_size > 64 * 1024:
        raise ValueError('桥接更新器验证回执过大。')
    receipt = _read_json(path)
    fields = {'schema_version', 'app_id', 'kind', 'version', 'verification',
              'main_sha256', 'worker', 'capabilities'}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or receipt['app_id'] != 'pocket-memory'
            or receipt['kind'] != 'legacy-bridge-worker-validation'
            or receipt['version'] != version
            or receipt['verification'] != 'frozen-worker-capability-probe'
            or receipt['main_sha256'] != main_sha256):
        raise ValueError('桥接更新器验证回执身份无效或未绑定当前主程序。')
    identity = receipt['worker']
    if (not isinstance(identity, dict) or set(identity) != {'size', 'sha256'}
            or type(identity['size']) is not int
            or identity['size'] != worker['size'] or identity['sha256'] != worker['sha256']):
        raise ValueError('桥接更新器验证回执与当前更新器字节不一致。')
    capability = receipt['capabilities']
    if (not isinstance(capability, dict)
            or set(capability) != {'schema_version', 'app_id', 'version', 'manifest_schemas', 'executor'}
            or type(capability['schema_version']) is not int or capability['schema_version'] != 1
            or capability['app_id'] != 'pocket-memory' or capability['version'] != version
            or capability['executor'] != 'standalone'
            or capability['manifest_schemas'] != [1, 2]
            or any(type(item) is not int for item in capability['manifest_schemas'])):
        raise ValueError('桥接更新器必须有本轮原生探针确认的 schema 1/2 能力。')
    return receipt


def prepare(built_app: Path, model_source: Path, destination: Path, *,
            version: str, mode: str, updater_exe: Path | None = None,
            schema_id: str, executor: str = 'main-exe-v1',
            bridge_worker_receipt: Path | None = None,
            verified_executable_hashes: dict | None = None) -> dict:
    if mode not in ('full', 'update') or not version.strip() or not schema_id.strip():
        raise ValueError('需要有效的交付类型、版本和数据兼容标识。')
    built_app, model_source, destination = (p.absolute() for p in
                                            (built_app, model_source, destination))
    for source in (built_app, model_source):
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError('交付输出不能与输入目录相交。')
    if destination.exists():
        raise ValueError('交付目录已存在；不覆盖已有交付或用户目录。')
    if executor not in ('main-exe-v1', 'standalone', 'legacy-bridge'):
        raise ValueError('交付必须明确使用 main-exe-v1、standalone 或 legacy-bridge 更新执行器。')
    if executor == 'legacy-bridge' and (mode != 'update' or bridge_worker_receipt is None):
        raise ValueError('legacy-bridge 仅用于 update，且必须提供本轮更新器验证回执。')
    if executor != 'legacy-bridge' and bridge_worker_receipt is not None:
        raise ValueError('更新器验证回执仅适用于显式 legacy-bridge 交付。')
    if executor in ('standalone', 'legacy-bridge') and updater_exe is None:
        raise ValueError('独立或桥接执行器交付需要已构建的 PocketMemoryUpdateWorker.exe。')
    if executor == 'main-exe-v1' and updater_exe is not None:
        raise ValueError('内置更新交付不接收独立更新器；旧版交付须显式选择 standalone。')
    worker = require_worker(updater_exe) if executor in ('standalone', 'legacy-bridge') else None
    if executor == 'standalone':
        from pocket_memory.embedded_update import CAPABILITY_PATH
        from pocket_memory.safe_update import _read_json
        marker = built_app / CAPABILITY_PATH
        if marker.exists() and _read_json(marker).get('schema_version') != 1:
            raise ValueError('standalone 仅用于旧版构建；新版能力声明不能重贴为旧协议发送给旧客户端。')
    programs = []
    omitted = []
    for path in regular_files(built_app):
        rel = path.relative_to(built_app).as_posix()
        if rel.casefold() == WORKER_RELATIVE_PATH.casefold():
            omitted.append(rel)
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
    if verified_executable_hashes is not None:
        expected = {'PocketMemory.exe'}
        actual = {item['path']: item['sha256'] for _, item in programs}
        if worker is not None:
            expected.add(WORKER_RELATIVE_PATH)
            actual[WORKER_RELATIVE_PATH] = worker['sha256']
        if (set(verified_executable_hashes) != expected or
                any(actual.get(path) != digest for path, digest in verified_executable_hashes.items())):
            raise ValueError('签名核验后程序文件发生变化或核验不完整；未创建交付目录。')
    models = []
    for path in regular_files(model_source):
        rel = path.relative_to(model_source).as_posix()
        if path.suffix.lower() in ('.part', '.tmp'):
            raise ValueError(f'模型资源包含未完成下载：{rel}')
        models.append((path, record(path, rel)))
    if not models or not any(item['path'].lower().endswith('.gguf') for _, item in models):
        raise ValueError('完整资源清单缺少 GGUF；不能生成会缺模型的更新包。')
    model_records = [item for _, item in sorted(models, key=lambda pair: pair[1]['path'])]
    generated_files = {}
    if executor == 'legacy-bridge':
        from pocket_memory.embedded_update import CAPABILITY_PATH, validate_capability
        from pocket_memory.safe_update import _read_json
        # Validate the original v2 build first. Only the copied bridge payload
        # receives the older three-field marker; the built app stays unchanged.
        source_manifest = dict(schema_version=2, app_id='pocket-memory', kind='program-update',
                               version=version, files=[item for _, item in programs],
                               model_requirements=model_records, update_executor='main-exe-v1',
                               data_compat=dict(accepted_schema_ids=[schema_id],
                                                target_schema_id=schema_id, migration='none'))
        validate_capability(built_app, source_manifest)
        require_worker(built_app / 'PocketMemory.exe')
        main_sha256 = next(item['sha256'] for _, item in programs if item['path'] == 'PocketMemory.exe')
        verify_bridge_worker_receipt(bridge_worker_receipt, version=version,
                                     main_sha256=main_sha256, worker=worker)
        original_marker = _read_json(built_app / CAPABILITY_PATH)
        marker = dict(schema_version=1, executor='main-exe-v1', python_dll=original_marker['python_dll'])
        raw = json.dumps(marker, separators=(',', ':')).encode('utf-8') + b'\n'
        generated_files[CAPABILITY_PATH] = raw
        replacement = {'path': CAPABILITY_PATH, 'size': len(raw),
                       'sha256': hashlib.sha256(raw).hexdigest()}
        programs = [(source, replacement if item['path'] == CAPABILITY_PATH else item)
                    for source, item in programs]
    if worker is not None:
        programs.append((updater_exe, {'path': WORKER_RELATIVE_PATH,
                                     'size': worker['size'], 'sha256': worker['sha256']}))
    files = [item for _, item in sorted(programs, key=lambda pair: pair[1]['path'])]
    manifest = dict(schema_version=2 if executor == 'main-exe-v1' else 1,
                    app_id='pocket-memory', kind='program-update',
                    version=version, files=files, model_requirements=model_records,
                    data_compat=dict(accepted_schema_ids=[schema_id],
                                     target_schema_id=schema_id, migration='none'))
    if executor == 'main-exe-v1':
        manifest['update_executor'] = executor
    validate_manifest(manifest)
    if executor == 'main-exe-v1':
        # Check the compiled runtime declaration before creating any delivery.
        # The built input may include excluded build resources; the final copied
        # program inventory is checked again below after those are filtered out.
        from pocket_memory.embedded_update import validate_capability
        validate_capability(built_app, manifest)
        require_worker(built_app / 'PocketMemory.exe')
    # All reads/hashes succeed before a fresh destination is created.
    destination.mkdir(parents=True)
    program_root = destination / 'payload' if mode == 'update' else destination
    for source, item in programs:
        target = program_root / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if item['path'] in generated_files:
            target.write_bytes(generated_files[item['path']])
        else:
            shutil.copy2(source, target)
        if digest(target) != item['sha256']:
            raise ValueError(f'交付复制校验失败：{item["path"]}')
    _verify_components(program_root, manifest)
    if worker is not None:
        copied_worker = require_worker(program_root / WORKER_RELATIVE_PATH)
        if any(copied_worker[key] != worker[key] for key in ('size', 'sha256')):
            raise ValueError('交付更新组件复制校验失败。')
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
                                      'version': version, 'data_schema_id': schema_id,
                                      'manifest': manifest},
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
    result = {'mode': mode, 'version': version, 'executor': executor,
            'manifest_schema': manifest['schema_version'], 'program_files': len(files),
            'model_files': len(models), 'model_bytes_omitted':
            sum(item['size'] for _, item in models) if mode == 'update' else 0,
            'omitted_non_program_files': omitted, 'destination': str(destination)}
    if executor == 'legacy-bridge':
        result['bridge_worker_receipt_sha256'] = digest(bridge_worker_receipt)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--built-app', type=Path, required=True)
    parser.add_argument('--model-source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--mode', choices=['full', 'update'], required=True)
    parser.add_argument('--updater-exe', type=Path)
    parser.add_argument('--executor', choices=['main-exe-v1', 'standalone', 'legacy-bridge'], default='main-exe-v1',
                        help='Default: v2; standalone: old builds; legacy-bridge: verified v2 build in an update-only v1 envelope.')
    parser.add_argument('--bridge-worker-receipt', type=Path,
                        help='Release validation receipt binding a compiled v2-capable worker to the exact main EXE.')
    parser.add_argument('--schema-id', required=True,
                        help='Explicit verified no-migration compatibility identifier; never guessed.')
    parser.add_argument('--expected-signer-thumbprint',
                        help='Optional: require this publisher SHA1 thumbprint, valid signatures and timestamps before creating manifests. Omission does not skip manifest or integrity checks.')
    args = parser.parse_args(argv)
    from scripts.release_signatures import verify_release_signatures
    if (args.executor == 'main-exe-v1' and args.updater_exe is not None) or (
            args.executor != 'main-exe-v1' and args.updater_exe is None):
        parser.error('更新执行器与输入组件不一致。')
    signature_report = None
    verified = None
    # A supplied publisher is an explicit opt-in, including an invalid/empty
    # value: verification must fail rather than silently omit the requested gate.
    if args.expected_signer_thumbprint is not None:
        signature_report = verify_release_signatures(args.built_app / 'PocketMemory.exe',
                                                    args.expected_signer_thumbprint, args.updater_exe)
        checks = {str(Path(row['file']).absolute()): row['sha256'] for row in signature_report['files']}
        verified = {'PocketMemory.exe': checks[str((args.built_app / 'PocketMemory.exe').absolute())]}
        if args.updater_exe is not None:
            verified[WORKER_RELATIVE_PATH] = checks[str(args.updater_exe.absolute())]
    result = prepare(args.built_app, args.model_source, args.destination,
                            version=args.version, mode=args.mode,
                            updater_exe=args.updater_exe, schema_id=args.schema_id,
                            executor=args.executor, bridge_worker_receipt=args.bridge_worker_receipt,
                            verified_executable_hashes=verified)
    result['release_signatures'] = signature_report if signature_report is not None else {
        'requested': False, 'passed': None, 'endpoint_policy_verified': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
