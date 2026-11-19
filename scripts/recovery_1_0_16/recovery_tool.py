# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Offline, one-time program recovery using the existing update transaction.

Freeze with source/ on the import path and recovery-release.json embedded at
the bundle root. The matching program ZIP is beside this tool, not inside the
old installation. There is deliberately no external trust-file CLI argument.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
import tempfile
import time

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'source'))

from pocket_memory.app_updates import extract_update_archive
from pocket_memory.embedded_update import clean_worker_environment
from pocket_memory.safe_update import (
    SafeUpdater, UpdateError, _absolute, _atomic_json, _hash, _no_links,
    _overlap, _read_json, running_install_processes,
)
from scripts import update_worker as worker
from scripts.repair_installed_worker import stop_original

_SHA = re.compile(r'[0-9a-f]{64}')


def local_path(value) -> Path:
    path = Path(value)
    if (not path.is_absolute() or str(value).replace('/', '\\').startswith('\\\\')):
        raise UpdateError('需要本机绝对路径，不能使用网络或相对路径。')
    _no_links(path)
    return _absolute(path)


def tool_directory() -> Path:
    return local_path(Path(sys.executable if getattr(sys, 'frozen', False) else __file__).parent)


def validate_release(value: dict) -> dict:
    fields = {'schema_version', 'app_id', 'version', 'package', 'allowed_previous'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['app_id'] != 'pocket-memory' or value['version'] != '1.0.16'):
        raise UpdateError('修复工具内嵌发布身份无效。')
    package = value['package']
    if (not isinstance(package, dict) or set(package) != {'filename', 'size', 'sha256'}
            or not isinstance(package['filename'], str)
            or not re.fullmatch(r'PocketMemory-[A-Za-z0-9._-]+\.zip', package['filename'])
            or type(package['size']) is not int or not 0 < package['size'] <= 1024 ** 3
            or not isinstance(package['sha256'], str) or not _SHA.fullmatch(package['sha256'])):
        raise UpdateError('修复工具内嵌程序包身份无效。')
    previous = value['allowed_previous']
    if not isinstance(previous, list) or not 1 <= len(previous) <= 16:
        raise UpdateError('修复工具缺少原版本白名单。')
    hashes = set()
    for entry in previous:
        if (not isinstance(entry, dict) or set(entry) != {'version', 'sha256'}
                or entry['version'] not in {'1.0.13', '1.0.14'}
                or not isinstance(entry['sha256'], str) or not _SHA.fullmatch(entry['sha256'])
                or entry['sha256'] in hashes):
            raise UpdateError('修复工具原版本白名单无效。')
        hashes.add(entry['sha256'])
    return value


def load_release() -> dict:
    if not getattr(sys, 'frozen', False) or not getattr(sys, '_MEIPASS', None):
        raise UpdateError('请使用已冻结并内嵌发布身份的修复工具；不接受外部发布JSON。')
    path = local_path(Path(sys._MEIPASS) / 'recovery-release.json')
    if path.stat().st_size > 64 * 1024:
        raise UpdateError('内嵌发布身份过大。')
    return validate_release(_read_json(path))


def default_config_path() -> Path:
    configured = os.environ.get('PM_UPDATE_CONFIG_PATH')
    if configured:
        return local_path(configured)
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise UpdateError('未找到原配置；请指定原有config.json。')
    return local_path(Path(local) / 'PocketMemory' / 'config.json')


def read_context(install_dir, config_path, release: dict) -> dict:
    install, config = local_path(install_dir), local_path(config_path)
    if _overlap(tool_directory(), install):
        raise UpdateError('修复工具必须位于旧安装目录之外；不要放在原EXE旁运行。')
    main = install / 'PocketMemory.exe'
    _no_links(main)
    if not main.is_file():
        raise UpdateError('所选目录没有PocketMemory.exe。')
    main_sha = _hash(main)
    previous = next((item for item in release['allowed_previous'] if item['sha256'] == main_sha), None)
    if previous is None:
        raise UpdateError('原EXE不属于本工具已核验的1.0.13/1.0.14；未改动程序。')
    if not config.is_file() or config.stat().st_size > 1024 * 1024:
        raise UpdateError('原配置不存在或过大；不会创建新配置。')
    value = _read_json(config)
    if not isinstance(value, dict) or not isinstance(value.get('data_dir'), str) or not value['data_dir']:
        raise UpdateError('原配置未明确记录资料库位置；不会猜测或创建资料库。')
    data = local_path(value['data_dir'])
    if not data.is_dir() or _overlap(data, config) or _overlap(data, tool_directory()):
        raise UpdateError('原资料目录无效或与配置/修复工具位置重叠。')
    database = data / 'pocket_memory.db'
    _no_links(database)
    if not database.is_file():
        raise UpdateError('原资料库数据库不存在。')
    # Only establish the persisted schema before closing the old app. Do not
    # create SQLite sidecars or interpret live WAL contents as a saved backup.
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'").fetchone():
            raise UpdateError('原资料库的notes表无法确认。')
    return dict(install_dir=str(install), config_path=str(config), data_dir=str(data),
                old_version=previous['version'], original_main_sha256=main_sha,
                original_config_sha256=_hash(config))


def new_work(protected=()) -> Path:
    candidates = []
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(Path(os.environ['LOCALAPPDATA']) / 'PocketMemoryRecovery')
    candidates.append(Path(tempfile.gettempdir()) / 'PocketMemoryRecovery')
    for candidate in candidates:
        root = local_path(candidate)
        if any(_overlap(root, local_path(path)) for path in protected):
            continue
        root.mkdir(parents=True, exist_ok=True)
        return local_path(tempfile.mkdtemp(prefix='attempt-', dir=root))
    raise UpdateError('未找到不与原程序/资料重叠的本地诊断目录。')


class Attempt:
    def __init__(self, work: Path):
        self.work = work
        self.history = []
        self.log = logging.getLogger('recovery-' + work.name)
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        handler = logging.FileHandler(work / 'recovery.log', encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        self.log.addHandler(handler)

    def record(self, state, message, **details):
        self.history.append(dict(state=state, time=time.time()))
        value = dict(state=state, message=message, log_dir=str(self.work),
                     history=list(self.history), **details)
        _atomic_json(self.work / 'recovery-result.json', value)
        self.log.info('%s: %s', state, message)
        return value

    def close(self):
        for handler in tuple(self.log.handlers):
            handler.close()
            self.log.removeHandler(handler)


def no_pending(updater: SafeUpdater):
    journal = updater._journal()
    if journal and journal['state'] not in {'committed', 'rolled_back'}:
        raise UpdateError('原安装存在未完成的更新事务；本工具未恢复或覆盖它，请保留原备份。')


def revalidate(prepared):
    fresh = read_context(prepared['context']['install_dir'], prepared['context']['config_path'], prepared['release'])
    if fresh != prepared['context']:
        raise UpdateError('确认后原程序、配置或资料路径发生变化；已停止。')
    no_pending(prepared['updater'])


def prepare_recovery(install_dir, config_path, *, port=0, attempt=None):
    if type(port) is not int or not 0 <= port < 65536:
        raise UpdateError('本机端口无效。')
    release = load_release()
    context = read_context(install_dir, config_path, release)
    protected = [context[key] for key in ('install_dir', 'data_dir', 'config_path')]
    if attempt is None:
        attempt = Attempt(new_work([*protected, tool_directory()]))
    if any(_overlap(attempt.work, local_path(path)) for path in protected):
        raise UpdateError('修复工作目录与原程序/资料重叠。')
    updater = SafeUpdater(context['install_dir'], protected_data_dirs=[context['data_dir']],
                          config_paths=[context['config_path']])
    prepared = dict(context=context, release=release, updater=updater, attempt=attempt, port=port)
    attempt.record('checking', '正在校验离线程序包、原资料路径和模型。', **context)
    try:
        no_pending(updater)
        archive = local_path(tool_directory() / release['package']['filename'])
        if not archive.is_file() or archive.stat().st_size != release['package']['size'] or _hash(archive) != release['package']['sha256']:
            raise UpdateError('配套程序ZIP与工具内嵌发布身份不一致；未关闭原程序。')
        source = attempt.work / 'source'
        manifest = extract_update_archive(archive, source, release['version'])
        if manifest['schema_version'] != 2 or manifest.get('update_executor') != 'main-exe-v1':
            raise UpdateError('一次性修复只接受无独立worker的v2程序包。')
        prepared['source'] = source
        # The shared read-only preflight may create only its protected update
        # lock metadata. It does not replace application, models or user data.
        updater.check(source, read_only=True)
        revalidate(prepared)
        attempt.record('prepared', '校验通过，等待确认后正常退出旧程序。',
                       target_version=release['version'], **context)
        return prepared
    except Exception as exc:
        attempt.log.exception('Preflight refused')
        exc.recovery_result = attempt.record('error', str(exc), program_replaced=False)
        attempt.close()
        raise


def available_port(preferred):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', preferred))
        return listener.getsockname()[1]


def spawn_application(request, work, **options):
    """Keep a frozen recovery parent out of the child application's runtime.

    The existing worker builds its explicit config/readiness environment from
    this temporary clean base. Restore only keys changed here, even if launch
    fails; unrelated parent environment entries remain untouched.
    """
    parent = dict(os.environ)
    child_base = clean_worker_environment()
    for key in tuple(child_base):
        if key.startswith('PM_UPDATE_'):
            child_base.pop(key)
    changed = {key for key in parent.keys() | child_base.keys()
               if parent.get(key) != child_base.get(key)}
    try:
        for key in changed:
            if key in child_base:
                os.environ[key] = child_base[key]
            else:
                os.environ.pop(key, None)
        return worker.spawn_application(request, work, **options)
    finally:
        for key in changed:
            if key in parent:
                os.environ[key] = parent[key]
            else:
                os.environ.pop(key, None)


def execute_recovery(prepared, confirm):
    context, release, updater, attempt = (prepared[key] for key in ('context', 'release', 'updater', 'attempt'))
    if not confirm(context, release['version']):
        return attempt.record('cancelled', '已取消；未关闭旧程序或替换程序文件。', program_replaced=False)
    # No synthetic old PID, ready/go receipt, or downloaded executable hook.
    request = dict(install_dir=context['install_dir'], config_path=context['config_path'],
                   data_dir=context['data_dir'], target_version=release['version'],
                   port=prepared['port'], health_token=secrets.token_hex(32),
                   local_app_data=os.environ.get('LOCALAPPDATA'), debug=False)
    probe = desktop = None
    applied = stopped = False
    try:
        revalidate(prepared)
        stop_original(context)
        stopped = True
        revalidate(prepared)
        request['port'] = available_port(prepared['port'])
        no_pending(updater)
        updater.recover()  # Only committed/rolled-back/no transaction passed the guard.
        attempt.record('applying', '正在安装程序，原程序保留在事务备份中。')
        result = updater.apply(prepared['source'])
        if result['state'] != 'pending_health':
            raise UpdateError('更新事务没有进入待健康确认状态。')
        applied = True
        attempt.record('health_check', '正在启动新程序并核对版本、资料与配置。')
        probe = spawn_application(request, attempt.work, probe=True)
        worker.verify_health(request, probe)
        worker.stop_owned_probe(request, probe)
        probe = None
        attempt.record('desktop_check', '正在确认原生窗口和资料页面已重新打开。')
        desktop = spawn_application(request, attempt.work, desktop=True)
        identity = worker.verify_desktop(request, desktop, attempt.work)
        if _hash(Path(context['config_path'])) != context['original_config_sha256']:
            raise UpdateError('原配置内容发生变化，未确认升级成功。')
        updater.commit('一次性修复：原生窗口、资料页面、版本及资料配置身份已确认。',
                       desktop_pids=[identity['pid'], identity['server_pid']])
        return attempt.record('complete', '已升级到1.0.16并重新打开；原模型、资料和配置保留。',
                              version=release['version'], backups_retained=True)
    except Exception as exc:
        attempt.log.exception('Upgrade failed; checking recovery')
        if not stopped:
            return attempt.record('error', '原程序尚未确认退出，未替换程序：' + str(exc), program_replaced=False)
        try:
            worker.stop_owned_probe(request, probe)
            worker.stop_owned_desktop(request, desktop)
            if applied:
                result = updater.rollback()
            else:
                # apply() rolls back its own failed replacement. Do not recover
                # a newly discovered interrupted transaction from another run.
                no_pending(updater)
                journal = updater._journal()
                result = {'state': journal['state'] if journal else 'no_transaction'}
            # Never launch an unknown executable after a failed replacement.
            if _hash(Path(context['install_dir']) / 'PocketMemory.exe') != context['original_main_sha256']:
                raise UpdateError('原程序字节尚未恢复，未启动未知EXE。')
            if _hash(Path(context['config_path'])) != context['original_config_sha256']:
                raise UpdateError('原配置已变化，未用过期资料路径重新启动；程序备份已保留。')
            # Recovery reads the restored daemon's actual port from its lock;
            # a failed requested-port reservation must not strand the old app.
            request['port'] = available_port(0)
            restored = spawn_application(request, attempt.work)
            worker.verify_restored_desktop(request, restored, attempt.work, context['old_version'])
            state = 'rolled_back' if result['state'] == 'rolled_back' else 'not_updated'
            return attempt.record(state, '升级未完成；原程序已恢复并重新打开。原因：' + str(exc),
                                  version=context['old_version'], backups_retained=True)
        except Exception as recovery:
            attempt.log.exception('Recovery needs attention')
            return attempt.record('recovery_required', f'升级失败：{exc}；恢复待处理：{recovery}。原事务备份已保留。',
                                  backups_retained=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Pocket Memory 一次性离线修复升级')
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--config-path', type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--quiet', action='store_true', help='Explicit noninteractive confirmation for controlled validation.')
    args = parser.parse_args(argv)
    root = None
    prepared = None
    attempt = None
    try:
        if args.quiet and (args.install_dir is None or args.config_path is None):
            raise UpdateError('--quiet需要明确的--install-dir和--config-path。')
        if not args.quiet:
            import tkinter as tk
            from tkinter import filedialog, messagebox
            root = tk.Tk()
            root.withdraw()
            if args.install_dir is None:
                selected = filedialog.askopenfilename(title='选择原来的 PocketMemory.exe',
                                                     filetypes=[('Pocket Memory', 'PocketMemory.exe')])
                if not selected:
                    return 1
                if Path(selected).name != 'PocketMemory.exe':
                    raise UpdateError('请选择原PocketMemory.exe。')
                args.install_dir = Path(selected).parent
        config = args.config_path or default_config_path()
        prepared = prepare_recovery(args.install_dir, config, port=args.port)
        attempt = prepared['attempt']
        def confirm(context, version):
            if args.quiet:
                return True
            return messagebox.askokcancel('确认修复升级',
                f"原程序：{context['install_dir']}\n原版本：{context['old_version']}\n"
                f"原资料库：{context['data_dir']}\n配置：{context['config_path']}\n升级到：{version}\n\n"
                '请先保存编辑并关闭旧窗口。确认后工具会正常退出后台、升级程序并重新打开。'
                '模型、资料和配置沿用，失败保留原程序备份。')
        result = execute_recovery(prepared, confirm)
        if not args.quiet:
            messagebox.showinfo('修复升级结果', result['message'] + '\n\n诊断目录：' + result['log_dir'])
        elif sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0 if result['state'] == 'complete' else 1
    except Exception as exc:
        saved_result = getattr(exc, 'recovery_result', None)
        if attempt is None and saved_result is None:
            protected = [path for path in (args.install_dir, args.config_path) if path and path.is_absolute()]
            try:
                attempt = Attempt(new_work([*protected, tool_directory()]))
            except Exception:
                attempt = None
        if saved_result is not None:
            result = saved_result
        elif attempt is not None:
            attempt.log.exception('Recovery stopped')
            result = attempt.record('error', str(exc))
        else:
            result = dict(state='error', message=str(exc), log_dir='无法创建安全的诊断目录')
        if root is not None:
            from tkinter import messagebox
            messagebox.showerror('修复升级已停止', result['message'] + '\n\n诊断目录：' + result['log_dir'])
        elif sys.stderr is not None:
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if attempt is not None:
            attempt.close()
        if root is not None:
            root.destroy()


if __name__ == '__main__':
    raise SystemExit(main())
