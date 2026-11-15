# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""One-transaction desktop handoff for the released 1.0.3 update worker.

The old worker is never replaced before applying the update. Its authenticated
new-program probe starts a verified external supervisor, which finishes the
existing transaction after that worker exits. No model/data migration occurs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from pocket_memory.app_updates import WORKER_RELATIVE_PATH, worker_identity_matches
from pocket_memory.safe_update import (
    STATE_DIR, SafeUpdater, UpdateError, _absolute, _atomic_json, _hash,
    _no_links, _overlap, _verify, validate_manifest,
)
from pocket_memory.version import APP_VERSION

LEGACY_WORKER_SHA256 = 'ac710a6427dd16a241a8f379142526e52aa6a0dd260a3f7dd9a6fbbe7222a2cd'
MARKER_NAME = 'desktop-handoff.json'
REQUEST_FIELDS = {'schema_version', 'install_dir', 'data_dir', 'config_path',
                  'source_dir', 'target_version', 'parent_pid', 'launcher_pid',
                  'port', 'health_token', 'local_app_data', 'debug'}


def _json(path: Path, limit=1024 * 1024):
    _no_links(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise UpdateError('Missing or oversized desktop handoff record')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise UpdateError('Invalid desktop handoff record')
    return value


def process_identity(pid: int) -> dict | None:
    """A live Windows process identity includes creation time to reject PID reuse."""
    if os.name != 'nt' or type(pid) is not int or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # PID no longer exists.
            return None
        raise UpdateError('Cannot inspect desktop handoff process identity')
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise UpdateError('Cannot inspect desktop handoff process state')
        if code.value != 259:
            return None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if (not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                       ctypes.byref(system), ctypes.byref(user))
                or not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))):
            raise UpdateError('Cannot authenticate desktop handoff process')
        return {'pid': pid, 'path': str(_absolute(name.value)),
                'created': (created.dwHighDateTime << 32) | created.dwLowDateTime}
    finally:
        kernel.CloseHandle(handle)


def identity_alive(identity: dict) -> bool:
    if (not isinstance(identity, dict) or set(identity) != {'pid', 'path', 'created'}
            or type(identity.get('created')) is not int):
        raise UpdateError('Invalid recorded process identity')
    return process_identity(identity['pid']) == identity


def _request(path: Path) -> dict:
    value = _json(path, 64 * 1024)
    if path.name != 'request.json' or set(value) != REQUEST_FIELDS or value['schema_version'] != 1:
        raise UpdateError('Unsupported legacy handoff request')
    for key in ('install_dir', 'data_dir', 'config_path', 'source_dir'):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise UpdateError('Handoff request paths must be absolute')
        value[key] = str(_absolute(value[key]))
    for key in ('parent_pid', 'launcher_pid'):
        if type(value[key]) is not int or value[key] <= 0:
            raise UpdateError('Invalid handoff request process')
    if (type(value['port']) is not int or not 0 < value['port'] < 65536
            or type(value['debug']) is not bool
            or not re.fullmatch(r'[0-9a-f]{64}', str(value['health_token']))
            or value['target_version'] != APP_VERSION):
        raise UpdateError('Invalid legacy handoff request identity')
    if value['local_app_data'] is not None:
        value['local_app_data'] = str(_absolute(value['local_app_data']))
    work = path.parent
    if (Path(value['source_dir']) != work / 'source'
            or any(_overlap(work, Path(value[k])) for k in ('install_dir', 'data_dir'))):
        raise UpdateError('Unsafe legacy handoff work directory')
    return value


def _transaction(request: dict) -> tuple:
    updater = SafeUpdater(request['install_dir'], protected_data_dirs=[request['data_dir']],
                          config_paths=[request['config_path']])
    updater._protect([updater.state])
    _no_links(updater.state)
    journal = updater._journal()
    if (not journal or journal['state'] not in {'pending_health', 'committed'}
            or journal['version'] != request['target_version']):
        raise UpdateError('No matching applied transaction for desktop handoff')
    receipt = _json(updater.receipt_path, 4 * 1024 * 1024)
    manifest = validate_manifest(receipt.get('manifest', {}))
    source = _json(Path(request['source_dir']) / 'update-manifest.json', 4 * 1024 * 1024)
    if (receipt.get('version') != request['target_version']
            or manifest['version'] != request['target_version'] or manifest != source):
        raise UpdateError('Desktop handoff source/installed manifest mismatch')
    entries = {entry['path']: entry for entry in manifest['files']}
    for relative in ('PocketMemory.exe', WORKER_RELATIVE_PATH):
        if relative not in entries:
            raise UpdateError('Desktop handoff executable is absent from inventory')
        _verify(updater.install / relative, entries[relative])
    old = _json(updater.state / journal['transaction'] / 'receipt-backup.json', 4 * 1024 * 1024)
    if old.get('version') != '1.0.3':
        raise UpdateError('Legacy handoff only supports the released 1.0.3 baseline')
    return updater, journal, entries


def load_handoff(request_path) -> dict:
    """Validate an existing handoff. Caller waits for legacy_identity to exit."""
    path = _absolute(request_path)
    request = _request(path)
    updater, journal, entries = _transaction(request)
    marker = _json(updater.state / MARKER_NAME, 64 * 1024)
    expected = {'schema_version', 'request_path', 'transaction', 'token', 'version',
                'legacy_identity', 'supervisor_identity', 'supervisor_path', 'expires'}
    if (set(marker) != expected or marker['schema_version'] != 1
            or marker['request_path'] != str(path)
            or marker['transaction'] != journal['transaction']
            or marker['token'] != request['health_token']
            or marker['version'] != APP_VERSION
            or type(marker['expires']) not in (int, float)
            or not time.time() < marker['expires'] <= time.time() + 905):
        raise UpdateError('Stale or mismatched desktop handoff')
    work = path.parent
    legacy = marker['legacy_identity']
    _no_links(work / 'PocketMemoryUpdateWorker.exe')
    if (not isinstance(legacy, dict) or set(legacy) != {'pid', 'path', 'created'}
            or type(legacy['pid']) is not int or legacy['pid'] <= 0
            or type(legacy['created']) is not int
            or legacy['path'] != str(work / 'PocketMemoryUpdateWorker.exe')
            or _hash(work / 'PocketMemoryUpdateWorker.exe') != LEGACY_WORKER_SHA256):
        raise UpdateError('Unrecognized original update worker')
    ready = _json(work / 'ready.json', 4096)
    go = _json(work / 'go.json', 4096)
    if (ready.get('pid') != legacy['pid'] or ready.get('token') != marker['token']
            or go.get('token') != marker['token']):
        raise UpdateError('Legacy handoff was not authorized by the original application')
    supervisor = work / 'desktop-supervisor' / 'PocketMemoryUpdateWorker.exe'
    if marker['supervisor_path'] != str(supervisor):
        raise UpdateError('Unexpected desktop supervisor path')
    updater._protect([supervisor])
    _verify(supervisor, entries[WORKER_RELATIVE_PATH])
    return dict(request=request, marker=marker, work=work, updater=updater,
                old_version='1.0.3', transaction=journal['transaction'])


def stage_handoff(config, data_dir) -> dict | None:
    """Called only by a frozen legacy health probe, before exposing success."""
    token = os.environ.get('PM_UPDATE_HEALTH_TOKEN', '')
    if (not getattr(sys, 'frozen', False) or not token
            or os.environ.get('PM_UPDATE_DESKTOP_PROTOCOL') == '1'):
        return None
    install = _absolute(sys.executable).parent
    matches = []
    for work in Path(tempfile.gettempdir()).glob('PocketMemory-update-*'):
        try:
            path = _absolute(work / 'request.json')
            request = _request(path)
            if request['health_token'] == token:
                matches.append((path, request))
        except (OSError, ValueError, UpdateError):
            continue
    if len(matches) != 1:
        raise UpdateError('Cannot uniquely identify the legacy update transaction')
    path, request = matches[0]
    if (Path(request['install_dir']) != install
            or Path(request['data_dir']) != _absolute(data_dir)
            or Path(request['config_path']) != _absolute(config.path)):
        raise UpdateError('Legacy probe does not match the selected installation/data/config')
    updater, journal, entries = _transaction(request)
    if journal['state'] != 'pending_health':
        raise UpdateError('Legacy desktop handoff requires an unconfirmed transaction')
    ready = _json(path.parent / 'ready.json', 4096)
    legacy = process_identity(ready.get('pid'))
    original_worker = path.parent / 'PocketMemoryUpdateWorker.exe'
    _no_links(original_worker)
    if (not legacy or legacy['path'] != str(original_worker)
            or ready.get('token') != token
            or os.getppid() != legacy['pid']
            or _hash(original_worker) != LEGACY_WORKER_SHA256):
        raise UpdateError('The calling updater is not the released 1.0.3 worker')
    supervisor = path.parent / 'desktop-supervisor' / 'PocketMemoryUpdateWorker.exe'
    updater._protect([supervisor])
    _no_links(supervisor)
    supervisor.parent.mkdir(exist_ok=False)
    shutil.copy2(install / WORKER_RELATIVE_PATH, supervisor)
    _verify(supervisor, entries[WORKER_RELATIVE_PATH])
    marker = {'schema_version': 1, 'request_path': str(path),
              'transaction': journal['transaction'], 'token': token, 'version': APP_VERSION,
              'legacy_identity': legacy, 'supervisor_identity': None,
              'supervisor_path': str(supervisor), 'expires': time.time() + 900}
    _atomic_json(updater.state / MARKER_NAME, marker)
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith('_PYI_') or key in {'_MEIPASS2', 'PM_UPDATE_HEALTH_TOKEN', 'PM_UPDATE_PROBE'}:
            environment.pop(key, None)
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    environment['PM_UPDATE_DESKTOP_PROTOCOL'] = '1'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    with (path.parent / 'desktop-supervisor.log').open('ab') as log:
        process = subprocess.Popen([str(supervisor), '--request', str(path), '--finalize-existing'],
                                   cwd=str(supervisor.parent), env=environment, stdout=log,
                                   stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    deadline = time.monotonic() + 15
    ack = path.parent / 'handoff-ready.json'
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise UpdateError('Desktop supervisor exited before accepting the transaction')
        if ack.is_file():
            ready = _json(ack, 4096)
            identity = process_identity(ready.get('pid'))
            if (ready.get('token') == token and ready.get('transaction') == journal['transaction']
                    and identity and identity['path'] == str(supervisor)
                    and worker_identity_matches(identity['pid'], process.pid, supervisor)):
                marker['supervisor_identity'] = identity
                _atomic_json(updater.state / MARKER_NAME, marker)
                return marker
        time.sleep(0.05)
    # Do not terminate an unowned child tree: the supervisor is bounded and will
    # see the original worker roll back when this probe refuses to serve health.
    raise UpdateError('Desktop supervisor did not acknowledge the transaction in time')


def yield_to_supervisor(config, data_dir, args) -> bool:
    """An original-worker restart delegates once; ordinary launches are untouched."""
    if (not getattr(sys, 'frozen', False) or os.environ.get('PM_UPDATE_DESKTOP_TOKEN')
            or os.environ.get('PM_UPDATE_HEALTH_TOKEN') or getattr(args, 'server_only', False)
            or getattr(args, 'quit', False) or not os.environ.get('PM_UPDATE_CONFIG_PATH')):
        return False
    marker_path = _absolute(sys.executable).parent / STATE_DIR / MARKER_NAME
    if not marker_path.is_file():
        return False
    try:
        marker = _json(marker_path, 64 * 1024)
        value = load_handoff(marker.get('request_path', ''))
        marker, request = value['marker'], value['request']
        if (Path(request['install_dir']) != _absolute(sys.executable).parent
                or _absolute(config.path) != Path(request['config_path'])
                or _absolute(data_dir) != Path(request['data_dir'])
                or getattr(args, 'host', '') != '127.0.0.1'
                or getattr(args, 'port', None) != request['port']
                or os.getppid() != marker['legacy_identity']['pid']
                or not marker['supervisor_identity']
                or not identity_alive(marker['supervisor_identity'])):
            return False
        _atomic_json(value['work'] / 'handoff-launcher.json',
                     {'token': marker['token'], 'transaction': marker['transaction'],
                      'identity': process_identity(os.getpid())})
        return True
    except (OSError, ValueError, TypeError, UpdateError):
        # Stale/aborted transactions must not block a later ordinary launch.
        return False
