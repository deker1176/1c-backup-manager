"""Opt-in short IIS pause for consistent file-based 1C snapshots.

An independent Windows Scheduler SYSTEM watchdog is registered before IIS stops.
The source is held with CreateFileW SHARE_NONE throughout the copy; no live,
unlocked filesystem snapshot, global IIS reset, or SMB file-handle closure.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from file_lock_diagnostics import iis_publications_for_base

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
POOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")


def in_window(now, start="23:00", end="06:00"):
    def parse(value):
        m = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", str(value))
        if not m:
            raise ValueError("Окно обслуживания IIS: ожидается время ЧЧ:ММ")
        return int(m[1]) * 60 + int(m[2])
    a, b = parse(start), parse(end)
    if a == b:
        raise ValueError("Начало и конец окна обслуживания IIS не должны совпадать")
    t = now.hour * 60 + now.minute
    return a <= t < b if a < b else (t >= a or t < b)


def maintenance_plan(job, now=None, discover=None):
    if not job.get("file_iis_maintenance_enabled"):
        raise RuntimeError("Не дано разрешение на остановку IIS для этого задания")
    if not job.get("file_force_confirmed"):
        raise RuntimeError("Для обслуживания IIS включите автоматическое завершение пользователей")
    if os.name != "nt":
        raise RuntimeError("Обслуживание IIS поддерживается только на Windows")
    pool = str(job.get("file_iis_pool_name") or "").strip()
    if not POOL_NAME.fullmatch(pool):
        raise ValueError("Укажите точное имя разрешённого пула IIS")
    start = str(job.get("file_iis_window_start") or "23:00")
    end = str(job.get("file_iis_window_end") or "06:00")
    if not in_window(now or datetime.now(), start, end):
        raise RuntimeError(f"Остановка IIS разрешена только в окне {start}–{end}")
    base = Path(str(job.get("one_c_file") or ""))
    if not (base / "1Cv8.1CD").is_file():
        raise RuntimeError("Файловая база 1С не найдена")
    pubs = (discover or iis_publications_for_base)(str(base))
    if not pubs:
        raise RuntimeError("Не найдена IIS-публикация с точным File= для этой базы")
    actual = {p["pool"] for p in pubs}
    if actual != {pool}:
        raise RuntimeError(f"Найдены пулы IIS: {', '.join(sorted(actual))}, "
                           f"но разрешён только {pool}. Остановка отменена")
    return pool, pubs


def _ps(script, timeout=45):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                          "-EncodedCommand", encoded], capture_output=True, text=True,
                         errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)
    if run.returncode:
        raise RuntimeError("Планировщик Windows: " +
                           ((run.stderr or run.stdout or "").strip()[-800:]))
    return run.stdout


def _appcmd(*args, timeout=45):
    exe = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "inetsrv" / "appcmd.exe"
    if not exe.is_file():
        raise RuntimeError(f"Не найден AppCmd IIS: {exe}")
    run = subprocess.run([str(exe), *args], capture_output=True, text=True,
                         errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)
    if run.returncode:
        raise RuntimeError(f"IIS {' '.join(args)}: "
                           f"{(run.stderr or run.stdout or '').strip()[-800:]}")
    return run.stdout.strip()


def pool_state(pool):
    state = _appcmd("list", "apppool", pool, "/text:state")
    if state not in {"Started", "Stopped", "Stopping", "Starting"}:
        raise RuntimeError(f"Неизвестное состояние пула IIS {pool}: {state}")
    return state


def wait_state(pool, wanted, seconds=90):
    deadline = time.monotonic() + seconds
    last = "unknown"
    while time.monotonic() < deadline:
        last = pool_state(pool)
        if last == wanted:
            return
        time.sleep(1)
    last = pool_state(pool)
    if last != wanted:
        raise RuntimeError(
            f"Пул IIS {pool} не перешёл в состояние {wanted} за {seconds} секунд; "
            f"текущее состояние: {last}")


def stop_pool(pool, log=print):
    """IIS shutdownTimeLimit defaults to 90s; never give up after 45s.

    Even if appcmd times out, WAS may have accepted the stop. Confirm its
    actual state before proceeding to exclusive 1C database snapshot.
    """
    seconds = 180
    began = time.monotonic()
    try:
        _appcmd("stop", "apppool", pool, timeout=seconds)
    except subprocess.TimeoutExpired as exc:
        log(f"IIS: appcmd stop {pool} выполняется более {seconds} сек.; "
            "проверяем фактическое состояние пула")
        try:
            wait_state(pool, "Stopped", seconds=30)
        except Exception as state_exc:
            raise RuntimeError(
                f"IIS: пул {pool} не остановлен за {seconds}+30 сек.; "
                f"копирование отменено: {state_exc}") from exc
    wait_state(pool, "Stopped", seconds=90)
    log(f"IIS: пул {pool} подтверждён как Stopped; "
        f"прошло {int(time.monotonic() - began)} сек.")


def start_pool(pool):
    state = pool_state(pool)
    if state == "Started":
        return
    if state == "Stopping":
        wait_state(pool, "Stopped", seconds=120)
    if pool_state(pool) == "Stopped":
        try:
            _appcmd("start", "apppool", pool, timeout=150)
        except subprocess.TimeoutExpired as exc:
            # AppCmd may time out even after WAS accepts the start request.
            try:
                wait_state(pool, "Started", seconds=30)
            except Exception as state_exc:
                raise RuntimeError(
                    f"IIS: пул {pool} не удалось запустить: {state_exc}") from exc
    wait_state(pool, "Started", seconds=90)


class IISWatchdog:
    """One-shot independent SYSTEM task; fail closed if registration is denied."""
    def __init__(self, pool):
        self.pool = pool
        self.name = "OneCBackupManager-RestoreIIS-" + uuid.uuid4().hex
        self.marker = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                       / "1CBackupManager" / "iis_recovery" / (self.name + ".json"))
        self.armed = False

    def arm(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        data = {"pool": self.pool, "marker": str(self.marker)}
        self.marker.write_text(json.dumps(data), encoding="utf-8")
        payload = base64.b64encode(json.dumps(data).encode()).decode()
        recovery = (
            "$ErrorActionPreference='Stop';"
            "$p=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString("
            "[Convert]::FromBase64String('" + payload + "')));"
            "if(Test-Path -LiteralPath $p.marker){"
            "$app=Join-Path $env:WINDIR 'System32\\inetsrv\\appcmd.exe';"
            "$state=(& $app list apppool $p.pool /text:state);"
            "if($state -ne 'Started'){& $app start apppool $p.pool;"
            "if($LASTEXITCODE -ne 0){exit 1}};"
            "Remove-Item -LiteralPath $p.marker -Force}"
        )
        code = base64.b64encode(recovery.encode("utf-16le")).decode()
        script = (
            "$ErrorActionPreference='Stop';"
            "$a=New-ScheduledTaskAction -Execute 'powershell.exe' "
            "-Argument '-NoProfile -NonInteractive -EncodedCommand " + code + "';"
            "$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(20);"
            "$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable;"
            "Register-ScheduledTask -TaskName '" + self.name + "' -Action $a -Trigger $t "
            "-Settings $s -User 'SYSTEM' -RunLevel Highest -Force | Out-Null"
        )
        try:
            _ps(script)
        except Exception as exc:
            self.marker.unlink(missing_ok=True)
            raise RuntimeError("Не удалось создать независимое аварийное задание "
                               f"восстановления IIS; пул не остановлен: {exc}") from exc
        self.armed = True

    def disarm(self):
        # Marker removed first; a concurrently triggered task then does nothing.
        self.marker.unlink(missing_ok=True)
        if self.armed:
            _ps("Unregister-ScheduledTask -TaskName '" + self.name
                + "' -Confirm:$false -ErrorAction Stop")
            self.armed = False


@contextmanager
def pause_pool(pool, log=print):
    if pool_state(pool) != "Started":
        raise RuntimeError(f"Пул IIS {pool} уже остановлен, состояние не меняем")
    watchdog = IISWatchdog(pool)
    watchdog.arm()
    attempted = False
    restored = False
    try:
        attempted = True
        log(f"IIS: временная остановка {pool}; аварийный запуск зарегистрирован в Планировщике")
        stop_pool(pool, log=log)
        yield
    finally:
        if attempted:
            try:
                prior = pool_state(pool)
                start_pool(pool)
                restored = True
                if prior == "Started":
                    log(f"IIS: пул {pool} уже работает; повторный запуск не требовался")
                else:
                    log(f"IIS: пул {pool} восстановлен (Started)")
            except Exception as exc:
                log(f"КРИТИЧЕСКИ: IIS {pool} не восстановлен: {exc}. "
                    "Аварийная задача в Планировщике сохранена")
                raise
        if restored:
            try:
                watchdog.disarm()
            except Exception as exc:
                log(f"Предупреждение: не удалось удалить задачу восстановления IIS: {exc}")


@contextmanager
def exclusive_file(source):
    if os.name != "nt":
        raise RuntimeError("Эксклюзивное копирование доступно только на Windows")
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # GENERIC_READ, SHARE_NONE, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN
    handle = create(str(source), 0x80000000, 0, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Исходная база занята", str(source))
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb", buffering=0) as stream:
        yield stream


def copy_exclusive(source, stage, log=print):
    source, stage = Path(source), Path(stage)
    target, partial = stage / "1Cv8.1CD", stage / "1Cv8.1CD.partial"
    deadline = time.monotonic() + 35
    while True:
        try:
            with exclusive_file(source) as stream:
                expected = source.stat().st_size
                if shutil.disk_usage(stage).free < expected + 16 * 1024 * 1024:
                    raise RuntimeError("Недостаточно места для временной копии базы")
                size = 0
                digest = hashlib.sha256()
                with partial.open("xb") as output:
                    while True:
                        block = stream.read(4 * 1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                        size += len(block)
                        digest.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                if size != expected:
                    raise RuntimeError("Размер копии не соответствует исходному файлу")
                partial.replace(target)
                log(f"Файловая 1С: снимок {size} байт, SHA256 {digest.hexdigest()}")
                return target
        except OSError as exc:
            partial.unlink(missing_ok=True)
            if getattr(exc, "winerror", None) not in (5, 32, 33) and \
               getattr(exc, "errno", None) not in (5, 13, 32):
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError("После остановки IIS файл 1Cv8.1CD всё ещё занят "
                                   "другим процессом/SMB. Копирование отменено") from exc
            time.sleep(1)


def stage_with_iis_pause(job, stage, log=print):
    pool, pubs = maintenance_plan(job)
    source = Path(str(job["one_c_file"])) / "1Cv8.1CD"
    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    a = os.path.normcase(str(source.parent.resolve()))
    b = os.path.normcase(str(stage.resolve()))
    if os.path.commonpath((a, b)) == a:
        raise RuntimeError("Временный каталог нельзя размещать внутри базы 1С")
    if shutil.disk_usage(stage).free < source.stat().st_size + 16 * 1024 * 1024:
        raise RuntimeError("Недостаточно места для снимка базы; IIS не остановлен")
    log(f"IIS: публикации {', '.join(p['app'] for p in pubs)}; пул {pool}")
    with pause_pool(pool, log):
        result = copy_exclusive(source, stage, log)
    log("IIS: доступ восстановлен; запускаем DumpIB из временного снимка")
    return result
