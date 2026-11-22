"""Automatic, configuration-independent discovery of Windows 1C web hosts.

Only acts on IIS application pools and Windows Apache services with a verified
default.vrd File= reference to exactly the selected file infobase. Never guesses
a web service by process name or touches unrelated SMB handles.

A separate opt-in checkbox permits any confirmed *shared* service/pool to be
paused. Other sites using that host become unavailable while copying.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from datetime import datetime
import glob
import json
import os
from pathlib import Path
import re
import time
import uuid

from file_lock_diagnostics import iis_publications_for_base, publication_uses_file_base
from iis_maintenance import (_ps, pause_pool, copy_exclusive, IISWatchdog)

_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")
_EXE = re.compile(r'(?i)(?:"([^"]+[/\\](?:httpd|apache)\.exe)"|(\S+[/\\](?:httpd|apache)\.exe))')
_CONFIG = re.compile(r'(?i)(?:^|\s)-f\s+(?:"([^"]+)"|(\S+))')
_ROOT = re.compile(r'(?im)^\s*ServerRoot\s+(?:"([^"]+)"|(\S+))\s*$')
_INCLUDE = re.compile(r'(?im)^\s*Include(?:Optional)?\s+(?:"([^"]+)"|(\S+))\s*$')
_DESCRIPTOR = re.compile(r'(?im)^\s*ManagedApplicationDescriptor\s+(?:"([^"]+)"|(\S+))\s*$')


def apache_service_config(image_path):
    """Find only the config associated with a particular installed Apache service."""
    exe = _EXE.search(str(image_path or ""))
    if not exe:
        return None
    exe_path = Path(exe.group(1) or exe.group(2))
    root = exe_path.parent.parent
    argument = _CONFIG.search(image_path)
    if not argument:
        return root / "conf" / "httpd.conf"
    candidate = Path(argument.group(1) or argument.group(2))
    return candidate if candidate.is_absolute() else root / candidate


def apache_config_files(config, max_files=100):
    """Resolve Apache Include/IncludeOptional (bounded, no recursive disk crawl)."""
    config = Path(config)
    if not config.is_file():
        return []
    try:
        head = config.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return []
    root_match = _ROOT.search(head)
    root = Path(root_match.group(1) or root_match.group(2)) if root_match else config.parent.parent
    files, pending, seen = [], [config], set()
    while pending and len(seen) < max_files:
        path = pending.pop()
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.stat().st_size > 2_000_000:
                continue
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        files.append((path, content))
        for entry in _INCLUDE.finditer(content):
            item = entry.group(1) or entry.group(2)
            pattern = Path(item)
            if not pattern.is_absolute():
                pattern = root / pattern
            # glob only the explicitly configured directory, not the whole disk
            pending.extend(Path(p) for p in sorted(glob.glob(str(pattern)))[:max_files])
    return files


def apache_config_publishes_base(config, base):
    """Match descriptor File=, not an Apache service name or a URL alias."""
    for path, content in apache_config_files(config):
        for match in _DESCRIPTOR.finditer(content):
            target = match.group(1) or match.group(2)
            descriptor = Path(target)
            if not descriptor.is_absolute():
                descriptor = path.parent / descriptor
            try:
                xml = descriptor.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            if publication_uses_file_base(xml, base):
                return True
    return False


def running_apache_services():
    """Returns only running Windows Apache services. No service modifications."""
    if os.name != "nt":
        return []
    script = (
        "$ErrorActionPreference='Stop';"
        "$v=@(Get-CimInstance Win32_Service | Where-Object { "
        "$_.State -eq 'Running' -and $_.PathName -match '(?i)(httpd|apache)\\.exe' } "
        "| Select-Object Name,PathName,State);"
        "$json=ConvertTo-Json -InputObject $v -Compress -Depth 3;"
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))"
    )
    raw = _ps(script, timeout=35).strip()
    if not raw:
        return []
    data = json.loads(base64.b64decode(raw).decode("utf-8"))
    return data if isinstance(data, list) else ([] if data is None else [data])


def apache_services_for_base(base, services=None):
    matches = []
    services = running_apache_services() if services is None else services
    for svc in services:
        name = str(svc.get("Name") or "")
        if not _SERVICE_NAME.fullmatch(name):
            continue
        conf = apache_service_config(svc.get("PathName", ""))
        if conf and apache_config_publishes_base(conf, base):
            matches.append({"name": name, "config": str(conf)})
    return matches


def web_targets_for_base(base):
    """Verify both host types; error if discovery is untrustworthy."""
    iis = iis_publications_for_base(base)
    apache = apache_services_for_base(base)
    return sorted({str(p["pool"]) for p in iis}), sorted({s["name"] for s in apache}), iis


class ApacheWatchdog(IISWatchdog):
    """Independent one-shot SYSTEM restore, scoped to a verified service."""
    def __init__(self, service_name):
        super().__init__(service_name)
        self.name = "OneCBackupManager-RestoreApache-" + uuid.uuid4().hex
        self.marker = self.marker.parent / (self.name + ".json")

    def arm(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        payload = {"name": self.pool, "marker": str(self.marker)}
        self.marker.write_text(json.dumps(payload), encoding="utf-8")
        encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        recovery = (
            "$ErrorActionPreference='Stop';"
            "$p=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString("
            "[Convert]::FromBase64String('" + encoded + "')));"
            "if(Test-Path -LiteralPath $p.marker){"
            "$svc=Get-Service -Name $p.name -ErrorAction Stop;"
            "if($svc.Status -ne 'Running'){"
            "Start-Service -Name $p.name -ErrorAction Stop;"
            "(Get-Service -Name $p.name).WaitForStatus('Running','00:02:00')};"
            "Remove-Item -LiteralPath $p.marker -Force}"
        )
        code = base64.b64encode(recovery.encode("utf-16le")).decode("ascii")
        script = (
            "$ErrorActionPreference='Stop';"
            "$a=New-ScheduledTaskAction -Execute 'powershell.exe' "
            "-Argument '-NoProfile -NonInteractive -EncodedCommand " + code + "';"
            "$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(20);"
            "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable "
            "-ExecutionTimeLimit (New-TimeSpan -Minutes 5);"
            "Register-ScheduledTask -TaskName '" + self.name + "' "
            "-Action $a -Trigger $t -Settings $s "
            "-User 'SYSTEM' -RunLevel Highest -Force | Out-Null"
        )
        try:
            _ps(script)
        except Exception as exc:
            self.marker.unlink(missing_ok=True)
            raise RuntimeError(
                f"Не зарегистрировано аварийное восстановление Apache {self.pool}; "
                f"служба не остановлена: {exc}") from exc
        self.armed = True


def apache_service_state(name):
    if not _SERVICE_NAME.fullmatch(name):
        raise ValueError("Недопустимое имя службы Apache")
    return _ps("(Get-Service -Name '" + name + "' -ErrorAction Stop).Status.ToString()").strip()


def apache_stop(name):
    _ps("Stop-Service -Name '" + name + "' -ErrorAction Stop; "
        "(Get-Service -Name '" + name + "').WaitForStatus('Stopped','00:02:00')",
        timeout=150)


def apache_start(name):
    state = apache_service_state(name)
    if state != "Running":
        _ps("Start-Service -Name '" + name + "' -ErrorAction Stop; "
            "(Get-Service -Name '" + name + "').WaitForStatus('Running','00:02:00')",
            timeout=150)


@contextmanager
def pause_apache_service(name, log=print):
    if apache_service_state(name) != "Running":
        raise RuntimeError(f"Служба Apache {name} не запущена; состояние не меняем")
    guard = ApacheWatchdog(name)
    guard.arm()
    stopped = False
    restored = False
    try:
        stopped = True
        log(f"Apache: остановка службы {name}; восстановление от SYSTEM зарегистрировано")
        apache_stop(name)
        yield
    finally:
        if stopped:
            try:
                apache_start(name)
                restored = True
                log(f"Apache: служба {name} запущена повторно")
            except Exception as exc:
                log(f"КРИТИЧЕСКИ: служба Apache {name} не восстановлена: {exc}. "
                    "Независимый watchdog оставлен активным")
                raise
        if restored:
            try:
                guard.disarm()
            except Exception as exc:
                log(f"Не удалось удалить аварийное задание Apache: {exc}")


def stage_with_web_pause(job, stage, log=print):
    if not job.get("file_web_maintenance_enabled"):
        raise RuntimeError("Не дано разрешение на остановку IIS/Apache")
    if not job.get("file_force_confirmed"):
        raise RuntimeError("Автоматическое завершение пользователей не включено")
    if os.name != "nt":
        raise RuntimeError("Остановка веб-служб доступна только на Windows")
    source_dir = Path(str(job["one_c_file"]))
    source = source_dir / "1Cv8.1CD"
    iis_pools, apache_services, pubs = web_targets_for_base(str(source_dir))
    if not iis_pools and not apache_services:
        raise RuntimeError("Не найдена подтверждённая веб-публикация этой базы в IIS/Apache")
    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    if stage.resolve().is_relative_to(source_dir.resolve()):
        raise RuntimeError("Временная копия не может располагаться внутри рабочей базы")
    import shutil
    if shutil.disk_usage(stage).free < source.stat().st_size + 16 * 1024 * 1024:
        raise RuntimeError("Недостаточно места для временной базы; веб-службы не остановлены")
    log("Найдены IIS-пулы: " + (", ".join(iis_pools) or "нет") +
        "; службы Apache: " + (", ".join(apache_services) or "нет"))
    for pub in pubs:
        if pub.get("shared"):
            log("ВНИМАНИЕ: общий пул " + str(pub["pool"]) +
                ", могут прерваться другие сайты: " +
                ", ".join(pub.get("pool_apps", [])))
    if apache_services:
        log("ВНИМАНИЕ: остановка службы Apache может прервать ВСЕ её сайты")
    # ExitStack restores each target in reverse, including if later stops fail.
    with ExitStack() as stack:
        for pool in iis_pools:
            stack.enter_context(pause_pool(pool, log=log))
        for service in apache_services:
            stack.enter_context(pause_apache_service(service, log=log))
        copied = copy_exclusive(source, stage, log=log)
    log("IIS/Apache восстановлены; DumpIB будет выполнен из временной базы")
    return copied
