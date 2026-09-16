"""Pocket Memory 守护进程管理。

让 HTTP server（含模型）作为独立进程常驻，webview 窗口只管前端展示。
关闭窗口时 server 继续运行，模型不卸载；二次启动秒开窗口。

lock 文件结构（JSON）：
{
  "pid": 12345,
  "host": "127.0.0.1",
  "port": 54321,
  "url": "http://127.0.0.1:54321",
  "started_at": "2026-08-12T10:00:00"
}
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
import urllib.request
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path

from pocket_memory.version import APP_VERSION

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".pocket-memory.lock"
READY_TIMEOUT_SECONDS = 90  # 模型加载较慢，给足时间
READY_PROBE_INTERVAL = 0.5
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def get_lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / LOCK_FILENAME


def read_lock(data_dir: Path) -> dict | None:
    """读取 lock 文件。返回 None 表示无 lock 或格式损坏。"""
    lock_path = get_lock_path(data_dir)
    if not lock_path.is_file():
        return None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "pid" in payload and "url" in payload:
            return payload
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("lock 文件损坏，忽略: %s", exc)
    return None


def write_lock(data_dir: Path, pid: int, host: str, port: int) -> dict:
    """原子写入 lock 文件。"""
    lock_path = get_lock_path(data_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    lock_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def clear_lock(data_dir: Path) -> None:
    """删除 lock 文件（server 退出时调用）。"""
    lock_path = get_lock_path(data_dir)
    try:
        lock_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("删除 lock 文件失败: %s", exc)


def is_pid_alive(pid: int) -> bool:
    """检查 PID 对应进程是否存活。Windows 和 POSIX 兼容。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def server_status(url: str, timeout: float = 1.5) -> dict | None:
    """Read the local server identity before deciding whether it is reusable."""
    if not is_loopback_url(url):
        return None
    try:
        req = urllib.request.Request(f"{url}/api/status", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def probe_http(url: str, timeout: float = 1.5) -> bool:
    """Probe whether a Pocket Memory server is reachable."""
    return server_status(url, timeout) is not None


def detect_running_server(data_dir: Path) -> dict | None:
    """检测是否已有 server 实例在运行。返回 lock 信息或 None。

    判定条件：lock 文件存在 + PID 存活 + HTTP 可连。三者任一不满足返回 None。
    若 lock 存在但进程已死，清理 stale lock。
    """
    lock = read_lock(data_dir)
    if lock is None:
        return None
    pid = lock.get("pid", 0)
    url = lock.get("url", "")
    if not is_pid_alive(pid):
        logger.info("lock 指向的进程 %s 已退出，清理 stale lock", pid)
        clear_lock(data_dir)
        return None
    status = server_status(url)
    if status is None:
        logger.info("lock 指向的进程 %s 存活但 HTTP 不可达，清理 stale lock", pid)
        clear_lock(data_dir)
        return None
    if status.get("app_version") != APP_VERSION:
        logger.info("lock 指向的 server 版本不兼容，停止旧服务后重启: %s", status.get("app_version", "unknown"))
        request_server_shutdown(url)
        for _ in range(20):
            if not is_pid_alive(pid):
                break
            time.sleep(0.15)
        clear_lock(data_dir)
        return None
    return lock


def wait_for_server_ready(url: str, timeout: float = READY_TIMEOUT_SECONDS) -> bool:
    """轮询等待 server 就绪（模型加载可能需要数十秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe_http(url, timeout=2.0):
            return True
        time.sleep(READY_PROBE_INTERVAL)
    return False


def request_server_shutdown(url: str, timeout: float = 5.0) -> bool:
    """向 server 发送 shutdown 请求。仅允许 127.0.0.1。"""
    if not is_loopback_url(url):
        logger.warning("拒绝向非本机地址发送 shutdown 请求: %s", url)
        return False
    try:
        req = urllib.request.Request(f"{url}/api/shutdown", method="POST", data=b"{}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("shutdown 请求失败: %s", exc)
        return False


def is_loopback_url(url: str) -> bool:
    """Validate lock-file URLs before sending any local control request."""
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS and not parsed.username and not parsed.password


def find_free_port(host: str = "127.0.0.1", preferred: int = 0) -> int:
    """获取可用端口。preferred=0 表示由系统分配。"""
    if preferred > 0:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, preferred))
                return preferred
        except OSError:
            logger.info("首选端口 %s 被占用，改用系统分配", preferred)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]
