# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Optional maintainer-side Authenticode gate; never modifies trust or signs binaries.

This is a release quality check, not endpoint-policy approval or a replacement
for the installed updater's exact manifest validation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_release_signatures(program: Path, expected_signer: str, worker: Path | None = None) -> dict:
    if not re.fullmatch(r'[a-fA-F0-9]{40}', expected_signer):
        raise ValueError('需要明确的正式发布者证书指纹（40 位十六进制），不能自动信任任意签名。')
    targets = [Path(program).absolute()]
    if worker is not None:
        targets.append(Path(worker).absolute())
    if len(set(targets)) != len(targets):
        raise ValueError('主程序和更新组件必须是不同的输入文件。')
    if os.name != 'nt':
        raise ValueError('Windows 发布必须在 Windows 上验证 Authenticode；未执行交付。')
    before = {str(path): file_hash(path) for path in targets}
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = Path(__file__).with_name('check_release_signatures.ps1')
    with tempfile.TemporaryDirectory(prefix='PocketMemory-signature-check-') as temporary:
        report_path = Path(temporary) / 'signatures.json'
        command = [str(shell), '-NoProfile', '-NonInteractive', '-File', str(script),
                   '-ProgramExe', str(targets[0]), '-ExpectedSignerThumbprint', expected_signer,
                   '-OutputPath', str(report_path)]
        if worker is not None:
            command += ['-LegacyWorker', str(targets[1])]
        try:
            result = subprocess.run(command, capture_output=True, timeout=90,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('发布签名检查未完成，未执行交付。') from exc
        if result.returncode != 0 or not report_path.is_file():
            raise ValueError('发布签名检查未通过：请先使用获授权的代码签名证书签名并加时间戳；未执行交付。')
        try:
            report = json.loads(report_path.read_text(encoding='utf-8-sig'))
            rows = report['files']
            if (report['passed'] is not True or not isinstance(rows, list) or len(rows) != len(targets)
                    or report['expected_signer_thumbprint'].upper() != expected_signer.upper()):
                raise ValueError('发布签名检查结果不完整。')
            observed = set()
            for row in rows:
                path = str(Path(row['file']).absolute())
                if (path not in before or path in observed or row['passed'] is not True
                        or row['signature_status'] != 'Valid' or row['timestamped'] is not True
                        or row['signer_thumbprint'].upper() != expected_signer.upper()
                        or row['sha256'] != before[path] or file_hash(Path(path)) != before[path]):
                    raise ValueError('发布签名或输入文件在检查时发生变化；未执行交付。')
                observed.add(path)
        except (OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError('发布签名结果或文件身份无法核验，未执行交付。') from exc
    # The caller binds these verified bytes to its copied manifest records.
    return report
