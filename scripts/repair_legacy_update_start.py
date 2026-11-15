# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""One-time 1.0.0 starter. No downloads, program replacement, or global settings.

Place the frozen helper beside the OLD PocketMemory.exe. It starts that same
installation with equivalent long environment paths so its existing updater can
download 1.0.1. Source --install/--check-only are for isolated acceptance only.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pocket_memory.safe_update import _absolute, _relative, _no_links, running_install_processes, UpdateError
from pocket_memory.desktop_runtime import ensure_native_runtime_config


def prepare(install, environment):
    install = _absolute(install)
    executable = _absolute(install/'PocketMemory.exe')
    if not executable.is_file():
        raise UpdateError('请把此工具放在原 PocketMemory.exe 旁边，再双击运行。')
    env = dict(environment)
    for key in ('TEMP', 'TMP', 'LOCALAPPDATA', 'PM_UPDATE_CONFIG_PATH'):
        if env.get(key):
            env[key] = str(_absolute(env[key]))
    if not env.get('LOCALAPPDATA'):
        raise UpdateError('未找到原用户配置目录，已停止；未修改任何资料。')
    configs = [install/'config.json', install/'beta-profile/PocketMemory/config.json',
               Path(env['LOCALAPPDATA'])/'PocketMemory/config.json']
    active = Path(env.get('PM_UPDATE_CONFIG_PATH', str(configs[-1])))
    configs.append(active)
    for config in set(configs):
        config = _absolute(config)
        if not config.is_file():continue
        if config.stat().st_size > 1024*1024:raise UpdateError('原配置文件过大，未修改。')
        value = json.loads(config.read_text('utf-8'))
        data = value.get('data_dir')
        if data:
            # The old updater also re-reads raw config paths. Do not rewrite
            # user config silently or pretend a temporary config avoids it.
            for part in Path(os.path.abspath(data)).parts[1:]:
                try:_relative(part)
                except UpdateError as exc:
                    raise UpdateError('原配置中的资料路径也使用短名或特殊名称。为保护原配置，本工具已停止；请使用新完整包并选择原资料目录。') from exc
    env['PM_UPDATE_CONFIG_PATH'] = str(_absolute(active))
    return executable, env


def run(install, *, check_only=False):
    executable, env = prepare(install, os.environ)
    if check_only:return {'state':'checked', 'install':str(executable.parent), 'exe':str(executable)}
    log_path = executable.parent/'PocketMemory-update-start-repair.log'
    _no_links(log_path)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    with log_path.open('ab') as log:
        result = subprocess.run([str(executable), '--quit'], cwd=executable.parent,
                                env=env, stdout=log, stderr=log, creationflags=flags, timeout=35)
        if result.returncode:
            raise UpdateError('原后台尚未正常退出，请关闭 Pocket Memory 后重试。')
        deadline = time.monotonic()+30
        while running_install_processes(executable.parent):
            if time.monotonic() >= deadline:
                raise UpdateError('原窗口或后台仍在运行，请关闭窗口后重试；本工具没有强制结束进程。')
            time.sleep(.3)
        ensure_native_runtime_config(executable)
        process = subprocess.Popen([str(executable)], cwd=executable.parent, env=env,
                                   stdout=log, stderr=log, creationflags=flags)
    return {'state':'restarted', 'pid':process.pid, 'install':str(executable.parent),
            'message':'已从原位置重新启动。请在设置 → 软件更新中重试；程序尚未由此工具更新。'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--install',type=Path,default=Path(sys.executable).parent)
    parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    try:
        result=run(args.install,check_only=args.check_only)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as exc:
        if getattr(sys,'frozen',False):ctypes.windll.user32.MessageBoxW(None,str(exc),'Pocket Memory 启动修复',0x10)
        else:print(str(exc),file=sys.stderr)
        return 1

if __name__=='__main__':raise SystemExit(main())
