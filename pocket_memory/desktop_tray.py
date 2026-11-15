# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Native Windows notification icon and the desktop's close/quit policy.

Uses the WinForms runtime already loaded by pywebview. No extra process,
model, Python package or UI message loop is started for the tray.
"""
from __future__ import annotations

import logging
from pathlib import Path
import threading

logger = logging.getLogger(__name__)


class NativeProcessWatch:
    """Keep a read-only handle to one process, even if Windows reuses its PID."""
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # SYNCHRONIZE only. This handle cannot terminate the server or write it.
        self.handle = kernel.OpenProcess(0x00100000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.kernel = kernel
        self._lock = threading.Lock()

    def has_exited(self):
        with self._lock:
            if self.handle is None:
                return True
            state = self.kernel.WaitForSingleObject(self.handle, 0)
            if state == 0:  # WAIT_OBJECT_0
                return True
            if state == 258:  # WAIT_TIMEOUT
                return False
            raise OSError('Cannot observe the bound server process')

    def close(self):
        with self._lock:
            if self.handle is not None:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


class WinFormsTray:
    def __init__(self, window, icon_path, restore, quit_application):
        self.window = window
        self.icon_path = icon_path
        self.restore = restore
        self.quit_application = quit_application
        self.icon = None
        self.menu = None
        self.image = None

    def _invoke(self, callback):
        # pywebview's shown event runs on a worker. NotifyIcon belongs to the
        # native window's UI thread so Explorer callbacks keep working hidden.
        from System import Action
        native = self.window.native
        if native is None or native.IsDisposed:
            return False
        if native.InvokeRequired:
            native.Invoke(Action(callback))
        else:
            callback()
        return True

    def start(self):
        def create():
            from System.Drawing import Icon, SystemIcons
            from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripSeparator
            if self.icon_path and Path(self.icon_path).suffix.lower() == '.ico':
                self.image = Icon(str(self.icon_path))
            else:
                self.image = SystemIcons.Application.Clone()
            self.menu = ContextMenuStrip()
            self.menu.Items.Add('打开 Pocket Memory').Click += self._restore
            self.menu.Items.Add(ToolStripSeparator())
            self.menu.Items.Add('退出 Pocket Memory').Click += self._quit
            self.icon = NotifyIcon()
            self.icon.Icon = self.image
            self.icon.Text = 'Pocket Memory — 双击打开，右键退出'
            self.icon.ContextMenuStrip = self.menu
            self.icon.DoubleClick += self._restore
            self.icon.Visible = True
        try:
            if not self._invoke(create):
                return False
            logger.info('Windows 通知区域图标已创建。')
            return self.icon is not None and bool(self.icon.Visible)
        except Exception:
            logger.exception('无法创建通知区域图标；关闭窗口将完全退出本次会话。')
            self.stop()
            return False

    def _restore(self, *_):
        self.restore()

    def _quit(self, *_):
        self.quit_application()

    def show_error(self, message):
        def show():
            from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon
            MessageBox.Show(message, 'Pocket Memory', MessageBoxButtons.OK, MessageBoxIcon.Warning)
        self._invoke(show)

    def set_quitting(self, quitting):
        def update():
            if self.icon is not None:
                self.icon.Text = ('Pocket Memory — 正在退出，请稍候' if quitting
                                  else 'Pocket Memory — 双击打开，右键退出')
        self._invoke(update)

    def stop(self):
        def dispose():
            if self.icon is not None:
                self.icon.Visible = False
                self.icon.Dispose()
                self.icon = None
            if self.menu is not None:
                self.menu.Dispose()
                self.menu = None
            if self.image is not None:
                self.image.Dispose()
                self.image = None
        try:
            if not self._invoke(dispose):
                # Form shutdown may precede webview.start returning. Disposal
                # still removes the shell icon after that UI thread has ended.
                dispose()
        except Exception:
            logger.warning('清理通知区域图标失败。', exc_info=True)


class DesktopSession:
    """Hide only when a working tray can restore the window.

    A normal close never terminates a process by name. Explicit Quit calls the
    launcher's identity-bound graceful shutdown callback; update handoff and
    readiness failures bypass this policy completely.
    """
    def __init__(self, window, icon_path=None, shutdown=None, *,
                 must_close=None, tray_factory=WinFormsTray, show_error=None):
        self.window = window
        self.shutdown = shutdown
        self.must_close = must_close or (lambda: False)
        self.tray = tray_factory(window, icon_path, self.restore, self.request_quit)
        self.show_error = show_error or self.tray.show_error
        self.active = False
        self.allow_close = False
        self.updating = False
        self.quitting = False
        self.starting = False
        self.minimized = False
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._closing = False

    def attach(self):
        self.window.events.shown += self.start
        self.window.events.closing += self.on_closing
        if hasattr(self.window.events, 'minimized'):
            self.window.events.minimized += self._on_minimized
            self.window.events.restored += self._on_restored
            self.window.events.maximized += self._on_restored

    def _on_minimized(self):
        self.minimized = True

    def _on_restored(self):
        self.minimized = False

    def start(self):
        with self._lock:
            if self.allow_close or self.active or self.starting:
                return
            self.starting = True
        try:
            # Never hold the policy lock while Invoke waits for the UI thread.
            active = bool(self.tray.start())
            with self._lock:
                self.active = active and not self.allow_close
            if not self.active:
                self.tray.stop()
        finally:
            with self._lock:
                self.starting = False

    def on_closing(self):
        if self.allow_close or self.must_close():
            return True
        if self.active:
            try:
                self.window.hide()
                logger.info('窗口已收起到通知区域；后台会话保持运行。')
                return False
            except Exception:
                logger.exception('无法收起窗口，将执行正常退出。')
        if self.shutdown is not None:
            # Do not leave an invisible server if Explorer/tray failed.
            self.request_quit()
            return False
        return True

    def restore(self):
        if self.allow_close:
            return
        try:
            self.window.show()
            if self.minimized:
                self.window.restore()
                self.minimized = False
            logger.info('已从通知区域恢复桌面窗口。')
        except Exception:
            logger.exception('恢复桌面窗口失败。')

    def request_quit(self):
        with self._lock:
            if self.quitting or self.updating:
                return
            self.quitting = True
        threading.Thread(target=self._quit, name='desktop-session-quit', daemon=True).start()

    def _quit(self):
        try:
            self.tray.set_quitting(True)
            if self.shutdown is not None and not self.shutdown():
                raise RuntimeError('后台服务未确认退出')
            exited = getattr(self.shutdown, 'has_exited', None)
            if callable(exited):
                # Accepted shutdown is not completed shutdown. Keep the tray
                # responsive while AI/background workers finish safely. The
                # session watcher also closes every GUI sharing this server.
                while not exited():
                    if self._stopped.wait(.25):
                        return
            self.close_for_server_exit()
            logger.info('已退出桌面窗口及其后台会话。')
        except Exception:
            # A shared server may disappear while this GUI's POST is still
            # returning. Do not undo the watcher's/update worker's close or
            # recreate a tray for an already-ended session.
            if self.updating or self._stopped.is_set():
                return
            exited = getattr(self.shutdown, 'has_exited', None)
            if callable(exited) and exited():
                self.close_for_server_exit()
                return
            logger.exception('退出未完成，保留窗口和托盘供重试。')
            self.allow_close = False
            self._closing = False
            self._stopped.clear()
            if not self.active:
                self.start()
            self.tray.set_quitting(False)
            self.restore()
            self.show_error('Pocket Memory 尚未完全退出，请稍后重试。详细原因已记录在启动日志中。')
        finally:
            with self._lock:
                self.quitting = False

    def close_for_update(self):
        # Set the bypass before destroy emits the cancellable closing event.
        # The existing update worker owns daemon saving/shutdown/rollback.
        self.updating = True
        self.close_for_server_exit()

    def close_for_server_exit(self):
        with self._lock:
            if self._closing:
                return
            self._closing = True
        self.allow_close = True
        self.stop()
        self.window.destroy()

    def stop(self):
        self._stopped.set()
        self.active = False
        self.tray.stop()
