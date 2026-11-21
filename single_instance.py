# -*- coding: utf-8 -*-
"""Prevent duplicate interactive GUI windows in the same Windows session.

Windows Session 0 hosts the actual Windows service and is deliberately excluded
by the caller. A local named mutex is owned as long as the first GUI runs;
subsequent launches set an auto-reset named event instead of creating a second
Tk process. The first GUI polls that event on its Tk thread and restores itself.

Requires only ctypes, including in PyInstaller --onefile builds.
"""
from __future__ import annotations
import ctypes
import os
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class GuiSingleInstance:
    def __init__(self, name="OneCBackupManager.GUI"):
        self.is_primary = True
        self._mutex = None
        self._event = None
        self._kernel = None
        if os.name != "nt":
            return

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        # Local\\ namespace: one GUI per interactive logon session. The Windows
        # service in Session 0 does not share this GUI mutex.
        self._mutex = kernel.CreateMutexW(None, False, f"Local\\{name}.Mutex")
        if not self._mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        self.is_primary = ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        self._event = kernel.CreateEventW(None, False, False, f"Local\\{name}.Activate")
        if not self._event:
            kernel.CloseHandle(self._mutex)
            self._mutex = None
            raise ctypes.WinError(ctypes.get_last_error())

    def activate_existing(self) -> bool:
        if self.is_primary or self._kernel is None or self._event is None:
            return False
        return bool(self._kernel.SetEvent(self._event))

    def activation_requested(self) -> bool:
        if not self.is_primary or self._kernel is None or self._event is None:
            return False
        return self._kernel.WaitForSingleObject(self._event, 0) == WAIT_OBJECT_0

    def close(self):
        if self._kernel is not None:
            for name in ("_event", "_mutex"):
                handle = getattr(self, name)
                if handle:
                    self._kernel.CloseHandle(handle)
                    setattr(self, name, None)
