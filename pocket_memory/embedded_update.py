# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Receipt-verified built-in executor, with explicit v2 worker-free delivery.

The downloaded update supplies no executable hooks. Only the currently installed
program is copied and executed; normal update transaction guards remain in force.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys

CAPABILITY_PATH = '_internal/update-runtime.json'
EMBEDDED_FLAG = '--internal-update-worker'


def validate_capability(root: Path, manifest):
    from .safe_update import _read_json, _verify, validate_manifest, UpdateError
    root = Path(root)
    manifest = validate_manifest(manifest)
    entries = {e['path']: e for e in manifest['files']}
    if CAPABILITY_PATH not in entries:
        raise UpdateError('当前安装没有内置更新能力声明')
    _verify(root / CAPABILITY_PATH, entries[CAPABILITY_PATH])
    capability = _read_json(root / CAPABILITY_PATH)
    capability_schema = capability.get('schema_version')
    fields = {'schema_version', 'executor', 'python_dll'}
    if capability_schema == 2:
        fields.add('manifest_schemas')
    if (set(capability) != fields
            or type(capability_schema) is not int or capability_schema not in (1, 2)
            or capability['executor'] != 'main-exe-v1'
            or not isinstance(capability['python_dll'], str)
            or not re.fullmatch(r'python3\d{1,2}\.dll', capability['python_dll'])):
        raise UpdateError('内置更新能力声明无效')
    if capability_schema == 2 and (capability['manifest_schemas'] != [1, 2]
                                   or any(type(v) is not int for v in capability['manifest_schemas'])):
        raise UpdateError('内置更新清单协议能力声明无效')
    if manifest['schema_version'] == 2 and capability_schema != 2:
        raise UpdateError('主程序尚未声明支持新版内置更新协议；不能把旧程序重编清单后作为新版交付')
    required = {'PocketMemory.exe', '_internal/base_library.zip',
                '_internal/' + capability['python_dll']}
    if not required.issubset(entries):
        raise UpdateError('内置更新缺少完整主程序运行库清单')
    return manifest


def verify_runtime(root: Path, manifest=None):
    from .safe_update import _no_links, _read_json, _verify, UpdateError
    from .update_component import WORKER_RELATIVE_PATH, inspect_worker
    root = Path(root)
    _no_links(root)
    if manifest is None:
        receipt = root / '.pocket-memory-update/installed.json'
        _no_links(receipt)
        envelope = _read_json(receipt)
        manifest = envelope['manifest']
        if not isinstance(manifest, dict):
            raise UpdateError('内置更新安装清单必须是对象')
        if (type(envelope.get('schema_version')) is not int or envelope['schema_version'] != 1
                or envelope.get('app_id') != 'pocket-memory'
                or envelope.get('version') != manifest.get('version')
                or envelope.get('data_schema_id') != manifest.get('data_compat', {}).get('target_schema_id')):
            raise UpdateError('内置更新安装记录不完整或身份不一致')
    manifest = validate_capability(root, manifest)
    entries = {e['path']: e for e in manifest['files']}
    worker = inspect_worker(root / WORKER_RELATIVE_PATH)
    if worker['status'] != 'missing':
        raise UpdateError('备用更新只适用于独立组件确实缺失，不接受损坏或不可读组件')
    for relative, entry in entries.items():
        if manifest['schema_version'] == 2 or relative != WORKER_RELATIVE_PATH:
            _verify(root / relative, entry)
    if inspect_worker(root / 'PocketMemory.exe')['status'] != 'ok':
        raise UpdateError('主程序不是完整的 Windows 可执行文件')
    # Refuse undeclared code/libraries; user data/models outside _internal are not read.
    internal = root / '_internal'
    def enumeration_error(error):
        raise error
    for folder, dirs, files in os.walk(internal, followlinks=False, onerror=enumeration_error):
        for name in [*dirs, *files]:
            path = Path(folder) / name
            _no_links(path)
            if path.is_file() and path.relative_to(root).as_posix() not in entries:
                raise UpdateError('运行库包含安装清单以外的文件')
    return manifest


def inspect_executor(root: Path) -> dict:
    from .update_component import inspect_installed_worker
    from .safe_update import UpdateError
    diagnostic = inspect_installed_worker(root)
    if diagnostic['status'] == 'ok':
        return dict(diagnostic, executor='standalone')
    if diagnostic['status'] != 'missing':
        return diagnostic
    try:
        manifest = verify_runtime(root)
    except (OSError, ValueError, KeyError, TypeError, UpdateError) as exc:
        return dict(diagnostic, fallback_error=str(exc))
    declared = manifest['schema_version'] == 2
    return dict(status='ok', executor='embedded', verification='installed_manifest_full_runtime',
                path=str(Path(root) / 'PocketMemory.exe'),
                legacy_component_status='not_required' if declared else 'missing',
                verified_files=len(manifest['files']) - (0 if declared else 1),
                message='已验证主程序内置更新方式。' if declared else '独立更新组件缺失，已验证并启用主程序内置更新方式。')


def stage_executor(root: Path, work: Path):
    """Return (command prefix, executable), revalidating at handoff time."""
    from .safe_update import _no_links, _overlap, _verify, _atomic_json, _hash, UpdateError
    from .update_component import WORKER_RELATIVE_PATH
    root, work = Path(root), Path(work)
    _no_links(root); _no_links(work)
    if _overlap(root, work):
        raise UpdateError('更新执行副本不能位于安装目录内')
    diagnostic = inspect_executor(root)
    if diagnostic['status'] != 'ok':
        raise UpdateError('更新执行器校验失败：' + diagnostic['status'])
    if diagnostic['executor'] == 'standalone':
        source = root / WORKER_RELATIVE_PATH
        target = work / 'PocketMemoryUpdateWorker.exe'
        _no_links(target)
        if target.exists():
            raise UpdateError('更新执行副本已存在')
        shutil.copy2(source, target)
        if _hash(target) != diagnostic['sha256']:
            raise UpdateError('自动更新组件复制校验失败')
        return [str(target)], target
    manifest = verify_runtime(root)
    entries = [e for e in manifest['files'] if e['path'] != WORKER_RELATIVE_PATH]
    required_bytes = sum(e['size'] for e in entries)
    if shutil.disk_usage(work).free < required_bytes + 64 * 1024 * 1024:
        raise UpdateError('临时磁盘空间不足，无法准备完整更新执行副本；原程序未替换')
    snapshot = work / 'installed-runtime'
    _no_links(snapshot)
    snapshot.mkdir(exist_ok=False)
    for entry in entries:
        target = snapshot / entry['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / entry['path'], target)
        _verify(target, entry)
    (snapshot / '.pocket-memory-update').mkdir()
    _atomic_json(snapshot / '.pocket-memory-update/installed.json', {
        'schema_version': 1, 'app_id': 'pocket-memory', 'version': manifest['version'],
        'data_schema_id': manifest['data_compat']['target_schema_id'], 'manifest': manifest})
    verify_runtime(snapshot)
    target = snapshot / 'PocketMemory.exe'
    return [str(target), EMBEDDED_FLAG], target


def clean_worker_environment():
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith('_PYI_') or key in {'_MEIPASS2', 'PM_UPDATE_HEALTH_TOKEN', 'PM_UPDATE_PROBE'}:
            env.pop(key, None)
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return env


def main(argv=None):
    """Early entry, before product/server/desktop imports. Frozen runtime only."""
    from .safe_update import _absolute, _overlap, UpdateError
    from scripts.update_worker import load_request, main as worker_main
    args = list(sys.argv[2:] if argv is None else argv)
    if not getattr(sys, 'frozen', False) or len(args) != 2 or args[0] != '--request':
        raise UpdateError('内置更新入口仅接受已安装程序的内部请求')
    request_path = _absolute(args[1])
    request = load_request(request_path)
    snapshot = _absolute(sys.executable).parent
    if (snapshot != request_path.parent / 'installed-runtime'
            or _absolute(getattr(sys, '_MEIPASS', '')) != snapshot / '_internal'
            or _overlap(snapshot, Path(request['install_dir']))
            or _overlap(snapshot, Path(request['data_dir']))):
        raise UpdateError('内置更新必须使用安装目录外的独立运行副本')
    verify_runtime(snapshot)
    return worker_main(args)
