# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Application-owned update checks and downloads. No user-selected URLs or paths.

Only the verified installed executor is used. Downloaded code is never used as an update
hook. The HTTPS feed is a trusted publisher boundary, not a signed-feed scheme.
Source checkouts can preview availability but cannot install or exit the app.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from pocket_memory.safe_update import (MANIFEST_NAME, SafeUpdater, UpdateError,
    _absolute, _atomic_json, _hash, _no_links, _overlap, _relative, _verify,
    is_program_path, validate_manifest)
from pocket_memory.version import APP_VERSION
from pocket_memory.update_component import (WORKER_RELATIVE_PATH, inspect_installed_worker,
                                            recovery_guidance, component_error)


TRUSTED_HOST = "download-files-1312623621.cos.ap-beijing.myqcloud.com"
# Old 1.0.13/1.0.14 clients cannot consume worker-free v2 manifests. Keep their
# feed intact; a future v2 full installation subscribes to this separate channel.
FEED_URL = f"https://{TRUSTED_HOST}/PocketMemory-update-v2.json"
MAX_FEED_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 3 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 40000
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger(__name__)


class UpdateFeedUnavailable(UpdateError):
    """A transient feed-access failure, distinct from metadata/install validation."""


def version_key(value: str) -> tuple:
    if not isinstance(value, str) or len(value) > 100:
        raise UpdateError("更新版本号无效。")
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?", value)
    if not match:
        raise UpdateError("更新版本号必须为有效的 SemVer。")
    identifiers = []
    for part in (match[4] or "").split(".") if match[4] else []:
        if not part or (part.isdigit() and len(part) > 1 and part.startswith("0")):
            raise UpdateError("更新预发布版本号无效。")
        identifiers.append((0, int(part)) if part.isdigit() else (1, part))
    if match[5] and any(not part for part in match[5].split(".")):
        raise UpdateError("更新构建版本号无效。")
    return (*map(int, match.group(1, 2, 3)), 1 if not match[4] else 0, tuple(identifiers))


def trusted_url(url: str, *, package=False) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise UpdateError("更新地址无效。")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != TRUSTED_HOST
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise UpdateError("更新地址必须是固定腾讯云发布域名的 HTTPS 地址。")
    decoded = urllib.parse.unquote(parsed.path)
    _relative(decoded.lstrip("/"))
    if not parsed.path.startswith("/") or parsed.path.startswith("//") or "%" in decoded:
        raise UpdateError("更新地址路径无效。")
    if package and (not decoded.startswith("/PocketMemory-") or not decoded.endswith(".zip")):
        raise UpdateError("更新包必须使用 PocketMemory- 开头的 ZIP 发布路径。")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateError("发布地址发生重定向，已停止更新以保护下载来源。")


def _open_https(url):
    trusted_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "PocketMemory-Updater/1", "Accept-Encoding": "identity"})
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=30)


def _json_bytes(raw: bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise UpdateError("更新元数据包含重复字段。")
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("更新元数据不是有效 JSON。") from exc
    if not isinstance(value, dict):
        raise UpdateError("更新元数据根节点必须为对象。")
    return value


def validate_feed(value: dict) -> dict:
    base = {"schema_version", "app_id", "published"}
    if (value.get("schema_version") != 1 or type(value.get("schema_version")) is not int
            or value.get("app_id") != "pocket-memory" or type(value.get("published")) is not bool):
        raise UpdateError("发布清单不属于受支持的 Pocket Memory 更新。")
    if not value["published"]:
        if set(value) != base:
            raise UpdateError("未发布清单不能声明可下载版本。")
        return value
    if set(value) != base | {"version", "package", "notes"}:
        raise UpdateError("发布清单字段不完整或包含不支持的操作。")
    version_key(value["version"])
    package = value["package"]
    if not isinstance(package, dict) or set(package) != {"url", "size", "sha256"}:
        raise UpdateError("发布清单缺少完整的程序包校验信息。")
    trusted_url(package["url"], package=True)
    if type(package["size"]) is not int or not 0 < package["size"] <= MAX_ARCHIVE_BYTES:
        raise UpdateError("程序更新包大小超出允许范围。")
    if not isinstance(package["sha256"], str) or not _HASH_PATTERN.fullmatch(package["sha256"]):
        raise UpdateError("程序更新包 SHA-256 无效。")
    if not isinstance(value["notes"], str) or len(value["notes"]) > 12000:
        raise UpdateError("更新说明无效。")
    return value


def fetch_feed() -> dict:
    try:
        with _open_https(FEED_URL) as response:
            raw = response.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"schema_version": 1, "app_id": "pocket-memory", "published": False}
        raise UpdateFeedUnavailable("暂时无法连接发布服务器。") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise UpdateError("无法验证更新服务器的安全连接，请稍后重试。") from exc
        raise UpdateFeedUnavailable("暂时无法连接发布服务器。") from exc
    except ssl.SSLError as exc:
        raise UpdateError("无法验证更新服务器的安全连接，请稍后重试。") from exc
    except (TimeoutError, ConnectionError, OSError) as exc:
        raise UpdateFeedUnavailable("暂时无法连接发布服务器。") from exc
    if len(raw) > MAX_FEED_BYTES:
        raise UpdateError("发布清单过大。")
    return validate_feed(_json_bytes(raw))


def download_package(package: dict, destination: Path, progress=lambda done, total: None) -> None:
    trusted_url(package["url"], package=True)
    _no_links(destination)
    if destination.exists():
        raise UpdateError("更新下载目标已存在。")
    digest, count = hashlib.sha256(), 0
    with _open_https(package["url"]) as response, destination.open("xb") as writer:
        while True:
            block = response.read(min(1024 * 1024, package["size"] - count + 1))
            if not block:
                break
            count += len(block)
            if count > package["size"] or count > MAX_ARCHIVE_BYTES:
                raise UpdateError("下载内容超过发布声明大小，已停止。")
            writer.write(block)
            digest.update(block)
            progress(count, package["size"])
        writer.flush()
        os.fsync(writer.fileno())
    if count != package["size"] or digest.hexdigest() != package["sha256"]:
        raise UpdateError("程序包下载不完整或校验失败，原程序未改动。")


def extract_update_archive(archive: Path, destination: Path, expected_version: str) -> dict:
    """Validate the whole ZIP directory and manifest before extracting any file."""
    _no_links(archive)
    _no_links(destination)
    if destination.exists():
        raise UpdateError("更新解压目录必须为全新目录。")
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_FILES:
            raise UpdateError("更新包文件数量超出允许范围。")
        paths, total, seen, spellings, files_seen = {}, 0, set(), {}, set()
        for info in infos:
            if info.orig_filename != info.filename:
                raise UpdateError("更新包包含被截断的路径。")
            rel = _relative(info.filename.rstrip("/"))
            mode = (info.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if (kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or info.flag_bits & 1
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise UpdateError("更新包包含链接、加密文件或不支持的压缩类型。")
            if rel.casefold() in seen:
                raise UpdateError("更新包包含重复或大小写冲突路径。")
            seen.add(rel.casefold())
            parts = rel.split("/")
            for length in range(1, len(parts) + 1):
                prefix = "/".join(parts[:length])
                previous = spellings.setdefault(prefix.casefold(), prefix)
                if previous != prefix or (length < len(parts) and prefix.casefold() in files_seen):
                    raise UpdateError("更新包包含父目录拼写或文件/目录冲突。")
            if not info.is_dir():
                if any(key.startswith(rel.casefold() + "/") for key in spellings):
                    raise UpdateError("更新包文件与目录发生冲突。")
                files_seen.add(rel.casefold())
            if info.file_size > MAX_EXPANDED_BYTES or (info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 300):
                raise UpdateError("更新包压缩比或文件大小异常。")
            total += info.file_size
            if total > MAX_EXPANDED_BYTES:
                raise UpdateError("更新包展开大小超出允许范围。")
            if not info.is_dir():
                paths[rel] = info
        manifests = [path for path in paths if path == MANIFEST_NAME or path.count("/") == 1 and path.endswith("/" + MANIFEST_NAME)]
        if len(manifests) != 1:
            raise UpdateError("更新包必须含唯一程序更新清单。")
        manifest_path = manifests[0]
        prefix = manifest_path[:-len(MANIFEST_NAME)]
        if prefix and any(not path.startswith(prefix) for path in paths):
            raise UpdateError("更新包含混杂的顶层目录。")
        relative = {path[len(prefix):]: info for path, info in paths.items()}
        manifest_info = relative[MANIFEST_NAME]
        if manifest_info.file_size > 2 * 1024 * 1024:
            raise UpdateError("程序文件清单过大。")
        manifest = validate_manifest(_json_bytes(bundle.read(manifest_info)))
        if manifest["version"] != expected_version:
            raise UpdateError("程序包版本与发布清单不一致。")
        expected = {"payload/" + item["path"]: item for item in manifest["files"]}
        # Backward-compatible documentation/manual helper is ignored, never run.
        ignored = {"PocketMemoryUpdate.exe", "更新说明.txt", "README.txt"}
        if set(relative) - {MANIFEST_NAME} - ignored != set(expected):
            raise UpdateError("程序包文件与允许的更新清单不一致。")
        for path, item in expected.items():
            if not is_program_path(item["path"]) or item["path"].lower().endswith(".gguf"):
                raise UpdateError("应用内更新只接收程序文件，不能安装模型包。")
            if relative[path].file_size != item["size"]:
                raise UpdateError("程序包文件大小与清单不一致。")
        if shutil.disk_usage(destination.parent).free < total + archive.stat().st_size + 1024 * 1024:
            raise UpdateError("临时磁盘空间不足。")
        destination.mkdir()
        _atomic_json(destination / MANIFEST_NAME, manifest)
        for path, item in expected.items():
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with bundle.open(relative[path]) as reader, target.open("xb") as writer:
                while True:
                    block = reader.read(min(1024 * 1024, item["size"] - count + 1))
                    if not block:
                        break
                    count += len(block)
                    if count > item["size"]:
                        raise UpdateError("解压内容超过文件声明大小。")
                    writer.write(block)
            _verify(target, item)
    return manifest


def worker_identity_matches(pid: int, launched_pid: int, executable: Path) -> bool:
    """Account for a onefile bootloader parent, without trusting JSON ancestry."""
    if type(pid) is not int or pid <= 0:
        return False
    if pid == launched_pid:
        return True  # The still-running process returned by our own Popen.
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t), ("module", wintypes.DWORD),
                    ("threads", wintypes.DWORD), ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
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
        return False
    parent_matches = False
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.pid == pid:
                parent_matches = entry.parent == launched_pid
                break
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if not parent_matches:
        return False
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        try:
            return _absolute(buffer.value) == _absolute(executable)
        except (OSError, UpdateError):
            return False
    finally:
        kernel.CloseHandle(handle)


def wait_for_worker_ready(worker, ready: Path, token: str, executable: Path, *, timeout=15.0):
    """Read the atomic receipt without treating a transient Windows lock as failure.

    Only receipt visibility/read access is retried. Invalid data, tokens and
    process identities never authorize go.json or application shutdown.
    The deadline must remain below the installed worker's 20-second go wait.
    """
    deadline = time.monotonic() + timeout
    last_access_error = None
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            raise UpdateError("自动更新组件未能启动；原程序仍保留。")
        try:
            _no_links(ready)
            with ready.open("rb") as handle:
                raw = handle.read(4097)
        except FileNotFoundError:
            # The atomic writer has not published the receipt yet.
            pass
        except PermissionError as exc:
            # Windows rename/scanner handles may deny reads briefly even
            # after the final name is visible. Do not relax file permissions.
            last_access_error = exc
        else:
            last_access_error = None
            if len(raw) > 4096:
                raise UpdateError("自动更新组件的准备信息过大；原程序未替换。")
            payload = _json_bytes(raw)
            if (payload.get("token") == token
                    and worker_identity_matches(payload.get("pid"), worker.pid, executable)):
                return
        time.sleep(0.1)
    if last_access_error is not None:
        logger.warning("Update readiness receipt remained unreadable: %s", ready,
                       exc_info=(type(last_access_error), last_access_error, last_access_error.__traceback__))
        raise UpdateError("更新准备文件持续无法读取，可能被占用或受权限限制；原程序和资料未改动。请稍后重试，若仍失败请保留更新诊断记录。") from last_access_error
    raise UpdateError("自动更新组件准备超时；原程序未替换。")


class AppUpdateService:
    def __init__(self, config, data_dir: Path, *, server_port: int, exit_callback, current_version: str = APP_VERSION):
        self.config = config
        self.data_dir = _absolute(data_dir)
        self.port = int(server_port)
        self.exit_callback = exit_callback
        version_key(current_version)
        self.current_version = current_version
        self.preview = not getattr(sys, "frozen", False)
        self.executable = os.path.abspath(sys.executable)
        self.install = _absolute(Path(sys.executable).parent) if not self.preview else None
        self._lock = threading.RLock()
        self._offer = None
        self._busy = False
        self._state = {"state": "preview" if self.preview else "idle", "current_version": current_version,
                       "preview": self.preview, "can_update": False, "has_update": False,
                       "message": "" if self.preview else "尚未检查更新。"}
        if self.install is not None:
            result_path = self.install / ".pocket-memory-update/auto-update-result.json"
            if result_path.is_file():
                try:
                    _no_links(result_path)
                    result = _json_bytes(result_path.read_bytes()[:64 * 1024])
                    if result.get("state") in {"complete", "rolled_back", "not_updated", "error", "recovery_required"}:
                        self._state["last_result"] = result
                except (OSError, ValueError, UpdateError):
                    pass
            health = self.refresh_installation_integrity()
            if health['status'] != 'ok':
                self._set(state='error', message=component_error(health))

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def refresh_installation_integrity(self) -> dict:
        if self.install is None:
            return {'status': 'not_applicable', 'verification': 'source_checkout'}
        from .embedded_update import inspect_executor
        diagnostic = inspect_executor(self.install)
        diagnostic.update(install_dir=str(self.install), executable=self.executable,
                          server_pid=os.getpid(), current_version=self.current_version)
        recovery = recovery_guidance(diagnostic) if diagnostic['status'] != 'ok' else None
        self._set(update_component=diagnostic, installation_integrity=diagnostic, recovery=recovery)
        if diagnostic['status'] == 'ok':
            logger.debug('Installed update component: %s', json.dumps(diagnostic, ensure_ascii=False))
        else:
            logger.error('Installed update component unavailable: %s', json.dumps(diagnostic, ensure_ascii=False))
            self._set(can_update=False)
        return diagnostic

    def _require_update_component(self) -> Path:
        diagnostic = self.refresh_installation_integrity()
        if diagnostic['status'] != 'ok':
            raise UpdateError(component_error(diagnostic))
        return Path(diagnostic['path'])

    def _begin(self, target, initial=None):
        with self._lock:
            if self._busy:
                return self.status()
            self._busy = True
            if initial:
                self._state.update(initial)
        def work():
            try:
                target()
            except Exception as exc:
                logger.exception('Application update failed (version=%s, executable=%s, install=%s)',
                                 self.current_version, self.executable, self.install)
                self._set(state="error", can_update=False, message=str(exc))
            finally:
                with self._lock:
                    self._busy = False
        threading.Thread(target=work, name="pocket-memory-update", daemon=True).start()
        return self.status()

    def check_async(self) -> dict:
        def check():
            self._set(state="checking", can_update=False, message="正在检查官方发布信息…")
            # Local integrity is independent of network/version availability.
            if not self.preview:
                self._require_update_component()
            try:
                feed = fetch_feed()
            except UpdateFeedUnavailable:
                # A failed check must not retain an older installable offer or
                # claim that the installed version was successfully verified.
                logger.warning("Update feed unavailable; no update offered", exc_info=True)
                with self._lock:
                    self._offer = None
                    self._state.update(state="unavailable", has_update=False, can_update=False,
                                       latest_version=None, notes="", package_bytes=None,
                                       message="暂无可用更新。")
                return
            self._offer = feed if feed["published"] else None
            if not feed["published"]:
                self._set(state="unpublished", has_update=False, latest_version=None,
                          message="尚未发布可安装的程序更新。")
                return
            newer = version_key(feed["version"]) > version_key(self.current_version)
            self._set(has_update=newer,
                      latest_version=feed["version"], notes=feed["notes"], package_bytes=feed["package"]["size"],
                      can_update=False)
            if newer and not self.preview:
                self._require_update_component()
            self._set(state="available" if newer else "current", can_update=newer and not self.preview,
                      message=("发现新版本。" if self.preview and newer else
                               "发现新版本，可以更新程序。" if newer else "当前版本无需更新。"))
        return self._begin(check, dict(state='checking', can_update=False,
                                     message='正在检查官方发布信息…'))

    def start_update(self) -> dict:
        with self._lock:
            if self.preview:
                raise UpdateError("当前运行环境不支持安装更新。")
            if self._busy:
                return self.status()
            if not self._offer or not self._state.get("can_update"):
                raise UpdateError("请先检查并确认存在已发布的新版本。")
            offered = self._offer
            context = (self.data_dir, self.config.path, getattr(self.config, "data_dir", None))
            return self._begin(lambda: self._download_and_handoff(offered, context),
                               dict(state='downloading', can_update=False, progress=0,
                                    message='正在确认发布信息并准备程序更新…'))

    def _download_and_handoff(self, offered, context):
        active_data, config_path, configured_data = context
        # Recheck even after a successful check: files can move/disappear meanwhile.
        helper = self._require_update_component()
        feed = fetch_feed()
        if feed != offered or not feed["published"] or version_key(feed["version"]) <= version_key(self.current_version):
            raise UpdateError("发布信息已变化，请重新检查更新。")
        work = _absolute(tempfile.mkdtemp(prefix="PocketMemory-update-"))
        if _overlap(work, self.install) or _overlap(work, active_data):
            raise UpdateError("自动更新临时目录与安装或资料目录重叠，已停止。")
        self._set(state="downloading", can_update=False, progress=0, message="正在下载程序更新；本机模型无需重复下载。")
        def progress(done, total):
            self._set(downloaded_bytes=done, package_bytes=total, progress=int(done * 100 / total))
        archive = work / "program-update.zip"
        download_package(feed["package"], archive, progress)
        self._set(state="verifying", message="正在校验程序包和本机模型…")
        source = work / "source"
        extract_update_archive(archive, source, feed["version"])
        prepared = SafeUpdater(self.install, protected_data_dirs=[active_data],
                               config_paths=[config_path]).check(source, read_only=True)
        if prepared["previous_transaction"] == "pending_health":
            raise UpdateError("上次程序更新尚未完成健康确认，已停止本次更新。")
        from .embedded_update import stage_executor, clean_worker_environment
        self._set(state="verifying", message="正在准备并校验更新执行环境…")
        worker_command, staged_helper = stage_executor(self.install, work)
        token = secrets.token_hex(32)
        if (self.data_dir != active_data or self.config.path != config_path
                or getattr(self.config, "data_dir", None) != configured_data):
            raise UpdateError("资料库或配置在下载期间发生变化，已停止更新；请重新检查。")
        request = {"schema_version": 1, "install_dir": str(self.install), "data_dir": str(active_data),
                   "config_path": str(_absolute(config_path)), "source_dir": str(source),
                   "target_version": feed["version"], "parent_pid": os.getpid(),
                   "launcher_pid": os.getppid(), "port": self.port, "health_token": token,
                   "local_app_data": os.environ.get("LOCALAPPDATA"), "debug": "--debug" in sys.argv}
        request_path = work / "request.json"
        _atomic_json(request_path, request)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with (work / "worker.log").open("ab") as log:
            worker = subprocess.Popen([*worker_command, "--request", str(request_path)],
                                      cwd=str(staged_helper.parent), stdout=log, stderr=subprocess.STDOUT,
                                      env=clean_worker_environment(),
                                      creationflags=creationflags, close_fds=True)
        try:
            wait_for_worker_ready(worker, work / "ready.json", token, staged_helper)
            _atomic_json(work / "go.json", {"token": token})
        except Exception:
            # No shutdown has been requested. Stop only the process we started;
            # a onefile child also has its own bounded, no-go cancellation.
            try:
                if worker.poll() is None:
                    worker.terminate()
            except OSError:
                logger.warning("Could not stop unconfirmed update worker", exc_info=True)
            raise
        self._set(state="restarting", shutdown_requested=True,
                  message="程序包已准备好，正在退出、更新并自动重新打开…")
        # Give the launcher watcher/frontend one polling interval to notice.
        time.sleep(1.5)
        self.exit_callback()
