# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Read-only discovery for the shipped portable Beta's one-time transition.

Only a running, allowlisted Beta is eligible. No recursive filesystem search,
directory chooser, process termination, configuration write, or update occurs
here. The caller must gate handoff on ``visible_window_pids`` being empty and
revalidate the returned identity immediately before requesting daemon shutdown.
"""
from __future__ import annotations

import ctypes
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit
import urllib.request

from pocket_memory.safe_update import UpdateError, _absolute, _hash, _read_json


class DiscoveryBlocked(UpdateError):
    def __init__(self, code: str, message: str, versions=()):
        super().__init__(message)
        self.code = code
        self.versions = tuple(sorted(set(versions)))


@dataclass(frozen=True)
class LegacyProcess:
    pid: int
    executable: str
    command_line: str | None = None
    parent_pid: int = 0


@dataclass(frozen=True)
class LegacyTransitionTarget:
    install_dir: Path
    data_dir: Path
    config_path: Path
    local_app_data: Path
    port: int
    parent_pid: int
    other_pids: tuple[int, ...]
    visible_window_pids: tuple[int, ...]
    app_version: str
    executable_sha256: str
    config_sha256: str | None
    server_url: str


def _same_path(left, right) -> bool:
    return os.path.normcase(str(_absolute(left))).casefold() == os.path.normcase(str(_absolute(right))).casefold()


def _native_processes() -> list[LegacyProcess]:
    if os.name != "nt":
        raise DiscoveryBlocked("unsupported_platform", "首次过渡升级只支持 Windows。")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise DiscoveryBlocked("process_inventory_failed", "无法读取运行中的旧程序，请稍后重试。")
    found = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not more:
            raise DiscoveryBlocked("process_inventory_failed", "无法读取运行中的旧程序，请稍后重试。")
        while more:
            if entry.name.casefold() == "pocketmemory.exe":
                handle = kernel.OpenProcess(0x1000, False, entry.pid)
                if not handle:
                    if ctypes.get_last_error() == 5:
                        raise DiscoveryBlocked("process_access_denied", "有旧程序无法确认身份；请用同一 Windows 用户运行升级器。")
                else:
                    try:
                        size = wintypes.DWORD(32768)
                        name = ctypes.create_unicode_buffer(size.value)
                        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                            raise DiscoveryBlocked("process_inventory_failed", "无法确认运行中旧程序的位置。")
                        found.append(LegacyProcess(int(entry.pid), name.value, parent_pid=int(entry.parent)))
                    finally:
                        kernel.CloseHandle(handle)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def inspect_running_processes() -> list[LegacyProcess]:
    """Use native image identities; CIM supplies optional command lines only.

    The fixed, read-only CIM query runs hidden, without elevation. A CIM failure
    falls back to locks at the exact discovered installation/profile locations;
    it never causes a search of drives or user document folders.
    """
    processes = _native_processes()
    if not processes:
        return processes
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = ("$ErrorActionPreference='Stop'; "
              "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
              "@(Get-CimInstance Win32_Process -Filter \"Name='PocketMemory.exe'\" | "
              "Select-Object ProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress")
    try:
        result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=15, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or len(result.stdout) > 1024 * 1024:
            return processes
        values = json.loads(result.stdout.decode("utf-8-sig"))
        values = values if isinstance(values, list) else [values]
        by_pid = {value.get("ProcessId"): value for value in values if isinstance(value, dict)}
        enriched = []
        for process in processes:
            value = by_pid.get(process.pid, {})
            command = value.get("CommandLine")
            if not (isinstance(command, str) and len(command) <= 32768
                    and isinstance(value.get("ExecutablePath"), str)
                    and _same_path(value["ExecutablePath"], process.executable)):
                command = None
            enriched.append(LegacyProcess(process.pid, process.executable, command, process.parent_pid))
        return enriched
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired, UpdateError):
        return processes


def visible_windows_for(pids: Iterable[int]) -> tuple[int, ...]:
    """Return the selected installation's PIDs owning visible top-level windows."""
    if os.name != "nt":
        raise DiscoveryBlocked("unsupported_platform", "无法确认旧程序窗口是否已关闭。")
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    selected, visible = set(pids), set()

    @callback_type
    def visit(window, _):
        if user.IsWindowVisible(window):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value in selected:
                visible.add(int(pid.value))
        return True

    if not user.EnumWindows(visit, 0):
        raise DiscoveryBlocked("window_inventory_failed", "无法确认旧程序窗口是否已关闭，请稍后重试。")
    return tuple(sorted(visible))


def _arguments(command: str) -> list[str]:
    if os.name != "nt":
        # The production path is native; this branch permits injected inventories
        # in portable unit tests with ordinary quoted Windows paths.
        return [value[1:-1] if value.startswith('"') and value.endswith('"') else value
                for value in shlex.split(command, posix=False)]
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise DiscoveryBlocked("unknown_arguments", "无法识别旧程序启动参数。")
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(argv)


def _daemon_arguments(process: LegacyProcess) -> dict | None:
    if not process.command_line:
        return None
    args = _arguments(process.command_line)
    if not args or not _same_path(args[0], process.executable):
        raise DiscoveryBlocked("unknown_arguments", "旧程序启动身份不一致，已停止自动识别。")
    if "--server-only" not in args[1:]:
        return None
    result, index = {}, 1
    while index < len(args):
        name = args[index]
        if name in result:
            raise DiscoveryBlocked("unknown_arguments", "旧程序含重复启动参数，无法安全过渡。")
        if name in {"--server-only", "--debug"}:
            result[name] = True
            index += 1
        elif name in {"--data-dir", "--host", "--port"} and index + 1 < len(args):
            result[name] = args[index + 1]
            index += 2
        else:
            raise DiscoveryBlocked("unknown_arguments", "旧程序采用未支持的启动方式，已停止自动识别。")
    if "--data-dir" in result and not Path(result["--data-dir"]).is_absolute():
        raise DiscoveryBlocked("unknown_arguments", "旧程序资料路径不是绝对路径，无法安全过渡。")
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DiscoveryBlocked("invalid_local_identity", "本机旧程序身份请求不能重定向。")


def _origin(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or not parsed.port):
            raise ValueError("not a local server origin")
        return f"http://{parsed.netloc}", parsed.port
    except (TypeError, ValueError) as exc:
        raise DiscoveryBlocked("invalid_local_identity", "旧程序锁文件不是有效的本机服务地址。") from exc


def _read_endpoint(origin: str, endpoint: str) -> dict:
    origin, _ = _origin(origin)
    request = urllib.request.Request(origin + endpoint, method="GET")
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
        raw = response.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise DiscoveryBlocked("invalid_local_identity", "旧程序身份响应过大。")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DiscoveryBlocked("invalid_local_identity", "旧程序身份响应无效。")
    return value


def _profile_candidates(install: Path, local: Path) -> list[tuple[Path, Path, Path | None]]:
    result = []
    for base in (install / "beta-profile", local):
        config = _absolute(base / "PocketMemory/config.json")
        configured = None
        if config.exists():
            value = _read_json(config).get("data_dir")
            if value is not None:
                if not isinstance(value, str) or not value or not Path(value).is_absolute():
                    raise DiscoveryBlocked("unknown_profile", "旧配置含无法确定的资料路径，已停止自动识别。")
                configured = _absolute(value)
        result.append((config, _absolute(base), configured))
    return result


def _select_profile(install: Path, active_data: Path, initial_data: Path | None,
                    profiles: list[tuple[Path, Path, Path | None]]) -> tuple[Path, Path]:
    matches = []
    for config, base, configured in profiles:
        beta = _same_path(base, install / "beta-profile")
        default = install / ("data-beta" if beta else "data")
        # Start-Beta always supplies data-beta. A subsequent library switch is
        # recorded in that same profile; the live settings/lock identify the
        # current library even when the original command line is now stale.
        initial_matches = initial_data is None or _same_path(initial_data, default)
        configured_matches = configured is not None and _same_path(configured, active_data)
        default_matches = _same_path(active_data, default)
        if beta:
            matches_profile = initial_matches and (configured_matches or default_matches)
        else:
            matches_profile = configured_matches or (configured is None and default_matches)
        if matches_profile:
            matches.append((config, base))
    unique = {str(config).casefold(): (config, base) for config, base in matches}
    if len(unique) != 1:
        raise DiscoveryBlocked("ambiguous_profile" if unique else "unknown_profile",
                               "无法唯一确认旧 Beta 的配置；请仅保留要升级的旧 Beta，按原来的启动方式打开后重试。")
    return next(iter(unique.values()))


def discover_legacy_install(allowed_exe_hashes: Mapping[str, str], *,
                            process_inventory: Iterable[LegacyProcess | Mapping] | None = None,
                            status_reader: Callable[[str], dict] | None = None,
                            settings_reader: Callable[[str], dict] | None = None,
                            local_app_data: str | Path | None = None,
                            visible_window_pids: Iterable[int] | None = None) -> LegacyTransitionTarget:
    """Find exactly one active supported Beta and preserve its selected profile.

    ``allowed_exe_hashes`` maps shipped EXE SHA256 values to exact API versions.
    Readers receive a validated loopback origin, without an endpoint suffix.
    Injectable inventories/readers are for deterministic tests; production uses
    native process identities, bounded CIM metadata, and read-only local HTTP.
    """
    if not allowed_exe_hashes:
        raise DiscoveryBlocked("empty_allowlist", "过渡包未声明支持的旧 Beta，无法继续。")
    allowed = {key.lower(): version for key, version in allowed_exe_hashes.items()}
    raw_processes = process_inventory if process_inventory is not None else inspect_running_processes()
    processes = [item if isinstance(item, LegacyProcess) else LegacyProcess(**item) for item in raw_processes]
    processes = [item for item in processes if Path(item.executable).name.casefold() == "pocketmemory.exe"]
    if not processes:
        raise DiscoveryBlocked("no_running_beta", "请先按原来的方式打开要升级的旧 Beta，再运行过渡升级。无需选择目录。")
    local = _absolute(local_app_data or os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    status_reader = status_reader or (lambda origin: _read_endpoint(origin, "/api/status"))
    settings_reader = settings_reader or (lambda origin: _read_endpoint(origin, "/api/settings"))
    supplied_windows = set(visible_window_pids) if visible_window_pids is not None else None
    grouped: dict[str, list[LegacyProcess]] = {}
    for process in processes:
        if type(process.pid) is not int or process.pid <= 0:
            raise DiscoveryBlocked("invalid_process_identity", "旧程序进程身份无效。")
        try:
            image = _absolute(process.executable)
        except (OSError, ValueError, UpdateError) as exc:
            raise DiscoveryBlocked("invalid_installation", "旧程序路径无法安全确认，已停止自动识别。") from exc
        grouped.setdefault(str(image.parent).casefold(), []).append(process)
    found, versions, unsupported = [], [], False
    for install_processes in grouped.values():
        install = _absolute(Path(install_processes[0].executable).parent)
        try:
            digest = _hash(install / "PocketMemory.exe")
        except (OSError, UpdateError) as exc:
            raise DiscoveryBlocked("invalid_installation", "旧程序文件已改变或无法读取，请重新识别。", versions) from exc
        version = allowed.get(digest)
        if not version:
            unsupported = True
            continue
        versions.append(version)
        try:
            profiles = _profile_candidates(install, local)
        except DiscoveryBlocked:
            raise
        except (OSError, ValueError, UpdateError) as exc:
            raise DiscoveryBlocked("unknown_profile", "无法读取旧程序配置，已停止自动识别。", versions) from exc
        origins: dict[tuple[int, str], Path | None] = {}
        by_pid = {process.pid: process for process in install_processes}
        initial_by_pid = {}
        fallback_pids = set()
        for process in install_processes:
            args = _daemon_arguments(process)
            if args is None:
                if process.command_line is None:
                    fallback_pids.add(process.pid)
                continue
            initial = _absolute(args["--data-dir"]) if args.get("--data-dir") else None
            initial_by_pid[process.pid] = initial
            if args.get("--port"):
                host = args.get("--host", "127.0.0.1")
                host = "[::1]" if host == "::1" else host
                origin, _ = _origin(f"http://{host}:{args['--port']}")
                origins[(process.pid, origin)] = initial
            else:
                fallback_pids.add(process.pid)
        # These are exact known locations only. No recursive directory search.
        data_candidates = [install / "data-beta", install / "data"]
        data_candidates.extend(data for _, _, data in profiles if data is not None)
        data_candidates.extend(data for data in initial_by_pid.values() if data is not None)
        for data in data_candidates:
            lock_path = _absolute(data) / ".pocket-memory.lock"
            if not lock_path.is_file():
                continue
            lock = _read_json(lock_path)
            pid = lock.get("pid")
            if type(pid) is not int or pid not in by_pid:
                continue
            if pid not in fallback_pids and pid not in initial_by_pid:
                continue
            origin, _ = _origin(lock.get("url", ""))
            origins.setdefault((pid, origin), initial_by_pid.get(pid))
        for (pid, origin), initial in origins.items():
            try:
                status = status_reader(origin)
                if (not isinstance(status, dict) or status.get("ok") is not True
                        or type(status.get("pid")) is not int or status.get("pid") != pid
                        or status.get("app_version") != version):
                    raise DiscoveryBlocked("invalid_local_identity", "旧程序接口与进程或版本不一致，已停止过渡。", versions)
                settings = settings_reader(origin)
                raw_data = settings.get("data_dir") if isinstance(settings, dict) else None
                if not isinstance(raw_data, str) or not Path(raw_data).is_absolute():
                    raise DiscoveryBlocked("invalid_local_identity", "旧程序未返回有效资料位置。", versions)
                data = _absolute(raw_data)
                if not data.is_dir():
                    raise DiscoveryBlocked("invalid_local_identity", "旧程序资料位置不存在。", versions)
                lock = _read_json(data / ".pocket-memory.lock")
                lock_origin, lock_port = _origin(lock.get("url", ""))
                if (type(lock.get("pid")) is not int or lock["pid"] != pid or lock_origin != origin
                        or type(lock.get("port")) is not int or lock["port"] != lock_port):
                    raise DiscoveryBlocked("invalid_local_identity", "旧程序当前资料锁与进程不一致。", versions)
                config, profile = _select_profile(install, data, initial, profiles)
                pids = set(by_pid)
                windows = (supplied_windows & pids if supplied_windows is not None
                           else set(visible_windows_for(pids)))
                found.append(LegacyTransitionTarget(install, data, config, profile, lock_port, pid,
                    tuple(sorted(pids - {pid})), tuple(sorted(windows)), version, digest,
                    _hash(config) if config.is_file() else None, origin))
            except DiscoveryBlocked:
                raise
            except (OSError, ValueError, TypeError, UpdateError) as exc:
                raise DiscoveryBlocked("unreachable_beta", "无法核对旧 Beta 的当前服务和资料；请等待旧程序启动完成后重试。", versions) from exc
    # An unknown running version can share a user's profile or library. Do not
    # silently ignore it and upgrade another installation in the same session.
    if unsupported:
        raise DiscoveryBlocked("unsupported_running_version", "检测到此过渡包不支持的运行中程序；请关闭其他版本，只保留要升级的旧 Beta。", versions)
    if len(grouped) > 1:
        raise DiscoveryBlocked("ambiguous_installation", "检测到多份正在运行的旧 Beta；请只保留要升级的一份后重试。", versions)
    unique = {(str(item.install_dir).casefold(), item.parent_pid, str(item.data_dir).casefold()): item for item in found}
    if len(unique) != 1:
        raise DiscoveryBlocked("ambiguous_installation" if unique else "no_active_beta",
                               "请只打开要升级的一份旧 Beta，等待界面加载完成后重试。无需选择目录。", versions)
    return next(iter(unique.values()))


def revalidate_legacy_target(target: LegacyTransitionTarget, allowed_exe_hashes: Mapping[str, str],
                             **discovery_options) -> LegacyTransitionTarget:
    """Reject profile/library/process changes while a transition UI is open."""
    current = discover_legacy_install(allowed_exe_hashes, **discovery_options)
    stable = ("install_dir", "data_dir", "config_path", "local_app_data", "port", "parent_pid",
              "app_version", "executable_sha256", "config_sha256", "server_url")
    if any(getattr(target, field) != getattr(current, field) for field in stable):
        raise DiscoveryBlocked("target_changed", "旧 Beta 的资料、配置或进程已改变，请重新识别后再升级。", (current.app_version,))
    return current
