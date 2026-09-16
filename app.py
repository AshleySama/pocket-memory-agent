from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from pocket_memory.config import AppConfig
from pocket_memory.daemon import (
    clear_lock,
    detect_running_server,
    find_free_port,
    request_server_shutdown,
    wait_for_server_ready,
)
from pocket_memory.server import PocketMemoryServer, is_loopback_host
from pocket_memory.storage import NoteStore

logger = logging.getLogger(__name__)


def resource_path(relative_path: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parent / relative_path


# WebView2 Runtime 的注册表位置（EdgeUpdate Client ID）
_WEBVIEW2_REGKEYS = [
    (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "HKLM"),
    (r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "HKCU"),
]
def is_webview2_installed() -> bool:
    """通过注册表检测 WebView2 Runtime 是否已安装。"""
    if sys.platform != "win32":
        return True
    try:
        import winreg
    except ImportError:
        return True
    hive_map = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    for subpath, hive_name in _WEBVIEW2_REGKEYS:
        try:
            key = winreg.OpenKey(hive_map[hive_name], subpath)
            version, _ = winreg.QueryValueEx(key, "pv")
            winreg.CloseKey(key)
            if version and version != "0.0.0.0":
                logger.info("WebView2 Runtime 已安装: v%s (%s)", version, hive_name)
                return True
        except (FileNotFoundError, OSError):
            pass
    return False


def find_app_icon() -> str | None:
    for ext in (".ico", ".png", ".jpg", ".jpeg"):
        path = resource_path(f"frontend/app-icon{ext}")
        if path.is_file():
            return str(path)
    return None


def default_data_dir() -> Path:
    """Choose a writable local default so first launch never depends on Tk."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "PocketMemory" / "data"
    return Path.home() / ".local" / "share" / "pocket-memory"


def choose_directory(initial_dir: Path | None = None) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="选择 Pocket Memory 数据目录",
            initialdir=str(initial_dir) if initial_dir else None,
            mustexist=False,
        )
        root.destroy()
        return Path(selected).resolve() if selected else None
    except Exception as exc:
        logger.warning("系统目录选择器不可用: %s", exc)
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pocket Memory local MVP")
    parser.add_argument("--data-dir", type=Path, help="Override the configured data directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--debug", action="store_true", help="开启 DevTools（F12）和页面刷新（Ctrl+R）")
    parser.add_argument("--server-only", action="store_true", help="仅启动 server 守护进程（不打开窗口）")
    parser.add_argument("--quit", action="store_true", help="退出正在运行的后台 server")
    parser.add_argument("--restart", action="store_true", help="停掉旧 server 再启动新 server（开发改代码后用）")
    return parser.parse_args()


def run_server_only(config: AppConfig, data_dir: Path, host: str, port: int) -> int:
    """仅运行 server（作为守护子进程被启动）。模型常驻，直到收到 shutdown 请求。"""
    if not is_loopback_host(host):
        print("安全限制：Pocket Memory 仅允许监听 127.0.0.1、::1 或 localhost。")
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    store = NoteStore(data_dir)
    welcome_image = resource_path("frontend/PocketMemory_welcome.jpg")
    try:
        store.ensure_welcome_note(welcome_image)
    except Exception:
        logger.exception("创建欢迎笔记失败")
    server = PocketMemoryServer((host, port), store, config, choose_directory)
    logger.info("Pocket Memory server (daemon) running at http://%s:%s", host, server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        store.close()
    # AI/OCR executor may still contain a non-daemon native inference call.
    # User notes are committed before those tasks are queued; pending tasks are
    # recovered on the next start, so the daemon must not remain as a zombie.
    os._exit(0)


def quit_running_server(data_dir: Path) -> int:
    """向已运行的 server 发送 shutdown 请求。"""
    lock = detect_running_server(data_dir)
    if lock is None:
        print("没有检测到正在运行的后台 server")
        clear_lock(data_dir)
        return 0
    url = lock["url"]
    print(f"正在退出后台 server: {url}")
    if request_server_shutdown(url):
        # 等待 server 真正退出
        for _ in range(20):
            time.sleep(0.3)
            if detect_running_server(data_dir) is None:
                print("后台 server 已退出")
                return 0
        print("server 仍在退出中，可能需要稍等")
        return 0
    print("shutdown 请求失败，强制清理 lock 文件")
    clear_lock(data_dir)
    return 1


def spawn_server_process(data_dir: Path, host: str, port: int, debug: bool) -> subprocess.Popen:
    """启动 server 子进程。子进程独立运行，主进程退出后继续。"""
    # 打包后 sys.executable 是 PocketMemory.exe 自身，直接用 --server-only 重新执行即可；
    # 开发模式下 sys.executable 是 python.exe，需附带 app.py 脚本路径
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--server-only",
               "--data-dir", str(data_dir), "--host", host, "--port", str(port)]
    else:
        # A source checkout may be started from a system Python that does not
        # carry the optional local-AI wheels. Prefer the project venv when it
        # exists so the daemon and its launcher use the same dependencies.
        venv_python = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
        python_executable = str(venv_python) if venv_python.is_file() else sys.executable
        cmd = [python_executable, str(Path(__file__).resolve()), "--server-only",
               "--data-dir", str(data_dir), "--host", host, "--port", str(port)]
    if debug:
        cmd.append("--debug")
    # Windows 下用 CREATE_NEW_PROCESS_GROUP 让子进程独立于父进程
    # CREATE_NO_WINDOW(0x08000000) 避免弹出黑色控制台窗口
    creationflags = 0
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    # 重定向输出到日志文件，避免子进程 stdout 阻塞
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"server-{time.strftime('%Y%m%d')}.log"
    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(f"\n{'=' * 60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] server 子进程启动\n{'=' * 60}\n")
    log_file.flush()
    # 打包后 __file__ 在 _internal 子目录，需用 exe 所在目录作为 cwd
    if getattr(sys, "frozen", False):
        work_dir = str(Path(sys.executable).resolve().parent)
    else:
        work_dir = str(Path(__file__).resolve().parent)
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=work_dir,
        creationflags=creationflags,
        close_fds=True,
    )
    logger.info("server 子进程已启动: PID=%s, 端口=%s, 日志=%s", proc.pid, port, log_path)
    return proc


def open_webview(url: str, debug: bool) -> None:
    """打开 webview 窗口。关闭窗口后函数返回，但不影响 server。"""
    webview = None
    try:
        import webview as _webview
        if not hasattr(_webview, "create_window") or not hasattr(_webview, "start"):
            _webview = None
    except ImportError:
        _webview = None
    webview = _webview

    if webview is None:
        webbrowser.open(url)
        print(f"Pocket Memory running at {url}. 后台 server 已启动，关闭浏览器不影响 server。")
        print(f"如需完全退出，请运行: python app.py --quit")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        if not is_webview2_installed():
            logger.warning("WebView2 Runtime 不可用，回退到系统浏览器")
            print("WebView2 Runtime 不可用，使用系统浏览器打开。")
            webbrowser.open(url)
            print(f"Pocket Memory running at {url}. 后台 server 已启动，关闭浏览器不影响 server。")
            print(f"如需完全退出，请运行: python app.py --quit")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            return

        webview.create_window("Pocket Memory", url, width=1180, height=820, min_size=(820, 600))
        start_kwargs = {}
        icon_path = find_app_icon()
        if icon_path:
            start_kwargs["icon"] = icon_path
        if debug:
            start_kwargs["debug"] = True
        webview.start(**start_kwargs)


def main() -> int:
    args = parse_args()

    if not is_loopback_host(args.host):
        print("安全限制：Pocket Memory 仅允许监听 127.0.0.1、::1 或 localhost。")
        return 2

    # --quit：退出后台 server
    if args.quit:
        config = AppConfig.load()
        data_dir = args.data_dir.resolve() if args.data_dir else config.data_dir
        if data_dir is None:
            print("未配置数据目录，无法定位 server")
            return 1
        return quit_running_server(data_dir)

    config = AppConfig.load()
    data_dir = args.data_dir.resolve() if args.data_dir else config.data_dir
    if data_dir is None:
        data_dir = default_data_dir()
        config.set_data_dir(data_dir)

    # --server-only：作为守护子进程运行
    if args.server_only:
        return run_server_only(config, data_dir, args.host, args.port)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    # --restart：先停掉旧 server，确保加载新代码
    if args.restart:
        quit_running_server(data_dir)

    # 检测是否已有 server 在运行
    existing = detect_running_server(data_dir)
    if existing is not None:
        url = existing["url"]
        logger.info("检测到已运行的后台 server: %s，直接打开窗口（秒开）", url)
        if args.no_browser:
            print(url)
            return 0
        open_webview(url, args.debug)
        logger.info("窗口已关闭。后台 server 仍在运行，下次启动将秒开。")
        return 0

    # 无 server 运行：启动 server 子进程
    port = find_free_port(args.host, args.port)
    logger.info("启动后台 server 子进程: %s:%s", args.host, port)
    proc = spawn_server_process(data_dir, args.host, port, args.debug)
    url = f"http://{args.host}:{port}"

    # 等待 server 就绪（模型加载可能需要数十秒）
    print(f"正在启动 Pocket Memory 后台服务（首次加载模型可能需要 30-60 秒）...")
    log_path = data_dir / "logs" / f"server-{time.strftime('%Y%m%d')}.log"
    print(f"日志: {log_path}")
    if not wait_for_server_ready(url, timeout=120):
        logger.error("server 启动超时")
        print("启动失败：server 长时间未就绪，请检查日志")
        return 1

    logger.info("server 已就绪: %s", url)

    if args.no_browser:
        print(url)
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        return 0

    # 打开 webview 窗口（关闭后不影响 server）
    open_webview(url, args.debug)
    logger.info("窗口已关闭。后台 server 仍在运行，下次启动将秒开。")
    print(f"窗口已关闭，后台 server 仍在运行。")
    print(f"如需完全退出: python app.py --quit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
