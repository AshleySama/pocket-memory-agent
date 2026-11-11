# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Local directory picker; Windows uses its built-in Common Item Dialog.

The packaged application deliberately excludes Tk. Keep this operation separate
from selecting/migrating a library: choosing a folder never changes stored data.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import uuid
from pathlib import Path


class DirectoryPickerError(RuntimeError):
    pass


_picker_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _windows_directory(initial_dir: Path | None) -> Path | None:
    # Run on a fresh STA thread (the HTTP request/UI caller may already use COM).
    # Method indices follow IUnknown -> IModalWindow -> IFileDialog in shobjidl.h.
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]

        @classmethod
        def parse(cls, value: str):
            return cls((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))

    HRESULT = ctypes.c_long
    pointer = ctypes.c_void_p
    ole = ctypes.WinDLL("ole32")
    shell = ctypes.WinDLL("shell32")
    ole.CoInitializeEx.argtypes = [pointer, wintypes.DWORD]
    ole.CoInitializeEx.restype = HRESULT
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), pointer, wintypes.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    ole.CoCreateInstance.restype = HRESULT
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [pointer]
    ole.CoTaskMemFree.restype = None
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, pointer,
                                                ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    shell.SHCreateItemFromParsingName.restype = HRESULT

    def check(value: int):
        if value < 0:
            raise DirectoryPickerError(f"Windows 目录选择器错误（0x{value & 0xffffffff:08X}）")

    def method(obj, index, result, *args):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(result, pointer, *args)(table[index])

    def release(obj):
        if obj:
            method(obj, 2, wintypes.ULONG)(obj)

    dialog = pointer()
    folder = pointer()
    selected = pointer()
    display_path = pointer()
    logger.info("directory-picker: STA initializing (thread=%s)", threading.get_native_id())
    check(ole.CoInitializeEx(None, 2))  # COINIT_APARTMENTTHREADED
    try:
        clsid = GUID.parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
        iid_dialog = GUID.parse("D57C7288-D4AD-4768-BE02-9D969532D960")
        iid_item = GUID.parse("43826D1E-E718-42EE-BC55-A1E261C37BFE")
        logger.info("directory-picker: creating Common Item Dialog")
        check(ole.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                   ctypes.byref(iid_dialog), ctypes.byref(dialog)))
        logger.info("directory-picker: configuring Common Item Dialog")
        flags = wintypes.DWORD()
        check(method(dialog, 10, HRESULT, ctypes.POINTER(wintypes.DWORD))(dialog, ctypes.byref(flags)))
        # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_NOCHANGEDIR.
        check(method(dialog, 9, HRESULT, wintypes.DWORD)(dialog, flags.value | 0x20 | 0x40 | 0x8))
        check(method(dialog, 17, HRESULT, wintypes.LPCWSTR)(dialog, "选择 Pocket Memory 数据目录"))
        check(method(dialog, 18, HRESULT, wintypes.LPCWSTR)(dialog, "选择此文件夹"))
        if initial_dir and initial_dir.is_dir():
            logger.info("directory-picker: resolving initial folder")
            check(shell.SHCreateItemFromParsingName(str(initial_dir), None,
                                                    ctypes.byref(iid_item), ctypes.byref(folder)))
            check(method(dialog, 12, HRESULT, pointer)(dialog, folder))
        logger.info("directory-picker: showing Common Item Dialog")
        result = method(dialog, 3, HRESULT, wintypes.HWND)(dialog, None)
        logger.info("directory-picker: dialog closed (HRESULT=0x%08X)", result & 0xffffffff)
        if (result & 0xffffffff) == 0x800704C7:  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
            return None
        check(result)
        check(method(dialog, 20, HRESULT, ctypes.POINTER(pointer))(dialog, ctypes.byref(selected)))
        # SIGDN_FILESYSPATH: reject virtual Shell locations such as "This PC".
        check(method(selected, 5, HRESULT, wintypes.DWORD, ctypes.POINTER(pointer))(
            selected, 0x80058000, ctypes.byref(display_path)))
        path = Path(ctypes.wstring_at(display_path)).resolve()
        if not path.is_dir():
            raise DirectoryPickerError("请选择本机或可访问位置中的文件夹")
        return path
    finally:
        if display_path:
            ole.CoTaskMemFree(display_path)
        release(selected)
        release(folder)
        release(dialog)
        ole.CoUninitialize()


def _other_directory(initial_dir: Path | None) -> Path | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择 Pocket Memory 数据目录", parent=root,
                                           initialdir=str(initial_dir) if initial_dir else None,
                                           mustexist=True)
        return Path(selected).resolve() if selected else None
    finally:
        root.destroy()


def choose_directory(initial_dir: Path | None = None) -> Path | None:
    """None means user cancelled; an unavailable dialog raises a visible error."""
    if not _picker_lock.acquire(blocking=False):
        raise DirectoryPickerError("目录选择窗口已打开，请先完成或取消选择")
    result: list[Path | None] = []
    errors: list[Exception] = []

    def show():
        try:
            logger.info("directory-picker: worker started")
            picker = _windows_directory if sys.platform == "win32" else _other_directory
            result.append(picker(initial_dir))
        except Exception as exc:
            logger.exception("directory-picker: worker failed")
            errors.append(exc)

    try:
        logger.info("directory-picker: request accepted")
        worker = threading.Thread(target=show, name="directory-picker", daemon=True)
        worker.start()
        worker.join()
        if errors:
            raise DirectoryPickerError(str(errors[0])) from errors[0]
        return result[0]
    finally:
        _picker_lock.release()
