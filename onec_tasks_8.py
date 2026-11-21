# -*- coding: utf-8 -*-
"""1C 8.x maintenance and external processing tasks.

Do not reuse this module for 1C 7.7. Non-destructive check is the default.
All shell subprocesses use argument arrays (shell=False).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
from datetime import datetime

from backup_extensions import job_key, safe_component, make_target_folder


CHECK_FLAGS = {
    "check_reindex": "-ReIndex",
    "check_logical": "-LogIntegrity",
    "check_refs": "-LogAndRefsIntegrity",
    "check_totals": "-RecalcTotals",
    "check_compress": "-IBCompression",
    "check_rebuild": "-Rebuild",
}


def build_check_options(job: dict) -> list[str]:
    """Translate supported GUI choices into documented /IBCheckAndRepair flags."""
    result = ["/IBCheckAndRepair"]
    if job.get("check_reindex", True):
        result.append("-ReIndex")
    if job.get("check_refs", False):
        result.append("-LogAndRefsIntegrity")
    elif job.get("check_logical", True):
        result.append("-LogIntegrity")
    if job.get("check_totals", True):
        result.append("-RecalcTotals")
    if job.get("check_compress", False):
        result.append("-IBCompression")
    if job.get("check_rebuild", False):
        result.append("-Rebuild")
    if job.get("check_mode", "test") == "test":
        result.append("-TestOnly")
    elif job.get("check_mode") == "repair":
        if not job.get("repair_confirmed", False):
            raise RuntimeError("Для исправления необходимо отдельно подтвердить резервную копию в настройках задачи")
        refs = job.get("repair_bad_refs", "none")
        result.append({"none": "-BadRefNone", "create": "-BadRefCreate", "clear": "-BadRefClear"}[refs])
        data = job.get("repair_bad_data", "none")
        if data != "none":
            result.append({"create": "-BadDataCreate", "delete": "-BadDataDelete"}[data])
    else:
        raise ValueError("Неверный режим ТиИ: test или repair")
    hours = int(job.get("check_time_hours", 0) or 0)
    minutes = int(job.get("check_time_minutes", 0) or 0)
    if hours < 0 or hours > 999 or minutes < 0 or minutes > 59:
        raise ValueError("Лимит проверки должен быть от 0 до 999 часов и до 59 минут")
    if hours or minutes:
        result.append(f"-TimeLimit:{hours:03d}:{minutes:02d}")
    return result


def platform_exe(cfg, job, autodetect):
    mode = job.get("platform_mode", "inherit")
    if mode == "manual":
        exe = Path(job.get("one_c_exe", ""))
        if exe.name.lower() != "1cv8.exe":
            raise RuntimeError("Нужен 1cv8.exe для 1С 8, не 1cestart.exe")
    elif mode == "auto":
        exe = Path(autodetect()) / "1cv8.exe"
    else:
        value = str(cfg.get("platform_bin", "")).strip()
        if value:
            p = Path(value)
            exe = p if p.suffix.lower() == ".exe" else p / "1cv8.exe"
        else:
            exe = Path(autodetect()) / "1cv8.exe"
    if not exe.is_file():
        raise RuntimeError(f"Не найден 1cv8.exe: {exe}")
    return exe


def connection_args(job):
    kind = job.get("connection_kind", "server")
    if kind == "file":
        folder = Path(str(job.get("one_c_file", "")).strip())
        if not (folder / "1Cv8.1CD").is_file():
            raise RuntimeError(f"Файловая информационная база не найдена: {folder}")
        return [f"/F{folder}"]
    if kind == "server":
        srv = str(job.get("one_c_server", "")).strip()
        ref = str(job.get("ib_ref") or job.get("ref") or "").strip()
        if not srv or not ref:
            raise RuntimeError("Укажите сервер и Ref информационной базы 1С")
        return [f"/S{srv}\\{ref}"]
    raise RuntimeError("Веб-подключение не поддерживает эти служебные задачи")


def login_args(sec, job):
    user = str(job.get("ib_user", "")).strip()
    password = sec.get("db_passwords", {}).get(job_key(job), sec.get("db_passwords", {}).get(job.get("ref", ""), ""))
    if not user:
        return []
    return [f"/N{user}"] + ([f"/P{password}"] if password else [])


def masked_command(args):
    result = []
    hide_next = False
    for arg in args:
        if hide_next:
            result.append("***")
            hide_next = False
        elif arg == "/C":
            result.append("/C")
            hide_next = True  # /C may include private exchange parameters
        elif arg.lower().startswith("/uc"):
            result.append("/UC***")
        elif arg.lower().startswith("/p") and arg.lower() != "/platform":
            result.append("/P***")
        else:
            result.append(arg)
    return " ".join(result)


def run_1cv8(args, out_log, log, timeout=24 * 3600):
    log("1С 8: " + masked_command(args))
    # The Designer may print to /Out or stderr depending on platform version.
    try:
        cp = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=max(30, timeout), creationflags=(0x08000000 if os.name == "nt" else 0))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"1С не завершилась за отведённое время. Журнал: {out_log}") from exc
    if cp.returncode != 0:
        raise RuntimeError(f"1С завершилась с кодом {cp.returncode}. Журнал: {out_log}. {cp.stderr[-300:]}")
    log(f"1С: выполнение завершилось, журнал: {out_log}")
    return Path(out_log)


def verified_repair_copy(exe, base_args, auth_args, permission_args, dest_folder, log):
    """Create a fresh nonempty .dt before any modifying check/repair run."""
    dest_folder.mkdir(parents=True, exist_ok=True)
    destination = dest_folder / f"pre_repair_{datetime.now():%Y-%m-%d_%H-%M-%S}.dt"
    outlog = destination.with_suffix(".log")
    if destination.exists():
        raise RuntimeError(f"Копия уже существует: {destination}")
    args = [str(exe), "DESIGNER", *base_args, *auth_args, *permission_args,
            "/DumpIB", str(destination), "/Out", str(outlog), "-NoTruncate",
            "/DisableStartupDialogs", "/DisableStartupMessages"]
    run_1cv8(args, outlog, log)
    if not destination.exists() or destination.stat().st_size <= 0:
        raise RuntimeError("Исправление отменено: свежая резервная копия .dt не создана")
    h = hashlib.sha256()
    with destination.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    (Path(str(destination) + ".sha256")).write_text(f"{h.hexdigest()}  {destination.name}\n", encoding="ascii")
    log(f"Перед исправлением создана локальная копия: {destination} ({destination.stat().st_size} байт)")
    return destination


def _with_ras_lock(cfg, sec, job, backend, log, callback):
    effective = dict(cfg)
    mode = job.get("platform_mode", "inherit")
    if mode == "manual":
        effective["platform_bin"] = str(Path(job.get("one_c_exe", "")).parent)
    elif mode == "auto":
        effective["platform_bin"] = ""
    server = str(job.get("one_c_server", "")).strip()
    if server:
        effective["server"] = server
        if server.casefold() != str(cfg.get("server", "")).casefold():
            old_server = str(cfg.get("server", "")).strip()
            effective["ras_endpoint"] = str(job.get("one_c_ras") or (
                f"{server}:1545" if old_server else "127.0.0.1:1545"))
            effective["cluster_agent"] = f"{server}:1540"
            effective["cluster_uuid"] = ""
        elif job.get("one_c_ras"):
            effective["ras_endpoint"] = str(job["one_c_ras"])
    if str(job.get("wait_seconds_override", "")).strip():
        effective["wait_seconds"] = max(0, int(job["wait_seconds_override"]))
    if job.get("force_terminate_override") is not None:
        effective["force_terminate"] = bool(job["force_terminate_override"])
    sec_for_job = dict(sec)
    if str(job.get("cluster_user", "")).strip():
        effective["cluster_user"] = job["cluster_user"]
        sec_for_job["cluster_password"] = sec.get("cluster_job_passwords", {}).get(job_key(job), sec.get("cluster_password", ""))
    engine = backend.BackupEngine(effective, sec_for_job, log)
    engine.prepare()
    try:
        clu = engine.get_cluster_uuid()
        ib, _ = engine.find_infobase(clu, str(job.get("ib_ref") or job.get("ref")))
        info = engine.get_infobase_info(clu, ib, job)
        if backend.bool_value(info.get("sessions-deny", "off")):
            raise RuntimeError("База уже заблокирована другим администратором. Изменять её блокировку нельзя")
        prev_jobs_denied = backend.bool_value(info.get("scheduled-jobs-deny", "off"))
        import secrets as system_secrets
        code = "MNT_" + system_secrets.token_hex(12)
        blocked = False
        try:
            engine.block_infobase(clu, ib, job, code)
            blocked = True
            engine.wait_and_kick(clu, ib)
            return callback([f"/UC{code}"])
        finally:
            if blocked:
                log("1С: снятие блокировки после обслуживания")
                engine.unblock_infobase(clu, ib, job, scheduled_was_denied=prev_jobs_denied)
                log("1С: сеансы снова разрешены")
    finally:
        engine.close()


def run_1c_check(cfg, sec, job, backend, log):
    """Check/repair with RAS exclusive access for server mode. Always test-only by default."""
    exe = platform_exe(cfg, job, backend.autodetect_platform_bin)
    base_args = connection_args(job)
    auth_args = login_args(sec, job)
    options = build_check_options(job)
    target = make_target_folder(cfg, job) / "maintenance_logs"
    target.mkdir(parents=True, exist_ok=True)
    outlog = target / f"check_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"
    repair = job.get("check_mode", "test") == "repair"
    if repair and not job.get("repair_confirmed"):
        raise RuntimeError("Нельзя запускать исправление без отдельного подтверждения")

    def actual_run(permission_args):
        if repair:
            verified_repair_copy(exe, base_args, auth_args, permission_args,
                                 make_target_folder(cfg, job) / "before_repair", log)
        cmd = [str(exe), "DESIGNER", *base_args, *auth_args, *permission_args,
               *options, "/Out", str(outlog), "-NoTruncate", "/DisableStartupDialogs", "/DisableStartupMessages"]
        return run_1cv8(cmd, outlog, log)

    if job.get("connection_kind", "server") == "server":
        return _with_ras_lock(cfg, sec, job, backend, log, actual_run)
    if repair and not job.get("file_offline_confirmed", False):
        raise RuntimeError("Для исправления файловой 1С подтвердите, что все пользователи вышли и доступ к папке закрыт")
    return actual_run([])


def run_1c_procedure(cfg, sec, job, backend, log):
    """Run a supplied external .epf; this does not magically execute a configuration procedure."""
    exe = platform_exe(cfg, job, backend.autodetect_platform_bin)
    processing = Path(str(job.get("procedure_epf", "")).strip())
    if processing.suffix.lower() != ".epf" or not processing.is_file():
        raise RuntimeError("Для вызова процедуры укажите существующую внешнюю обработку .epf")
    base_args = connection_args(job)
    auth_args = login_args(sec, job)
    folder = make_target_folder(cfg, job) / "procedure_logs"
    folder.mkdir(parents=True, exist_ok=True)
    outlog = folder / f"procedure_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"
    param = str(job.get("procedure_parameter", "")).strip()

    def actual_run(permission_args):
        cmd = [str(exe), "ENTERPRISE", *base_args, *auth_args, *permission_args,
               "/Execute", str(processing)]
        if param:
            cmd.extend(["/C", param])
        cmd.extend(["/Out", str(outlog), "-NoTruncate", "/DisableStartupMessages", "/DisableStartupDialogs"])
        hrs = int(job.get("procedure_timeout_hours", 2) or 2)
        # Requires .epf to close 1C after completing the operation.
        return run_1cv8(cmd, outlog, log, timeout=max(60, hrs * 3600))

    if job.get("procedure_exclusive", False) and job.get("connection_kind", "server") == "server":
        return _with_ras_lock(cfg, sec, job, backend, log, actual_run)
    return actual_run([])
