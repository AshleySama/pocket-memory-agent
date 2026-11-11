# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Trusted standalone worker for application-owned updates; no directory picker.

Build this entry as a onefile executable at a future authorized release point.
The running application copies its INSTALLED worker outside the installation and
passes an internal request. Never execute a worker supplied by the downloaded ZIP.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pocket_memory.safe_update import (STATE_DIR, SafeUpdater, UpdateError, _absolute,
    _atomic_json, _no_links, _overlap, _read_json, running_install_processes)
from pocket_memory.app_updates import version_key, _NoRedirect


def load_request(path: Path) -> dict:
    path = _absolute(path)
    if path.name != "request.json" or path.stat().st_size > 64 * 1024:
        raise UpdateError("Invalid application update request")
    request = _read_json(path)
    required = {"schema_version", "install_dir", "data_dir", "config_path", "source_dir", "target_version",
                "parent_pid", "launcher_pid", "port", "health_token", "local_app_data", "debug"}
    if set(request) != required or type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise UpdateError("Unsupported application update request")
    for field in ("install_dir", "data_dir", "config_path", "source_dir"):
        if not isinstance(request[field], str) or not Path(request[field]).is_absolute():
            raise UpdateError("Update request paths must be absolute")
        request[field] = str(_absolute(request[field]))
    install, source = Path(request["install_dir"]), Path(request["source_dir"])
    if source != path.parent / "source" or _overlap(path.parent, install) or _overlap(path.parent, Path(request["data_dir"])):
        raise UpdateError("Worker staging overlaps installation/data or uses an unexpected source")
    for field in ("parent_pid", "launcher_pid"):
        if type(request[field]) is not int or request[field] <= 0:
            raise UpdateError("Invalid parent identity")
    if type(request["port"]) is not int or not 0 < request["port"] < 65536:
        raise UpdateError("Invalid local server port")
    if not isinstance(request["health_token"], str) or not re.fullmatch(r"[0-9a-f]{64}", request["health_token"]):
        raise UpdateError("Invalid update health token")
    if type(request["debug"]) is not bool:
        raise UpdateError("Invalid launcher mode")
    if request["local_app_data"] is not None:
        request["local_app_data"] = str(_absolute(request["local_app_data"]))
    version_key(request["target_version"])
    return request


def _parent_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not evidence that the old application exited.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_for_old_exit(request: dict, timeout=90.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if not _parent_alive(request["parent_pid"]) and not running_install_processes(Path(request["install_dir"])):
            return
        if time.monotonic() >= deadline:
            raise UpdateError("旧程序未完全退出，本次更新已停止；没有强制结束任何旧进程。")
        time.sleep(0.25)


def restart_command(request: dict, *, probe=False) -> list[str]:
    command = [str(Path(request["install_dir"]) / "PocketMemory.exe")]
    if probe:
        command.append("--server-only")
    command.extend(["--data-dir", request["data_dir"], "--host", "127.0.0.1", "--port", str(request["port"])])
    if request["debug"]:
        command.append("--debug")
    return command


def restart_environment(request: dict, *, probe=False) -> dict:
    environment = dict(os.environ)
    environment["PM_UPDATE_CONFIG_PATH"] = request["config_path"]
    if request["local_app_data"] is not None:
        environment["LOCALAPPDATA"] = request["local_app_data"]
    if probe:
        environment["PM_UPDATE_HEALTH_TOKEN"] = request["health_token"]
        environment["PM_UPDATE_PROBE"] = "1"
    else:
        environment.pop("PM_UPDATE_HEALTH_TOKEN", None)
        environment.pop("PM_UPDATE_PROBE", None)
    return environment


def validate_windows_executable(path: Path) -> None:
    """Reject missing/truncated/non-x64 PE headers without invoking the loader.

    This is a bounded structural check, not a signature or full PE validation.
    Some Windows loader/error-handler configurations can block CreateProcess on
    an obviously invalid file instead of promptly returning an error.
    """
    _no_links(path)
    try:
        with path.open("rb") as executable:
            size = os.fstat(executable.fileno()).st_size
            dos = executable.read(64)
            if len(dos) != 64 or dos[:2] != b"MZ":
                raise UpdateError("Windows executable has a missing or truncated MZ header")
            pe_offset = int.from_bytes(dos[0x3C:0x40], "little")
            if pe_offset < 64 or pe_offset > size - 24:
                raise UpdateError("Windows executable PE header offset is outside the file")
            executable.seek(pe_offset)
            coff = executable.read(24)  # PE signature followed by the COFF header.
            if len(coff) != 24 or coff[:4] != b"PE\0\0":
                raise UpdateError("Windows executable has an invalid PE signature")
            if int.from_bytes(coff[4:6], "little") != 0x8664:
                raise UpdateError("Windows executable is not AMD64")
            optional_size = int.from_bytes(coff[20:22], "little")
            # PE32+ has 112 fixed bytes before its optional data directories.
            if optional_size < 112 or pe_offset + 24 + optional_size > size:
                raise UpdateError("Windows executable has a truncated PE32+ optional header")
            if executable.read(2) != b"\x0b\x02":
                raise UpdateError("Windows executable does not have a PE32+ optional header")
    except OSError as exc:
        raise UpdateError(f"Windows executable is missing or unreadable: {path}") from exc


def spawn_application(request: dict, work: Path, *, probe=False):
    command = restart_command(request, probe=probe)
    if os.name == "nt":
        validate_windows_executable(Path(command[0]))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with (work / ("health-probe.log" if probe else "application-restart.log")).open("ab") as log:
        return subprocess.Popen(command, cwd=request["install_dir"],
                                env=restart_environment(request, probe=probe), stdout=log,
                                stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)


def _local_request(request: dict, path: str, *, method="GET"):
    url = f"http://127.0.0.1:{request['port']}{path}"
    headers = {"X-Pocket-Update-Token": request["health_token"], "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=b"{}" if method == "POST" else None, headers=headers, method=method)
    # Do not let HTTP_PROXY route local identity/shutdown requests off-machine.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(req, timeout=2)


def verify_health(request: dict, process, timeout=150.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise UpdateError("新程序在健康检查前退出。")
        try:
            with _local_request(request, "/api/update-health") as response:
                raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise UpdateError("健康响应过大。")
            value = json.loads(raw)
            if (isinstance(value, dict) and value.get("ok") is True
                    and value.get("token") == request["health_token"]
                    and value.get("pid") == process.pid
                    and value.get("app_version") == request["target_version"]
                    and str(_absolute(value.get("data_dir", ""))) == request["data_dir"]
                    and str(_absolute(value.get("config_path", ""))) == request["config_path"]):
                return value
        except (urllib.error.URLError, OSError, ValueError, TypeError, UpdateError):
            pass
        time.sleep(0.3)
    raise UpdateError("新程序身份、版本或资料路径健康检查未通过。")


def stop_owned_probe(request: dict, process, timeout=25.0) -> None:
    """Only ever terminate the Popen child created by this worker."""
    if process is None or process.poll() is not None:
        return
    authenticated = False
    try:
        with _local_request(request, "/api/update-health") as response:
            payload = json.loads(response.read(64 * 1024))
        authenticated = (isinstance(payload, dict) and payload.get("token") == request["health_token"]
                         and payload.get("pid") == process.pid)
    except Exception:
        pass
    if authenticated:
        try:
            with _local_request(request, "/api/shutdown", method="POST") as response:
                response.read(4096)
        except Exception:
            pass
    else:
        # The port might belong to another local app while our child is still
        # starting. Never send an unauthenticated shutdown to that listener.
        process.terminate()
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run_worker(request_path: Path) -> dict:
    request_path = _absolute(request_path)
    request = load_request(request_path)
    work, install = request_path.parent, Path(request["install_dir"])
    updater = SafeUpdater(install, protected_data_dirs=[request["data_dir"]], config_paths=[request["config_path"]])
    updater._protect([updater.state])
    def state(name, message, *, persist=False):
        value = {"state": name, "message": message, "target_version": request["target_version"]}
        _atomic_json(work / "worker-state.json", value)
        if persist:
            updater.state.mkdir(exist_ok=True)
            _atomic_json(updater.state / "auto-update-result.json", value)
        return value
    _atomic_json(work / "ready.json", {"token": request["health_token"], "pid": os.getpid()})
    deadline = time.monotonic() + 20
    while not (work / "go.json").exists():
        if time.monotonic() >= deadline:
            return state("cancelled", "应用未确认退出，更新取消。")
        time.sleep(0.1)
    if _read_json(work / "go.json").get("token") != request["health_token"]:
        raise UpdateError("Invalid application handoff token")
    state("waiting_exit", "正在等待当前程序完成保存并退出。")
    try:
        wait_for_old_exit(request)
    except Exception as exc:
        return state("error", str(exc), persist=True)
    probe, applied = None, False
    try:
        prior = updater.recover()
        if prior["state"] == "pending_health":
            return state("recovery_required", "此前更新尚待健康确认；本次未覆盖、未冒充回退，也未启动未确认的新程序。", persist=True)
        state("applying", "正在安全替换程序，原程序保留在备份中。")
        result = updater.apply(Path(request["source_dir"]))
        applied = result["state"] == "pending_health"
        state("health_check", "正在启动新程序并自动检查版本与资料路径。")
        probe = spawn_application(request, work, probe=True)
        verify_health(request, probe)
        stop_owned_probe(request, probe)
        probe = None
        updater.commit("自动健康检查通过：本机随机令牌、版本、进程、资料路径和配置路径均匹配；探针已正常退出。")
        spawn_application(request, work)
        return state("complete", "程序更新完成，已自动重新打开。", persist=True)
    except Exception as exc:
        try:
            stop_owned_probe(request, probe)
            if applied:
                restored = updater.rollback()
            else:
                restored = updater.recover()
            if restored["state"] == "pending_health":
                return state("recovery_required", f"更新未完成：{exc}。现有待确认事务已保留，未宣称回退。", persist=True)
            spawn_application(request, work)
        except Exception as recovery:
            return state("recovery_required", f"更新失败：{exc}；自动恢复未完成：{recovery}。备份已保留。", persist=True)
        if restored["state"] == "rolled_back":
            return state("rolled_back", f"新版本未能通过自动检查，已回退并重新打开原程序。原因：{exc}", persist=True)
        return state("not_updated", f"本次未替换程序，已重新打开原程序。原因：{exc}", persist=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pocket Memory internal update worker")
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_worker(args.request)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] == "complete" else 2
    except Exception as exc:
        print(f"Automatic update stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
