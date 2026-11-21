# -*- coding: utf-8 -*-
"""Read actual SCM service state without requiring pywin32 or admin privileges."""
from __future__ import annotations
import ctypes
import os
from ctypes import wintypes

SERVICE_QUERY_STATUS = 0x0004
SC_MANAGER_CONNECT = 0x0001
ERROR_SERVICE_DOES_NOT_EXIST = 1060


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    ]


def query_service_state(service_name="OneCBackupManager"):
    """Return Windows SCM state integer, None if missing, or raise OSError."""
    if os.name != "nt":
        return None
    scm = ctypes.WinDLL("advapi32", use_last_error=True)
    scm.OpenSCManagerW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    scm.OpenSCManagerW.restype = wintypes.HANDLE
    scm.OpenServiceW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD)
    scm.OpenServiceW.restype = wintypes.HANDLE
    scm.QueryServiceStatus.argtypes = (wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS))
    scm.QueryServiceStatus.restype = wintypes.BOOL
    scm.CloseServiceHandle.argtypes = (wintypes.HANDLE,)
    scm.CloseServiceHandle.restype = wintypes.BOOL
    hscm = scm.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not hscm:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        hsvc = scm.OpenServiceW(hscm, service_name, SERVICE_QUERY_STATUS)
        if not hsvc:
            code = ctypes.get_last_error()
            if code == ERROR_SERVICE_DOES_NOT_EXIST:
                return None
            raise ctypes.WinError(code)
        try:
            status = SERVICE_STATUS()
            if not scm.QueryServiceStatus(hsvc, ctypes.byref(status)):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(status.dwCurrentState)
        finally:
            scm.CloseServiceHandle(hsvc)
    finally:
        scm.CloseServiceHandle(hscm)


def service_labels(state):
    """Return (agent label, service label) for a Windows SCM state."""
    if state is None:
        return "Агент не установлен", "Служба: не установлена"
    names = {
        1: ("Агент остановлен", "Служба: остановлена"),
        2: ("Агент запускается", "Служба: запускается"),
        3: ("Агент останавливается", "Служба: останавливается"),
        4: ("Агент запущен", "Служба: работает"),
        5: ("Агент ожидает продолжения", "Служба: возобновляется"),
        6: ("Агент приостанавливается", "Служба: приостанавливается"),
        7: ("Агент приостановлен", "Служба: приостановлена"),
    }
    return names.get(int(state), (f"Агент: состояние {state}", f"Служба: код {state}"))
