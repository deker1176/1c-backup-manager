"""Safe, opt-in targeted Windows process termination for file-based 1C sessions.

File mode cannot use RAS. Only clients with a command-line /F referring to the
selected infobase can be terminated; remote computers require WinRM and admin
permissions. This cannot guarantee eviction of every file-mode session.
"""
from __future__ import annotations

import base64
import json
import ntpath
import os
import re
import subprocess

_CLIENTS = frozenset(("1cv8.exe", "1cv8c.exe", "1cv8s.exe"))
_FILE_ARG = re.compile(r'(?i)(?:^|\s)/F\s*(?:"([^"]+)"|([^\s"]+))')
_FILE_CONNECTION = re.compile(r'(?i)(?:^|\s)File\s*=\s*(?:"([^"]+)"|([^\s;"]+))')
_HOST = re.compile(r"^[A-Za-z0-9_.-]{1,253}$")


def split_items(value):
    values = value if isinstance(value, (list, tuple)) else re.split(r"[,;\r\n]+", str(value or ""))
    return [str(v).strip() for v in values if str(v).strip()]


def normalize_windows_path(value):
    path = str(value or "").strip().strip('"').replace("/", "\\")
    if not path:
        return ""
    return ntpath.normcase(ntpath.normpath(path)).rstrip("\\")


def commandline_matches_file_base(commandline, exe_name, allowed_paths):
    """Recognize only target /F or File= and never an unrelated Designer."""
    name = str(exe_name or "").casefold()
    commandline = str(commandline or "")
    if name not in _CLIENTS:
        return False
    if re.search(r"(?i)(?:^|\s)DESIGNER(?:\s|$)", commandline):
        return False
    if name == "1cv8.exe" and not re.search(r"(?i)(?:^|\s)ENTERPRISE(?:\s|$)", commandline):
        return False
    normalized_paths = {normalize_windows_path(p) for p in allowed_paths if str(p).strip()}
    for regex in (_FILE_ARG, _FILE_CONNECTION):
        for matched in regex.finditer(commandline):
            if normalize_windows_path(matched.group(1) or matched.group(2)) in normalized_paths:
                return True
    return False


def select_matching_processes(processes, paths):
    selected = []
    seen = set()
    for process in processes:
        if not isinstance(process, dict):
            continue
        try:
            pid = int(process.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        host = str(process.get("host") or "localhost")
        if pid <= 0 or (host, pid) in seen:
            continue
        if commandline_matches_file_base(process.get("commandline"), process.get("name"), paths):
            selected.append(process)
            seen.add((host, pid))
    return selected


def validate_file_disconnect_settings(job):
    mode = str(job.get("file_disconnect_mode") or "wait_only")
    if mode not in {"wait_only", "force_local", "force_hosts"}:
        raise ValueError("Неизвестный режим отключения файловых сеансов 1С")
    seconds = int(job.get("file_wait_seconds", 60) or 0)
    if not 0 <= seconds <= 3600:
        raise ValueError("Ожидание должно быть от 0 до 3600 секунд")
    if mode != "wait_only" and not job.get("file_force_confirmed", False):
        raise ValueError("Подтвердите риск потери несохранённых данных при завершении процессов")
    hosts = split_items(job.get("file_session_hosts")) if mode == "force_hosts" else []
    if mode == "force_hosts" and not hosts:
        raise ValueError("Для удалённого отключения укажите компьютеры Windows")
    for host in hosts:
        if not _HOST.fullmatch(host):
            raise ValueError(f"Недопустимое имя компьютера: {host}")
    return mode, seconds, hosts


# The JSON payload is base64-encoded to avoid interpolating user input in PS.
# Before terminating, Windows re-checks PID + command line + creation time.
_PS = r'''
$ErrorActionPreference='Stop'
$p=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__DATA__')))
$fn={
    param([string]$action,[int]$wantedPid,[string]$expectedCmd,[string]$expectedCreation)
    $ErrorActionPreference='Stop'
    if($action -eq 'list') {
        Get-CimInstance -ClassName Win32_Process -Filter "Name='1cv8.exe' OR Name='1cv8c.exe' OR Name='1cv8s.exe'" | ForEach-Object {
            [PSCustomObject]@{
                pid=[int]$_.ProcessId; name=[string]$_.Name;
                commandline=[string]$_.CommandLine;
                created=([datetime]$_.CreationDate).ToUniversalTime().ToString('o')
            }
        }
    } elseif($action -eq 'terminate') {
        $process=Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f $wantedPid)
        if(!$process){throw "Process $wantedPid no longer exists"}
        $created=([datetime]$process.CreationDate).ToUniversalTime().ToString('o')
        if((@('1cv8.exe','1cv8c.exe','1cv8s.exe') -notcontains [string]$process.Name) -or
           ($process.CommandLine -cne $expectedCmd) -or ($created -cne $expectedCreation)) {
           throw "Process identification changed; termination cancelled"
        }
        Stop-Process -Id $wantedPid -Force -ErrorAction Stop
        [PSCustomObject]@{pid=$wantedPid;terminated=$true}
    } else {throw 'Invalid operation'}
}
$a=@([string]$p.action,[int]$p.pid,[string]$p.commandline,[string]$p.created)
if($p.host -eq 'localhost') {
    $out=@(& $fn @a)
} else {
    $out=@(Invoke-Command -ComputerName $p.host -ScriptBlock $fn -ArgumentList $a -ErrorAction Stop)
}
Write-Output (ConvertTo-Json -InputObject @($out) -Depth 4 -Compress)
'''


def _powershell(host, operation, pid=0, commandline="", created=""):
    if os.name != "nt":
        raise RuntimeError("Отключение файловых сеансов работает только на Windows")
    if host != "localhost" and not _HOST.fullmatch(host):
        raise ValueError(f"Неверное имя компьютера: {host}")
    data = json.dumps({"host": host, "action": operation, "pid": int(pid),
                       "commandline": commandline, "created": created}, ensure_ascii=False)
    payload = base64.b64encode(data.encode("utf-8")).decode("ascii")
    command = _PS.replace("__DATA__", payload)
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, text=True, errors="replace", timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Не удалось вызвать PowerShell на {host}: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"Ошибка управления сеансами на {host}: {(result.stderr or '').strip()[-600:]}")
    raw = (result.stdout or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Не удалось прочитать ответ от {host}: {raw[-300:]}") from exc
    return value if isinstance(value, list) else ([] if value is None else [value])


def evict_targeted_clients(job, log=print):
    mode, _, other_hosts = validate_file_disconnect_settings(job)
    if mode == "wait_only":
        return 0
    paths = [str(job.get("one_c_file") or "")] + split_items(job.get("file_base_aliases", ""))
    if not paths[0].strip():
        raise ValueError("Укажите каталог файловой базы")
    hosts = ["localhost"] + (other_hosts if mode == "force_hosts" else [])
    selected = []
    for host in dict.fromkeys(hosts):
        processes = _powershell(host, "list")
        for p in processes:
            p["host"] = host
        matching = select_matching_processes(processes, paths)
        log(f"Файловая 1С: {host}: подходящих клиентских процессов: {len(matching)}")
        selected += matching
    if not selected:
        raise RuntimeError("Не найден процесс, однозначно относящийся к этой базе. "
                           "Укажите ПК клиентов и алиасы сетевого пути /F. "
                           "Закрывать чужие сеансы без подтверждения пути запрещено")
    for process in selected:
        _powershell(process["host"], "terminate", process["pid"],
                    str(process["commandline"]), str(process["created"]))
        log(f"Принудительно закрыт {process['name']} PID {process['pid']} на {process['host']} "
            "(несохранённые данные могут быть потеряны)")
    return len(selected)
