# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Shared installed/built updater checks; never load or execute the component.

PE checks detect missing, truncated and wrong-format files, not publisher trust.
SHA256 lets delivery code compare the built, copied and archived bytes.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
import stat

WORKER_RELATIVE_PATH = '_internal/PocketMemoryUpdateWorker.exe'
REQUIRED_COMPONENT_PATHS = frozenset({WORKER_RELATIVE_PATH})


def recovery_guidance(result: dict) -> dict:
    """One recovery route for the whole installation; never distribute loose EXEs."""
    reasons = {'missing': '缺少自动更新组件', 'not_file': '自动更新组件路径不是文件',
               'unreadable': '无法读取自动更新组件，可能被占用或权限不足',
               'unsafe_path': '自动更新组件位于不支持的链接路径',
               'invalid_executable': '自动更新组件不完整或格式异常',
               'integrity_mismatch': '自动更新组件与安装记录不一致',
               'invalid_receipt': '安装完整性记录无法核对'}
    return {'action': 'replace_full_installation',
            'message': '当前安装：' + reasons.get(result.get('status'), '组件完整性检查未通过') + '，无法自动更新。',
            'steps': ['先保存笔记，在“设置 → 数据”确认资料库位置并导出完整备份。',
                      '从托盘完全退出程序，将官方完整包完整解压到新文件夹后启动。不要覆盖或删除旧安装目录。',
                      '在新程序中选择原资料库，确认笔记和附件正常后再停用旧目录；无需逐个补文件。'],
            'path': result.get('path', '')}


def component_error(result: dict) -> str:
    guide = recovery_guidance(result)
    return guide['message'] + ''.join(guide['steps']) + '当前检查位置：' + guide['path']


def inspect_installed_worker(install: Path) -> dict:
    result = inspect_worker(Path(install) / WORKER_RELATIVE_PATH)
    if result['status'] != 'ok':
        return result
    # A full ZIP need not have a transaction receipt. If a receipt exists, it
    # must be readable and its component inventory must match; never fall back
    # to PE-only validation when a receipt is damaged or omits the component.
    from pocket_memory.safe_update import _read_json, validate_manifest, UpdateError
    receipt = Path(install) / '.pocket-memory-update/installed.json'
    try:
        receipt.lstat()
    except FileNotFoundError:
        result['verification'] = 'executable_structure'
        return result
    except OSError:
        result['status'] = 'invalid_receipt'
        return result
    try:
        manifest = validate_manifest(_read_json(receipt)['manifest'])
        entry = next(x for x in manifest['files'] if x['path'] == WORKER_RELATIVE_PATH)
        if (result['size'], result['sha256']) != (entry['size'], entry['sha256']):
            result['status'] = 'integrity_mismatch'
        result['verification'] = 'installed_manifest'
    except (OSError, ValueError, KeyError, TypeError, StopIteration, UpdateError):
        result['status'] = 'invalid_receipt'
    return result


def runtime_integrity() -> dict:
    if not getattr(sys, 'frozen', False):
        return {'status': 'not_applicable', 'verification': 'source_checkout'}
    return inspect_installed_worker(Path(sys.executable).parent)


def inspect_worker(path: Path) -> dict:
    path = Path(os.path.abspath(path))  # Do not resolve away symlinks/junctions.
    result = {'status': 'missing', 'path': str(path), 'size': None, 'sha256': None}
    try:
        for item in reversed((path, *path.parents)):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
                result.update(status='unsafe_path', detail='link/reparse point or hardlink', unsafe_path=str(item))
                return result
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            result.update(status='not_file', detail='component path is not a regular file')
            return result
        with path.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            result['size'] = size
            dos = stream.read(64)
            if len(dos) != 64 or dos[:2] != b'MZ':
                raise ValueError('missing or truncated MZ header')
            offset = int.from_bytes(dos[0x3c:0x40], 'little')
            if offset < 64 or offset > size - 24:
                raise ValueError('PE header offset outside file')
            stream.seek(offset)
            coff = stream.read(24)
            if len(coff) != 24 or coff[:4] != b'PE\0\0':
                raise ValueError('invalid PE signature')
            if int.from_bytes(coff[4:6], 'little') != 0x8664:
                raise ValueError('component is not AMD64')
            optional = int.from_bytes(coff[20:22], 'little')
            if optional < 112 or offset + 24 + optional > size or stream.read(2) != b'\x0b\x02':
                raise ValueError('missing or truncated PE32+ optional header')
            stream.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            # Do not accept a file changed while it was inspected.
            after = path.stat()
            if (after.st_size, after.st_mtime_ns, after.st_ino) != (info.st_size, info.st_mtime_ns, info.st_ino):
                result.update(status='unreadable', detail='component changed during inspection')
                return result
            result.update(status='ok', sha256=digest.hexdigest())
    except FileNotFoundError:
        result.update(status='missing', detail='component or parent directory missing')
    except (PermissionError, OSError) as exc:
        result.update(status='unreadable', detail=type(exc).__name__, errno=exc.errno,
                      winerror=getattr(exc, 'winerror', None))
    except ValueError as exc:
        result.update(status='invalid_executable', detail=str(exc))
    return result


def require_worker(path: Path) -> dict:
    result = inspect_worker(path)
    if result['status'] != 'ok':
        raise ValueError(f"更新组件检查未通过（{result['status']}）：{result['path']}；"
                         '必须包含完整的 PocketMemoryUpdateWorker.exe，不能交付缺件包。')
    return result
