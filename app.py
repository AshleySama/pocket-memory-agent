# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
import json
from urllib.request import urlopen
from pathlib import Path

from pocket_memory.config import AppConfig, app_directory
from pocket_memory.daemon import (
    clear_lock,
    detect_running_server,
    find_free_port,
    request_server_shutdown,
    read_lock,
    server_status,
    wait_for_server_ready,
)
from pocket_memory.server import PocketMemoryServer, is_loopback_host
from pocket_memory.storage import NoteStore
from pocket_memory.directory_picker import choose_directory
from pocket_memory.desktop_directory import DesktopDirectoryApi
from pocket_memory.desktop_runtime import ensure_native_runtime_config
from pocket_memory.desktop_readiness import DesktopReadiness, update_desktop_requested
from pocket_memory.desktop_tray import DesktopSession, NativeProcessWatch

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pocket Memory local MVP")
    parser.add_argument("--data-dir", type=Path, help="Override the configured data directory")
    parser.add_argument("--beta", action="store_true", help="Use data-beta only when this Beta profile has no selected library")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--browser", action="store_true", help="直接使用系统浏览器打开本地界面")
    parser.add_argument("--debug", action="store_true", help="开启 DevTools（F12）和页面刷新（Ctrl+R）")
    parser.add_argument("--server-only", action="store_true", help="仅启动 server 守护进程（不打开窗口）")
    parser.add_argument("--quit", action="store_true", help="退出正在运行的后台 server")
    parser.add_argument("--restart", action="store_true", help="停掉旧 server 再启动新 server（开发改代码后用）")
    return parser.parse_args()


def selected_data_dir(args, config: AppConfig) -> Path | None:
    """An existing profile selection wins over the portable Beta's default."""
    if args.data_dir:
        return args.data_dir.resolve()
    if config.data_dir is not None:
        return config.data_dir
    return app_directory() / 'data-beta' if args.beta else None


def run_server_only(config: AppConfig, data_dir: Path, host: str, port: int) -> int:
    """仅运行 server（作为守护子进程被启动）。模型常驻，直到收到 shutdown 请求。"""
    if not is_loopback_host(host):
        print("安全限制：Pocket Memory 仅允许监听 127.0.0.1、::1 或 localhost。")
        return 2
    if os.environ.get('PM_UPDATE_HEALTH_TOKEN'):
        from pocket_memory.update_probe import run_probe
        return run_probe(config, data_dir, resource_path('frontend'), host, port)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    store = NoteStore(data_dir)
    welcome_image = resource_path("frontend/PocketMemory_welcome.jpg")
    if not os.environ.get('PM_UPDATE_HEALTH_TOKEN'):
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


def update_is_restarting(url: str) -> bool:
    try:
        with urlopen(url + '/api/app-update', timeout=2) as response:
            value = json.load(response)
        return value.get('state') == 'restarting' and value.get('shutdown_requested') is True
    except Exception:
        return False


def close_launcher_for_update(url: str, destroy_window=None, *, exit_process=None,
                              close_timeout: float = 3.0) -> bool:
    """Exit only this launcher after the server confirms its worker handoff.

    pywebview/.NET can retain the launcher even after its native window closes.
    Bound both a stuck destroy call and lingering native threads. The daemon owns
    the library and finishes saving independently; the worker also waits for it.
    Normal window closure never uses this forced-exit path.
    """
    if not update_is_restarting(url):
        return False
    if destroy_window is not None:
        finished = threading.Event()

        def close_window():
            try:
                destroy_window()
            except Exception:
                logger.warning("更新时关闭本窗口失败，将退出当前启动器", exc_info=True)
            finally:
                finished.set()

        threading.Thread(target=close_window, daemon=True,
                         name="update-launcher-close").start()
        finished.wait(close_timeout)
    # os._exit affects this launcher only: never terminate the daemon or another
    # PocketMemory process, and do not rely on .NET's thread shutdown behavior.
    (exit_process if exit_process is not None else os._exit)(0)
    return True  # Injectable callbacks can return in focused tests.


def configure_launcher_log(data_dir: Path) -> None:
    """Persist desktop-only errors too; frozen windowed builds have no console."""
    try:
        folder = data_dir / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"launcher-{time.strftime('%Y%m%d')}.log"
        root = logging.getLogger()
        if not any(isinstance(h, logging.FileHandler) and
                   Path(h.baseFilename) == path.resolve() for h in root.handlers):
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
            root.addHandler(handler)
    except OSError:
        logger.warning("无法写入启动日志，继续启动", exc_info=True)


def show_browser_launch_help(url: str) -> None:
    """No .NET dependency: still give the user a usable URL if shell launch fails."""
    text = f"Pocket Memory 本地服务已启动。\n\n请打开浏览器，在地址栏输入：\n{url}\n\n资料仍保存在本机。"
    logger.error("无法自动打开浏览器；本地地址：%s", url)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "Pocket Memory", 0x40)
        except Exception:
            logger.warning("无法显示启动提示", exc_info=True)
    else:
        print(text)


def open_browser_session(url: str) -> None:
    """Reuse the same daemon and keep its update handoff alive; never spawn AI."""
    try:
        opened = webbrowser.open(url, new=2)
    except Exception:
        logger.warning("系统浏览器启动失败", exc_info=True)
        opened = False
    if not opened:
        show_browser_launch_help(url)
        return
    logger.info("已用系统浏览器打开本地界面：%s", url)
    unavailable = 0
    try:
        while True:
            if close_launcher_for_update(url):
                return
            try:
                with urlopen(url + '/api/status', timeout=2) as response:
                    alive = json.load(response).get('ok') is True
            except Exception:
                alive = False
            unavailable = 0 if alive else unavailable + 1
            if unavailable >= 3:
                logger.info("本地服务已退出，浏览器启动器结束。")
                return
            time.sleep(1)
    except KeyboardInterrupt:
        return


def show_native_startup_error(message: str) -> None:
    """Keep errors on desktop; never silently switch an EXE user to a browser."""
    text = ("Pocket Memory 无法打开桌面窗口。\n\n" + message +
            "\n\n请确认已完整解压程序。详细原因已记录到资料目录的 logs/launcher-日期.log。")
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, 'Pocket Memory 启动失败', 0x10)
    else:
        print(text)


class BoundDesktopServer:
    """Observe one server process, not the mutable location of its library.

    The read-only process handle survives directory migration and cannot follow
    PID reuse. Every shutdown POST additionally checks the URL's current PID.
    """
    def __init__(self, data_dir: Path, url: str):
        self.url = url
        self.pid = None
        self.watch = None
        identity = read_lock(data_dir)
        if (not identity or identity.get('url') != url
                or type(identity.get('pid')) is not int or identity['pid'] <= 0):
            logger.warning('未能绑定桌面后台身份；退出时不会停止未知服务。')
            return
        watch = None
        try:
            watch = NativeProcessWatch(identity['pid'])
            status = server_status(url)
            if (not status or status.get('pid') != identity['pid']
                    or watch.has_exited()):
                watch.close()
                logger.warning('桌面后台身份已变化；未绑定该服务。')
                return
            self.pid = identity['pid']
            self.watch = watch
        except Exception:
            if watch is not None:
                watch.close()
            logger.warning('无法观察桌面后台进程；不会停止未知服务。', exc_info=True)

    def has_exited(self):
        return self.watch is not None and self.watch.has_exited()

    def __call__(self):
        if self.watch is None:
            return False
        if self.watch.has_exited():
            return True
        status = server_status(self.url)
        if not status or status.get('pid') != self.pid:
            logger.warning('后台地址已不属于本会话；不发送退出请求。')
            return False
        if self.watch.has_exited():
            return True
        # Leave enough time for an in-flight request holding operation_lock.
        # This runs off the UI thread. Never force-kill model/storage workers.
        return request_server_shutdown(self.url, timeout=120)

    def close(self):
        if self.watch is not None:
            self.watch.close()


def desktop_shutdown_callback(data_dir: Path, url: str):
    return BoundDesktopServer(data_dir, url)


def close_desktop_if_server_exited(shutdown, desktop, window):
    exited = getattr(shutdown, 'has_exited', None)
    if not callable(exited) or not exited():
        return False
    logger.info('绑定的后台进程已退出，关闭本桌面会话。')
    if desktop is not None:
        desktop.close_for_server_exit()
    else:
        window.destroy()
    return True


def open_webview(url: str, debug: bool, *, shutdown=None) -> bool:
    """Start the native desktop host after preparing its CLR configuration."""
    stopped = threading.Event()
    window = None
    readiness = None
    desktop = None
    update_attempt = update_desktop_requested()
    try:
        readiness = DesktopReadiness.from_environment(url)
        config_path = ensure_native_runtime_config()
        if config_path is not None:
            logger.info("原生桌面运行配置已就绪：%s", config_path)
        import webview
        if not callable(getattr(webview, "create_window", None)) or not callable(getattr(webview, "start", None)):
            raise ImportError("pywebview API unavailable")
        # WebView2 otherwise cancels <a download>/attachment downloads before
        # its native SaveFileDialog can appear. Managed report exports already
        # exist on disk; this enables the user's additional "下载副本" action.
        webview_settings = getattr(webview, 'settings', None)
        if webview_settings is not None and 'ALLOW_DOWNLOADS' in webview_settings:
            webview_settings['ALLOW_DOWNLOADS'] = True
        if not is_webview2_installed():
            raise RuntimeError("此电脑缺少 Microsoft Edge WebView2 Runtime，请安装该运行组件后重试。")
        directory_api = DesktopDirectoryApi()
        window = webview.create_window(
            "Pocket Memory", url, width=1180, height=820,
            min_size=(820, 600), maximized=True, js_api=directory_api,
        )
        directory_api._window = window
        start_kwargs = {"gui": "edgechromium"}
        icon_path = find_app_icon()
        if icon_path:
            start_kwargs["icon"] = icon_path
        if debug:
            start_kwargs["debug"] = True
        if readiness is not None:
            readiness.attach(window)
            start_kwargs['func'] = readiness.run
        if (sys.platform == 'win32' and getattr(window, 'events', None) is not None
                and hasattr(window.events, 'closing')):
            desktop = DesktopSession(
                window, icon_path, shutdown,
                must_close=lambda: readiness is not None and readiness.failed,
            )
            desktop.attach()
        def update_watch():
            while not stopped.wait(1):
                destroy = desktop.close_for_update if desktop is not None else window.destroy
                if close_launcher_for_update(url, destroy):
                    return
                # Another GUI or a delayed accepted shutdown may have ended
                # our shared server. Do not leave a dead window/tray behind.
                if close_desktop_if_server_exited(shutdown, desktop, window):
                    return
        threading.Thread(target=update_watch, daemon=True, name='update-window-watch').start()
        logger.info("正在启动原生窗口。")
        webview.start(**start_kwargs)
        if readiness is not None:
            readiness.close()
        logger.info("原生窗口已正常关闭。")
        return readiness is None or not readiness.failed
    except Exception as exc:
        stopped.set()
        if desktop is not None:
            desktop.allow_close = True
        if update_attempt:
            logger.error("更新后的原生桌面窗口启动失败 (%s)。", type(exc).__name__)
            if readiness is not None:
                try:
                    readiness.fail('native_startup_failed')
                except Exception:
                    logger.error("无法写入桌面启动失败记录。")
        else:
            logger.exception("原生桌面窗口启动失败；不会自动打开浏览器。")
        if window is not None and getattr(window, 'native', None) is not None:
            def close_partial_window():
                try:
                    window.destroy()
                except Exception:
                    logger.warning("关闭未完成初始化的窗口失败", exc_info=True)
            closer = threading.Thread(target=close_partial_window, daemon=True)
            closer.start()
            closer.join(timeout=1)
        if not update_attempt:
            show_native_startup_error(str(exc))
        return False
    finally:
        stopped.set()
        if desktop is not None:
            desktop.allow_close = True
            desktop.stop()
        if readiness is not None:
            readiness.stopped.set()
        cleanup = getattr(shutdown, 'close', None)
        if callable(cleanup):
            cleanup()


def main() -> int:
    # Accepted scope candidate is on for normal desktop and daemon starts.
    # Explicit 0 remains an operational rollback, including inherited children.
    os.environ.setdefault('POCKET_MEMORY_SCOPE_FILTER', '1')
    args = parse_args()

    # Preserve the exact selected config when an updater restarts the application.
    configured_path = os.environ.get('PM_UPDATE_CONFIG_PATH')
    config_path = Path(configured_path).resolve() if configured_path else None

    if not is_loopback_host(args.host):
        print("安全限制：Pocket Memory 仅允许监听 127.0.0.1、::1 或 localhost。")
        return 2

    # --quit：退出后台 server
    if args.quit:
        config = AppConfig.load(config_path)
        data_dir = selected_data_dir(args, config)
        if data_dir is None:
            print("未配置数据目录，无法定位 server")
            return 1
        return quit_running_server(data_dir)

    config = AppConfig.load(config_path)
    data_dir = selected_data_dir(args, config)
    if data_dir is None:
        data_dir = default_data_dir()
        config.set_data_dir(data_dir)

    # --server-only：作为守护子进程运行
    if args.server_only:
        return run_server_only(config, data_dir, args.host, args.port)

    from pocket_memory.legacy_update_handoff import yield_to_supervisor
    if yield_to_supervisor(config, data_dir, args):
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    configure_launcher_log(data_dir)

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
        if args.browser:
            open_browser_session(url)
        else:
            if not open_webview(url, args.debug, shutdown=desktop_shutdown_callback(data_dir, url)):
                return 1
        logger.info("桌面会话已结束。")
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
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                logger.error("本次启动的后台仍未退出，保留日志供诊断")
        return 1

    logger.info("server 已就绪: %s", url)

    if args.no_browser:
        print(url)
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        return 0

    # 原生窗口关闭后收起到通知区域；托盘“退出”关闭本次后台会话。
    if args.browser:
        open_browser_session(url)
    else:
        if not open_webview(url, args.debug, shutdown=desktop_shutdown_callback(data_dir, url)):
            # Only stop the daemon this launch created; never stop an existing
            # session that may contain another user's active work.
            quit_running_server(data_dir)
            return 1
    logger.info("桌面会话已结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
