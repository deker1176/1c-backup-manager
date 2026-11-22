"""Read-only diagnostics for Windows file-mode 1C infobase locks.

The Windows Restart Manager API identifies *local* processes with handles to
1Cv8.1CD. IIS publications are resolved by their default.vrd connection strings,
not by guessing the application pool. No web or SMB handle is ever closed here.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET


_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_APP = re.compile(r'^APP "([^"]+)" \(applicationPool:([^)]+)\)\s*$', re.M)
_VDIR = re.compile(r'^VDIR "([^"]+)" \(physicalPath:(.*?)\)\s*$', re.M)
_FILE = re.compile(r'(?i)(?:^|[;\s])File\s*=\s*(?:"([^"]+)"|([^;]+))')


def norm_path(value):
    value = str(value or "").strip().strip('"').replace("/", "\\")
    return ntpath.normcase(ntpath.normpath(value)).rstrip("\\") if value else ""


def publication_uses_file_base(vrd_xml, base):
    """Match parsed 1C File= connection strings; never print credentials."""
    try:
        root = ET.fromstring(vrd_xml)
    except (ET.ParseError, TypeError, ValueError):
        return False
    expected = norm_path(base)
    for point in root.iter():
        connection = str(point.attrib.get("ib", ""))
        for match in _FILE.finditer(connection):
            if norm_path(match.group(1) or match.group(2)) == expected:
                return True
    return False


def find_iis_publications(base, applications_output, vdirs_output, read_descriptor=None):
    """Pure matching layer, also used in tests with mock AppCmd output."""
    if read_descriptor is None:
        read_descriptor = lambda p: Path(p).read_text(encoding="utf-8-sig")
    applications = dict(_APP.findall(applications_output))
    vdirs = {name.rstrip("/"): path for name, path in _VDIR.findall(vdirs_output)}
    results = []
    for app, pool in applications.items():
        virtual_root = vdirs.get(app)
        if not virtual_root:
            continue
        root_path = Path(os.path.expandvars(virtual_root))
        try:
            vrd = read_descriptor(str(root_path / "default.vrd"))
        except (OSError, UnicodeError):
            continue
        if not publication_uses_file_base(vrd, base):
            continue
        results.append({
            "app": app,
            "pool": pool,
            "shared": sum(other_pool == pool for other_pool in applications.values()) > 1,
            "pool_apps": [other for other, other_pool in applications.items() if other_pool == pool],
            "descriptor": str(root_path / "default.vrd"),
        })
    return results


def iis_publications_for_base(base):
    if os.name != "nt":
        return []
    appcmd = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "inetsrv" / "appcmd.exe"
    if not appcmd.is_file():
        return []
    def run(*args):
        p = subprocess.run([str(appcmd), "list", *args], capture_output=True, text=True,
                           errors="replace", timeout=15, creationflags=_CREATE_NO_WINDOW)
        if p.returncode:
            raise RuntimeError(f"AppCmd list {' '.join(args)}: {p.stderr[-300:]}")
        return p.stdout
    return find_iis_publications(base, run("app"), run("vdir"))


def rm_locking_pids(database_file):
    """Return local process IDs holding a specific file using Restart Manager.

    Only a hint for selection, not a licence to kill a process: the caller must
    independently verify its executable, PID, command and creation time.
    """
    if os.name != "nt" or not Path(database_file).is_file():
        return set()

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS),
            ("strAppName", wintypes.WCHAR * 256),
            ("strServiceShortName", wintypes.WCHAR * 64),
            ("ApplicationType", wintypes.DWORD),
            ("AppStatus", wintypes.ULONG),
            ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    dll = ctypes.WinDLL("Rstrtmgr.dll")
    dll.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                    wintypes.LPWSTR]
    dll.RmStartSession.restype = wintypes.DWORD
    dll.RmRegisterResources.argtypes = [
        wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
    dll.RmRegisterResources.restype = wintypes.DWORD
    dll.RmGetList.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(wintypes.DWORD)]
    dll.RmGetList.restype = wintypes.DWORD
    dll.RmEndSession.argtypes = [wintypes.DWORD]
    dll.RmEndSession.restype = wintypes.DWORD

    handle = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    code = dll.RmStartSession(ctypes.byref(handle), 0, key)
    if code:
        raise OSError(code, "Restart Manager: RmStartSession")
    try:
        files = (wintypes.LPCWSTR * 1)(str(Path(database_file).resolve()))
        code = dll.RmRegisterResources(handle, 1, files, 0, None, 0, None)
        if code:
            raise OSError(code, "Restart Manager: RmRegisterResources")
        count = wintypes.UINT()
        needed = wintypes.UINT()
        reason = wintypes.DWORD()
        code = dll.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count),
                             None, ctypes.byref(reason))
        if code == 0:
            return set()
        if code != 234:  # ERROR_MORE_DATA
            raise OSError(code, "Restart Manager: RmGetList (size)")
        if needed.value > 16384:
            raise RuntimeError("Restart Manager returned too many processes")
        array = (RM_PROCESS_INFO * needed.value)()
        count.value = needed.value
        code = dll.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count),
                             array, ctypes.byref(reason))
        if code:
            raise OSError(code, "Restart Manager: RmGetList (data)")
        return {int(array[i].Process.dwProcessId) for i in range(count.value)}
    finally:
        dll.RmEndSession(handle)


def smb_open_files_for_base(base):
    """Read SMB open handles on this server, never close or mutate them."""
    if os.name != "nt":
        return []
    b64 = base64.b64encode(str(base).encode("utf-8")).decode("ascii")
    ps = (
        "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + b64 + "')); "
        "$prefix=$p.TrimEnd('\\')+'\\'; "
        "Get-SmbOpenFile -ErrorAction SilentlyContinue | Where-Object { "
        "$_.Path -and $_.Path.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) "
        "} | Select-Object ClientComputerName,ClientUserName,Path,FileId | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    cmd = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                             "-EncodedCommand", cmd], capture_output=True, text=True,
                            errors="replace", timeout=25, creationflags=_CREATE_NO_WINDOW)
    if result.returncode or not result.stdout.strip():
        return []
    records = json.loads(result.stdout)
    return records if isinstance(records, list) else ([] if records is None else [records])


def diagnose_file_1c_lock(job, log=print):
    """Non-destructive, best-effort diagnosis before a targeted eviction."""
    if os.name != "nt":
        return
    base = str(job.get("one_c_file") or "").strip()
    if not base:
        return
    try:
        pids = rm_locking_pids(Path(base) / "1Cv8.1CD")
        log(f"Файловая 1С: Restart Manager обнаружил {len(pids)} локальных процессов с открытым файлом базы" +
            (f" (PID: {', '.join(map(str, sorted(pids)))})" if pids else ""))
    except (OSError, RuntimeError, ValueError) as exc:
        log(f"Файловая 1С: диагностика блокировок Windows недоступна: {exc}")
    try:
        for pub in iis_publications_for_base(base):
            note = ("общий пул, отключать его нельзя" if pub["shared"]
                    else "отдельный пул; автоматическая остановка выключена")
            log(f"Файловая 1С: IIS-публикация {pub['app']}, пул {pub['pool']} "
                f"({note}); приложение не завершает процессы IIS")
    except (OSError, RuntimeError, ValueError) as exc:
        log(f"Файловая 1С: диагностика IIS недоступна: {exc}")
    try:
        handles = smb_open_files_for_base(base)
        if handles:
            hosts = sorted({str(h.get("ClientComputerName", "?")) for h in handles})
            log(f"Файловая 1С: SMB: открыто {len(handles)} файлов базы, клиенты: {', '.join(hosts)}. "
                "SMB-дескрипторы не закрываются автоматически")
    except (OSError, RuntimeError, ValueError) as exc:
        log(f"Файловая 1С: диагностика SMB недоступна: {exc}")
