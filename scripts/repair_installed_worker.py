# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Offline, one-time repair of the installed update worker only.

Freeze with worker-repair/manifest.json and worker-repair/PocketMemoryUpdateWorker.exe
as embedded data. The build supplies schema_version=1, worker_sha256 and a nonempty
allowed_previous_sha256 list. No runtime download or user-selected payload is used.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pocket_memory.safe_update import (
    SafeUpdater, UpdateError, _absolute, _atomic_json, _hash, _no_links, _read_json,
    running_install_processes,
)


WORKER_NAME = "PocketMemoryUpdateWorker.exe"
RESOURCE_FOLDER = "worker-repair"
_SHA = re.compile(r"[0-9a-f]{64}")
# The bootstrap may execute ONLY original releases whose bytes were checked at
# release time. Temp request files are discovery hints, never executable trust.
KNOWN_MAIN_SHA256 = frozenset({
    "487f447639c040409686acebfe04040a33c64fd9f91076880302ca4a3b9e4c50",
    "d2338ab6a173f17e1378f82d1b0cc9828c0205da41d49caf2d17daec28549751",
    "27d2c050e626d9d7a44e98552c31fe0376eb0d754811253dec033f993d983fff",
})
MAX_REQUESTS = 128
MAX_TEMP_ENTRIES = 4096
REQUEST_MAX_AGE = 30 * 24 * 60 * 60


def resource_directory() -> Path:
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / RESOURCE_FOLDER


def _validate_manifest(value: dict) -> dict:
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "worker_sha256", "allowed_previous_sha256"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise UpdateError("修复工具的内嵌清单格式无效。")
    previous = value["allowed_previous_sha256"]
    if (not isinstance(previous, list) or not 1 <= len(previous) <= 64
            or not isinstance(value["worker_sha256"], str)
            or not _SHA.fullmatch(value["worker_sha256"])
            or any(not isinstance(item, str) or not _SHA.fullmatch(item) for item in previous)):
        raise UpdateError("修复工具的内嵌 SHA-256 白名单无效。")
    return {**value, "allowed_previous_sha256": list(previous)}


def _verify_file(path: Path, digest: str) -> None:
    _no_links(path)
    if not path.is_file() or _hash(path) != digest:
        raise UpdateError(f"文件校验失败，已停止：{path}")


def _copy_verified(source: Path, destination: Path, digest: str) -> None:
    _no_links(source)
    _no_links(destination)
    # Exclusive creation never overwrites an earlier backup or staging file.
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    _verify_file(destination, digest)


def _check_transaction(updater: SafeUpdater) -> None:
    _no_links(updater.journal_path)
    journal = updater._journal()
    if journal and journal["state"] not in {"committed", "rolled_back"}:
        raise UpdateError("此安装存在尚未完成的程序更新，已停止修复；请先处理原更新事务。")


def _embedded_manifest(resource_dir=None) -> tuple[Path, dict]:
    resources = _absolute(resource_directory() if resource_dir is None else resource_dir)
    path = _absolute(resources / "manifest.json")
    if path.stat().st_size > 64 * 1024:
        raise UpdateError("修复工具的内嵌清单过大。")
    manifest = _validate_manifest(_read_json(path))
    _verify_file(resources / WORKER_NAME, manifest["worker_sha256"])
    return resources, manifest


def _local_path(value) -> Path:
    if (not isinstance(value, (str, os.PathLike)) or not os.fspath(value)
            or not Path(value).is_absolute()
            or os.fspath(value).replace("/", "\\").startswith("\\\\")):
        raise UpdateError("修复需要有效的本机绝对路径，不能使用网络或相对路径。")
    return _absolute(value)


def _context(install, config, data=None, *, port=6778) -> dict:
    install, config = _local_path(install), _local_path(config)
    if not config.is_file() or config.stat().st_size > 64 * 1024:
        raise UpdateError("原配置文件不存在或无法确认，已停止；不会创建新配置或新资料库。")
    value = _read_json(config)
    data = _local_path(data if data is not None else value.get("data_dir"))
    if not data.is_dir() or type(port) is not int or not 0 < port < 65536:
        raise UpdateError("原资料目录或本机端口无效，已停止。")
    return {"install_dir": str(install), "config_path": str(config),
            "data_dir": str(data), "port": port}


def _request_context(path: Path) -> dict:
    path = _local_path(path)
    if (path.name != "request.json" or not path.parent.name.startswith("PocketMemory-update-")
            or path.stat().st_size > 64 * 1024):
        raise UpdateError("无法确认原更新请求。")
    value = _read_json(path)
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise UpdateError("原更新请求版本未知。")
    result = _context(value.get("install_dir"), value.get("config_path"),
                      value.get("data_dir"), port=value.get("port"))
    result["request_path"] = str(path)
    return result


def _recent_requests(roots=None) -> list[Path]:
    if roots is None:
        roots = [os.environ.get("TEMP"), os.environ.get("TMP")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(str(Path(local) / "Temp"))
    found, seen_roots, examined = {}, set(), 0
    for selected in roots:
        if not selected:
            continue
        root = _local_path(selected)
        key = str(root).casefold()
        if key in seen_roots or not root.is_dir():
            continue
        seen_roots.add(key)
        with os.scandir(root) as entries:
            for entry in entries:
                examined += 1
                if examined > MAX_TEMP_ENTRIES:
                    raise UpdateError("临时目录项目过多，无法安全自动定位。请把修复工具放到原 PocketMemory.exe 旁运行。")
                if not entry.name.startswith("PocketMemory-update-"):
                    continue
                try:
                    path = _local_path(Path(entry.path) / "request.json")
                    if not path.is_file():
                        continue
                    stamp = path.stat().st_mtime
                    if not 0 <= time.time() - stamp <= REQUEST_MAX_AGE:
                        continue
                    found[str(path).casefold()] = (stamp, path)
                except (OSError, UpdateError):
                    continue
                if len(found) > MAX_REQUESTS:
                    raise UpdateError("发现过多原更新记录，请把修复工具放到原 PocketMemory.exe 旁运行。")
    return [item[1] for item in sorted(found.values(), key=lambda item: item[0], reverse=True)]


def _preflight(context: dict, manifest: dict, allowed_main_sha256=None) -> None:
    install = _local_path(context["install_dir"])
    main = _local_path(install / "PocketMemory.exe")
    worker = _local_path(install / "_internal" / WORKER_NAME)
    allowed = KNOWN_MAIN_SHA256 if allowed_main_sha256 is None else allowed_main_sha256
    if not main.is_file() or _hash(main) not in allowed:
        raise UpdateError("原 PocketMemory.exe 不属于已核验版本，已停止；不会启动未知程序。")
    if (not worker.is_file() or _hash(worker) not in
            {*manifest["allowed_previous_sha256"], manifest["worker_sha256"]}):
        raise UpdateError("原更新组件不属于此工具支持的版本，已停止。")
    updater = SafeUpdater(install, protected_data_dirs=[context["data_dir"]],
                          config_paths=[context["config_path"]])
    _check_transaction(updater)
    updater._protect([worker, updater.state])


def discover_installation(*, install_dir=None, adjacent_dir=None, roots=None,
                         manifest=None, allowed_main_sha256=None) -> dict:
    """Read-only, bounded discovery. Never infer an install from a version label."""
    if manifest is None:
        _, manifest = _embedded_manifest()
    manifest = _validate_manifest(manifest)
    adjacent = _local_path(adjacent_dir or Path(sys.executable).parent)
    explicit = _local_path(install_dir) if install_dir is not None else None
    preferred = explicit or (adjacent if (adjacent / "PocketMemory.exe").is_file() else None)
    candidates = {}
    for request in _recent_requests(roots):
        try:
            value = _request_context(request)
            if preferred is not None and _local_path(value["install_dir"]) != preferred:
                continue
            # Invalid stale requests cannot choose another executable. Once an
            # identity matches, pending transactions are checked below, not skipped.
            install = _local_path(value["install_dir"])
            main, worker = install / "PocketMemory.exe", install / "_internal" / WORKER_NAME
            allowed = KNOWN_MAIN_SHA256 if allowed_main_sha256 is None else allowed_main_sha256
            if not main.is_file() or _hash(main) not in allowed or not worker.is_file():
                continue
            key = tuple(str(_local_path(value[field])).casefold()
                        for field in ("install_dir", "config_path", "data_dir"))
            candidates.setdefault(key, value)
        except (OSError, UpdateError, TypeError):
            continue
    if len(candidates) > 1:
        raise UpdateError("发现多个安装目录或资料配置，不能替您选择。请把修复工具放到要修复的原 PocketMemory.exe 旁；若仍提示，请保留诊断记录。")
    if candidates:
        selected = next(iter(candidates.values()))
    elif preferred is not None:
        configured = os.environ.get("PM_UPDATE_CONFIG_PATH")
        if not configured:
            local = os.environ.get("LOCALAPPDATA")
            if not local:
                raise UpdateError("未找到原配置位置，请保留更新失败诊断记录。")
            configured = str(_local_path(local) / "PocketMemory" / "config.json")
        selected = _context(preferred, configured)
    else:
        raise UpdateError("没有找到可确认的原安装。请把此修复工具放到原 PocketMemory.exe 同一目录后双击，不用删除或重新安装。")
    _preflight(selected, manifest, allowed_main_sha256)
    return selected


def _confirm_close(context: dict) -> bool:
    message = ("已找到原 Pocket Memory：\n" + context["install_dir"] +
               "\n\n请先保存编辑并关闭 Pocket Memory 桌面窗口，再点“确定”。\n"
               "工具会正常退出后台服务、修复更新组件并重新打开原程序。"
               "不会强制结束进程，模型、资料和配置不会被替换。")
    if os.name != "nt":
        raise UpdateError("自动关闭与重新打开仅支持 Windows；脚本检查请使用 --quiet。")
    return ctypes.windll.user32.MessageBoxW(None, message, "Pocket Memory 更新组件修复", 0x21) == 1


def _launch_environment(context: dict) -> dict:
    env = dict(os.environ)
    env["PM_UPDATE_CONFIG_PATH"] = context["config_path"]
    # A PyInstaller onefile parent must not make the original application reuse
    # its temporary extraction directory or inherited launcher readiness token.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for key in tuple(env):
        if (key.startswith(("_PYI_", "PM_UPDATE_READY_", "PM_UPDATE_DESKTOP_"))
                or key in {"PM_UPDATE_HEALTH_TOKEN", "PM_UPDATE_PROBE"}):
            env.pop(key, None)
    return env


def stop_original(context: dict, *, timeout=30) -> None:
    """Request normal daemon shutdown; timeout never kills a user process."""
    install = _local_path(context["install_dir"])
    # Unknown process visibility is an error, not proof that the app has exited.
    pids = running_install_processes(install)
    if not pids:
        return
    command = [str(install / "PocketMemory.exe"), "--quit", "--data-dir", context["data_dir"]]
    proc = subprocess.Popen(command, cwd=str(install), env=_launch_environment(context),
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + timeout
    try:
        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("正常退出后台服务超时；未强制结束任何进程，也未替换更新组件。请关闭原窗口后再试。") from exc
    while running_install_processes(install):
        if time.monotonic() >= deadline:
            raise UpdateError("原 Pocket Memory 窗口或后台仍在运行；未替换更新组件。请关闭原窗口后再试。")
        time.sleep(0.25)


def reopen_original(context: dict) -> int:
    install = _local_path(context["install_dir"])
    command = [str(install / "PocketMemory.exe"), "--data-dir", context["data_dir"],
               "--host", "127.0.0.1", "--port", str(context["port"])]
    return subprocess.Popen(command, cwd=str(install), env=_launch_environment(context),
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).pid


@contextlib.contextmanager
def _selected_config(context):
    previous = os.environ.get("PM_UPDATE_CONFIG_PATH")
    os.environ["PM_UPDATE_CONFIG_PATH"] = context["config_path"]
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PM_UPDATE_CONFIG_PATH", None)
        else:
            os.environ["PM_UPDATE_CONFIG_PATH"] = previous


def bootstrap_original(context: dict, *, resource_dir=None, manifest=None,
                       allowed_main_sha256=None, reopen=True) -> dict:
    resources, embedded = _embedded_manifest(resource_dir)
    manifest = embedded if manifest is None else _validate_manifest(manifest)
    _preflight(context, manifest, allowed_main_sha256)
    if not _confirm_close(context):
        return {"state": "cancelled", "message": "已取消；未关闭程序或修改任何文件。"}
    # Revalidate after user interaction and again before running the original EXE.
    _preflight(context, manifest, allowed_main_sha256)
    stop_original(context)
    with _selected_config(context):
        result = repair_installed_worker(context["install_dir"], resource_dir=resources, manifest=manifest)
    _preflight(context, manifest, allowed_main_sha256)
    if reopen:
        result["reopened_pid"] = reopen_original(context)
        result["message"] = "更新组件已修复，正在重新打开原 Pocket Memory。窗口出现后，请在“设置 → 软件更新”中点击“更新并重启”。模型、资料和配置保持原样。"
    return result


def repair_installed_worker(install_dir, *, resource_dir=None, manifest=None) -> dict:
    """Repair a known worker under the updater lock; API overrides are for tests/builds."""
    install = _absolute(install_dir)
    executable = _absolute(install / "PocketMemory.exe")
    if not install.is_dir() or not executable.is_file():
        raise UpdateError("请把修复工具放在原 PocketMemory.exe 同一目录，再双击运行。")
    target = _absolute(install / "_internal" / WORKER_NAME)
    if not target.is_file():
        raise UpdateError("原安装缺少更新组件，无法进行此项修复。")
    resources = _absolute(resource_directory() if resource_dir is None else resource_dir)
    payload = _absolute(resources / WORKER_NAME)
    if manifest is None:
        manifest_path = _absolute(resources / "manifest.json")
        if manifest_path.stat().st_size > 64 * 1024:
            raise UpdateError("修复工具的内嵌清单过大。")
        manifest = _read_json(manifest_path)
    manifest = _validate_manifest(manifest)
    replacement_digest = manifest["worker_sha256"]
    _verify_file(payload, replacement_digest)
    configured_path = os.environ.get("PM_UPDATE_CONFIG_PATH")
    updater = SafeUpdater(install, config_paths=[configured_path] if configured_path else ())
    repair_root = _absolute(updater.state / RESOURCE_FOLDER)
    updater._protect([target, repair_root])

    with updater._locked():
        _check_transaction(updater)
        updater._not_running()
        _no_links(target)
        previous_digest = _hash(target)
        if previous_digest == replacement_digest:
            return {"state": "already_repaired", "worker": str(target), "sha256": replacement_digest,
                    "message": "此安装的更新组件已经修复，无需重复操作。请手动打开 Pocket Memory。"}
        if previous_digest not in manifest["allowed_previous_sha256"]:
            raise UpdateError("原更新组件不属于此工具支持的版本，已停止；未替换任何程序文件。")

        _no_links(repair_root)
        repair_root.mkdir(exist_ok=True)
        attempt = repair_root / uuid.uuid4().hex
        _no_links(attempt)
        attempt.mkdir()  # A new, same-volume directory; never reuse an unknown backup.
        backup = attempt / "original-worker.exe"
        staged = attempt / "replacement.tmp"
        restore = attempt / "restore.tmp"
        record_path = attempt / "repair.json"
        _copy_verified(target, backup, previous_digest)
        record = {"schema_version": 1, "state": "prepared", "worker": str(target),
                  "previous_sha256": previous_digest, "sha256": replacement_digest,
                  "backup": str(backup)}
        replace_attempted = False
        try:
            _copy_verified(payload, staged, replacement_digest)
            _atomic_json(record_path, record)
            # Recheck after copying: the app or another actor may have changed state.
            _check_transaction(updater)
            updater._not_running()
            updater._protect([target, attempt])
            _verify_file(target, previous_digest)
            _verify_file(staged, replacement_digest)
            replace_attempted = True
            os.replace(staged, target)
            _verify_file(target, replacement_digest)
            _atomic_json(record_path, {**record, "state": "repaired"})
        except Exception as exc:
            try:
                if replace_attempted:
                    _no_links(target)
                    current = _hash(target) if target.is_file() else None
                    if current != previous_digest:
                        if current not in {None, replacement_digest}:
                            raise UpdateError("更新组件已被其他操作改变，不能覆盖未知文件。")
                        _check_transaction(updater)
                        updater._not_running()
                        updater._protect([target, attempt])
                        _verify_file(backup, previous_digest)
                        _copy_verified(backup, restore, previous_digest)
                        _no_links(target)
                        latest = _hash(target) if target.is_file() else None
                        if latest != current:
                            raise UpdateError("恢复期间更新组件又发生变化，不能覆盖未知文件。")
                        os.replace(restore, target)
                        _verify_file(target, previous_digest)
                _atomic_json(record_path, {**record, "state": "not_repaired", "error": str(exc)})
            except Exception as recovery:
                raise UpdateError(f"修复失败：{exc}；自动恢复未完成：{recovery}。原组件备份保留在：{backup}") from exc
            raise UpdateError(f"修复未完成，本工具未写入新组件或已恢复备份：{exc}。备份：{backup}") from exc
        return {**record, "state": "repaired",
                "message": "更新组件已修复，原组件备份已保留。请手动打开 Pocket Memory，在设置中重试软件更新。"}


def _write_json(value: dict) -> None:
    output = json.dumps(value, ensure_ascii=False) + "\n"
    if sys.stdout is not None:
        sys.stdout.write(output)
        sys.stdout.flush()
    elif os.name == "nt":
        # Windowed PyInstaller sets sys.stdout=None even when a test redirects
        # the Win32 standard handle to a pipe. Preserve --quiet's JSON contract.
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteFile.restype = wintypes.BOOL
        handle = kernel.GetStdHandle(-11 & 0xFFFFFFFF)
        if handle and handle != ctypes.c_void_p(-1).value:
            raw = output.encode("utf-8")
            buffer = ctypes.create_string_buffer(raw)
            written = wintypes.DWORD()
            if not kernel.WriteFile(handle, buffer, len(raw), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value != len(raw):
                raise OSError("JSON 输出未完整写入。")


def _show_message(message: str, failed: bool) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Pocket Memory 更新组件修复", 0x10 if failed else 0x40)
    elif sys.stdout is not None:
        print(message)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pocket Memory 离线更新组件修复")
    parser.add_argument("--install-dir", type=Path, help="原安装目录；默认从相邻程序或本机更新记录定位")
    parser.add_argument("--quiet", action="store_true", help="仅修复并输出 JSON，不关闭或启动程序，不显示提示窗口")
    parser.add_argument("--no-reopen", action="store_true", help="修复完成后不重新打开原程序")
    args = parser.parse_args(argv)
    try:
        if args.quiet and args.install_dir is not None:
            # Preserve the original repair-only automation/API contract.
            result = repair_installed_worker(args.install_dir)
        else:
            resources, manifest = _embedded_manifest()
            context = discover_installation(install_dir=args.install_dir, manifest=manifest)
            if args.quiet:
                with _selected_config(context):
                    result = repair_installed_worker(context["install_dir"], resource_dir=resources, manifest=manifest)
            else:
                result = bootstrap_original(context, resource_dir=resources, manifest=manifest,
                                            reopen=not args.no_reopen)
        failed = False
    except Exception as exc:
        result = {"state": "error", "message": f"修复未完成：{exc}"}
        failed = True
    if args.quiet:
        _write_json(result)
    else:
        _show_message(result["message"], failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
