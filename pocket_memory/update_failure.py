# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Failure facts and an allowlisted diagnostic. Never repairs or executes files."""
from __future__ import annotations

from .safe_update import UpdateError

REASONS = {
    'missing': '更新所需文件缺失。',
    'unreadable': '更新所需文件无法读取，可能涉及权限或占用。',
    'not_file': '更新组件路径不是普通文件。',
    'unsafe_path': '更新路径校验未通过。',
    'invalid_executable': '更新组件文件不完整或格式异常。',
    'integrity_mismatch': '文件大小或 SHA256 与清单记录不一致。',
    'inventory_mismatch': '更新文件集合与清单不一致，存在缺件或额外文件。',
    'invalid_receipt': '安装完整性记录无法核验。',
    'invalid_hold': '上次更新失败记录无法核验。',
    'handshake_failed': '更新准备未获得有效确认，未请求关闭当前程序。',
    'validation_failed': '更新安全校验未通过。',
    'tls_validation_failed': '无法验证更新服务器的安全连接。',
    'update_failed': '本次更新处理失败，原因仍需核对。',
    'previous_attempt_failed': '上次更新记录显示未完成，需先核对失败或回滚结果。',
}
STAGES = {'startup', 'check', 'download', 'verify', 'prepare', 'handshake'}
STEPS = [
    '本次自动更新已暂停；可继续使用笔记，先保存编辑并保留原安装、资料、模型及更新备份。',
    '导出下方的最小诊断供开发者核对。诊断只含更新状态，不含笔记、用户名、安装路径或令牌。',
    '若有安全软件提示，请保留提示交由管理方核验；确认原因和合规处理方式前，不重复下载、补回隔离文件或强行执行。',
]


def failure(code='update_failed', stage='check', exc=None):
    result = {'code': code if code in REASONS else 'update_failed',
              'stage': stage if stage in STAGES else 'check'}
    if isinstance(exc, OSError):
        for key in ('errno', 'winerror'):
            value = getattr(exc, key, None)
            if type(value) is int:
                result[key] = value
    return result


def from_exception(exc, stage):
    # Use typed failure facts, not arbitrary messages or guesses about security tools.
    code = getattr(exc, 'failure_code', None)
    if code in REASONS:
        return failure(code, stage, exc if isinstance(exc, OSError) else exc.__cause__)
    if isinstance(exc, FileNotFoundError):
        return failure('missing', stage, exc)
    if isinstance(exc, OSError):
        return failure('unreadable', stage, exc)
    if isinstance(exc.__cause__, OSError):
        return failure('missing' if isinstance(exc.__cause__, FileNotFoundError) else 'unreadable', stage, exc.__cause__)
    if stage in {'prepare', 'handshake'}:
        return failure('handshake_failed', stage)
    return failure('validation_failed' if isinstance(exc, UpdateError) else 'update_failed', stage)


def guidance(record):
    return {'action': 'pause_and_diagnose',
            'message': REASONS.get(record.get('code'), REASONS['update_failed']) +
                       ' 已停止重复下载和执行；仅凭此结果不能确认是否由安全软件拦截。',
            'steps': list(STEPS)}


def diagnostic(state):
    """Construct from fixed fields; NEVER serialize raw status, exceptions or logs."""
    record = state.get('update_failure') or {}
    version = str(state.get('current_version', ''))
    import re
    out = {'schema_version': 1, 'app': 'PocketMemory',
           'current_version': version if re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?', version) else 'unknown',
           'automatic_retry_paused': state.get('retry_blocked') is True,
           'cause': 'not_determined', 'security_product_confirmed': False}
    if record:
        out['failure'] = failure(record.get('code'), record.get('stage'))
        for key in ('errno', 'winerror'):
            if type(record.get(key)) is int:
                out['failure'][key] = record[key]
    component = state.get('installation_integrity') or {}
    out['component'] = 'embedded_runtime' if component.get('executor') == 'embedded' else 'update_executor'
    out['component_status'] = component.get('status') if component.get('status') in set(REASONS) | {'ok', 'not_applicable'} else 'unknown'
    transaction = (state.get('transaction_recovery') or {}).get('state')
    out['transaction_state'] = transaction if transaction in {'prepared', 'applying', 'pending_health', 'rolling_back', 'invalid'} else 'no_pending_observed'
    out['failure_record_saved'] = state.get('failure_record_saved') is True
    return out
