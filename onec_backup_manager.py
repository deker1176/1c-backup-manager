# -*- coding: utf-8 -*-
"""
1C Backup Manager v2
- Dynamic infobase list with Add/Remove
- Friendly GUI error messages instead of raw tracebacks
- Per-base schedule time HH:MM
- Optional Windows service scheduler (pywin32)
- Keeps previous backup logic intact
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import shutil
import uuid
from datetime import datetime, timedelta
from contextlib import nullcontext
from task_runtime import task_deadline, run_commands, limit_seconds
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from backup_extensions import (
    job_type, job_key, make_target_folder, backup_mssql, backup_files,
    finalize_backup, ftp_check, global_backup_lock, prune_local, safe_component,
    pack_single, sha256_file, ftp_upload
)

APP_NAME = "1C Backup Manager"
APP_VERSION = "5.4"
CONTACT_TELEGRAM = "@dek76"
CONFIG_NAME = "config.json"
SECRETS_NAME = "secrets.dat"
SVC_STATE_NAME = "scheduler_state.json"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SERVICE_AVAILABLE = False
if os.name == "nt":
    try:
        import win32serviceutil
        import win32service
        import win32event
        import servicemanager
        SERVICE_AVAILABLE = True
    except Exception:
        SERVICE_AVAILABLE = False


def is_windows_service_session() -> bool:
    """Return True when the process was launched in Windows Session 0 (SCM/service)."""
    if os.name != "nt":
        return False
    try:
        session_id = wintypes.DWORD()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            os.getpid(), ctypes.byref(session_id)
        )
        return bool(ok) and session_id.value == 0
    except Exception:
        return False


def decode_service_output(data: bytes) -> str:
    if not data:
        return ""
    # pywin32 service-util messages on Russian Windows are often written in OEM CP866.
    for enc in ("utf-8", "cp866", "cp1251", "mbcs"):
        try:
            return data.decode(enc)
        except Exception:
            pass
    return data.decode(errors="replace")


def friendly_service_error(text: str, returncode: int) -> str:
    low = (text or "").lower()
    if "(5)" in low or "access is denied" in low or "отказано в доступе" in low:
        return "Недостаточно прав для управления службой. Запустите программу от имени администратора."
    if "1053" in low or "timely fashion" in low or "своевременно" in low:
        return "Служба не ответила диспетчеру Windows вовремя. Обновите EXE до исправленной версии и повторите запуск."
    if "1069" in low or "logon failure" in low or "ошибки входа" in low:
        return "Не удалось войти под учётной записью службы. Проверьте пользователя и пароль в свойствах службы."
    if text.strip():
        return text.strip()
    return f"Ошибка управления службой. Код возврата: {returncode}"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / CONFIG_NAME
SECRETS_PATH = app_dir() / SECRETS_NAME
SVC_STATE_PATH = app_dir() / SVC_STATE_NAME


DEFAULT_CONFIG = {
    "server": "",
    "ras_endpoint": "127.0.0.1:1545",
    "cluster_agent": "",
    "cluster_uuid": "",
    "cluster_user": "",
    "platform_bin": "",
    "ui_language": "ru",
    "backup_root": r"D:\Backup\1C_DT",
    "wait_seconds": 120,
    "force_terminate": True,
    "default_keep": 7,
    "service_poll_seconds": 30,
    "ftp": {"host": "", "port": 21, "tls": True, "user": "", "path": "/backups"},
    "ftp_profiles": [],
    "task_folders": [],
    "notifications": {"smtp": {"host": "", "port": 587, "security": "STARTTLS", "user": "", "from": "", "to": ""}, "telegram": {"chat_id": ""}, "max": {"recipient_type": "chat_id", "recipient_id": ""}},
    "ui_close_action": "ask",
    "ui_minimize_to_tray": True,
    "journal_retention_days": 60,
    "databases": []
}


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob_from_bytes(data: bytes):
    buf = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf


def dpapi_encrypt(text: str) -> str:
    if not text:
        return ""
    if os.name != "nt":
        raise RuntimeError("DPAPI is available only on Windows.")
    data = text.encode("utf-8")
    in_blob, _ = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_decrypt(encoded: str) -> str:
    if not encoded:
        return ""
    if os.name != "nt":
        raise RuntimeError("DPAPI is available only on Windows.")
    raw = base64.b64decode(encoded)
    in_blob, _ = _blob_from_bytes(raw)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return data.decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        return {"cluster_password": "", "db_passwords": {}, "sql_passwords": {}, "ftp_password": "", "ftp_profile_passwords": {}, "cluster_job_passwords": {}, "smtp_password": "", "telegram_bot_token": "", "max_bot_token": ""}
    wrapped = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    return {
        "cluster_password": dpapi_decrypt(wrapped.get("cluster_password", "")),
        "db_passwords": {
            k: dpapi_decrypt(v) for k, v in wrapped.get("db_passwords", {}).items()
        },
        "sql_passwords": {
            k: dpapi_decrypt(v) for k, v in wrapped.get("sql_passwords", {}).items()
        },
        "ftp_password": dpapi_decrypt(wrapped.get("ftp_password", "")),
        "ftp_profile_passwords": {k: dpapi_decrypt(v) for k, v in wrapped.get("ftp_profile_passwords", {}).items()},
        "cluster_job_passwords": {k: dpapi_decrypt(v) for k, v in wrapped.get("cluster_job_passwords", {}).items()},
        "smtp_password": dpapi_decrypt(wrapped.get("smtp_password", "")),
        "telegram_bot_token": dpapi_decrypt(wrapped.get("telegram_bot_token", "")),
        "max_bot_token": dpapi_decrypt(wrapped.get("max_bot_token", "")),
    }


def save_secrets(cluster_password: str, db_passwords: dict[str, str],
                 sql_passwords: dict[str,str] | None = None, ftp_password: str = "",
                 ftp_profile_passwords: dict[str, str] | None = None,
                 cluster_job_passwords: dict[str, str] | None = None,
                 smtp_password: str = "", telegram_bot_token: str = "",
                 max_bot_token: str = "") -> None:
    wrapped = {
        "cluster_password": dpapi_encrypt(cluster_password),
        "db_passwords": {k: dpapi_encrypt(v) for k, v in db_passwords.items()},
        "sql_passwords": {k: dpapi_encrypt(v) for k,v in (sql_passwords or {}).items()},
        "ftp_password": dpapi_encrypt(ftp_password),
        "ftp_profile_passwords": {k: dpapi_encrypt(v) for k, v in (ftp_profile_passwords or {}).items()},
        "cluster_job_passwords": {k: dpapi_encrypt(v) for k, v in (cluster_job_passwords or {}).items()},
        "smtp_password": dpapi_encrypt(smtp_password),
        "telegram_bot_token": dpapi_encrypt(telegram_bot_token),
        "max_bot_token": dpapi_encrypt(max_bot_token),
    }
    tmp = SECRETS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(wrapped, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SECRETS_PATH)


def deep_copy(obj):
    return json.loads(json.dumps(obj))


def normalize_db(db: dict) -> dict:
    result = dict(db)
    kind = result.get("type", "1c")
    result.setdefault("type", kind)
    result["enabled"] = bool(result.get("enabled", True))
    result["name"] = str(result.get("name", ""))
    result["ref"] = str(result.get("ref", ""))
    result["ib_user"] = str(result.get("ib_user", ""))
    result["keep"] = max(1, int(result.get("keep", 7) or 7))
    result["schedule_time"] = str(result.get("schedule_time", ""))
    result["folder"] = str(result.get("folder", ""))
    result.setdefault("archive_format", "zip" if kind == "files" else "none")
    result.setdefault("remove_raw_after_pack", False)
    result.setdefault("ftp_enabled", False)
    result.setdefault("sql_server", ".")
    result.setdefault("sql_database", "")
    result.setdefault("sql_backup_dir", "")
    result.setdefault("sql_driver", "ODBC Driver 18 for SQL Server")
    result.setdefault("sql_windows_auth", True)
    result.setdefault("sql_trust_cert", False)
    result.setdefault("sql_user", "")
    result.setdefault("file_paths", "")
    result.setdefault("sql_remove_stage", False)
    result.setdefault("connection_kind", "server")
    result.setdefault("ib_ref", "")
    result.setdefault("one_c_server", "")
    result.setdefault("one_c_file", "")
    result.setdefault("one_c_url", "")
    result.setdefault("one_c_ras", "")
    result.setdefault("one_c_exe", "")
    result.setdefault("platform_mode", "inherit")
    result.setdefault("ftp_profile", "")
    result.setdefault("ftp_targets", [])
    result.setdefault("task_group", "")
    result.setdefault("archive_prefix", "")
    result.setdefault("cluster_user", "")
    result.setdefault("wait_seconds_override", "")
    result.setdefault("force_terminate_override", None)
    result.setdefault("commands_enabled", False)
    result.setdefault("command_before", "")
    result.setdefault("command_after", "")
    result.setdefault("stop_after_enabled", False)
    result.setdefault("stop_after_hours", 24)
    result.setdefault("stop_after_minutes", 0)
    result.setdefault("notify_email", False)
    result.setdefault("notify_telegram", False)
    result.setdefault("notify_max", False)
    result.setdefault("notify_on_success", False)
    result.setdefault("notify_on_failure", False)
    result.setdefault("command_body", "")
    # 1C 8.x maintenance and external processor tasks (no 1C 7.7).
    result.setdefault("check_mode", "test")
    result.setdefault("check_reindex", True)
    result.setdefault("check_logical", True)
    result.setdefault("check_refs", False)
    result.setdefault("check_totals", True)
    result.setdefault("check_compress", False)
    result.setdefault("check_rebuild", False)
    result.setdefault("repair_bad_refs", "none")
    result.setdefault("repair_bad_data", "none")
    result.setdefault("repair_confirmed", False)
    result.setdefault("file_offline_confirmed", False)
    result.setdefault("file_disconnect_mode", "wait_only")
    result.setdefault("file_wait_seconds", 60)
    result.setdefault("file_session_hosts", "")
    result.setdefault("file_base_aliases", "")
    result.setdefault("file_force_confirmed", False)
    result.setdefault("file_web_maintenance_enabled", False)
    result.setdefault("file_iis_maintenance_enabled", False)
    result.setdefault("file_iis_pool_name", "")
    result.setdefault("file_iis_window_start", "23:00")
    result.setdefault("file_iis_window_end", "06:00")
    result.setdefault("check_time_hours", 0)
    result.setdefault("check_time_minutes", 0)
    result.setdefault("procedure_name", "Выполнение обмена")
    result.setdefault("procedure_epf", "")
    result.setdefault("procedure_parameter", "")
    result.setdefault("procedure_exclusive", False)
    result.setdefault("procedure_timeout_hours", 2)
    return result


def load_config() -> dict:
    cfg = deep_copy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in data.items():
            if k != "databases":
                cfg[k] = v
        if isinstance(data.get("databases"), list):
            cfg["databases"] = [normalize_db(x) for x in data["databases"]]
    cfg["databases"] = [normalize_db(x) for x in cfg.get("databases", [])]
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load_scheduler_state() -> dict:
    if not SVC_STATE_PATH.exists():
        return {}
    try:
        return json.loads(SVC_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_scheduler_state(state: dict) -> None:
    tmp = SVC_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SVC_STATE_PATH)


def db_state_key(db: dict) -> str:
    return job_key(db)


def get_db_state(db: dict, state: dict | None = None) -> dict:
    state = state if state is not None else load_scheduler_state()
    entry = dict(state.get(db_state_key(db), {}) or {})

    # Migration from v2/v2.1 state format.
    if not entry.get("last_scheduled_date") and entry.get("last_run_date"):
        entry["last_scheduled_date"] = entry.get("last_run_date")
    if not entry.get("last_attempt_time") and entry.get("last_run_time"):
        entry["last_attempt_time"] = entry.get("last_run_time")
    if not entry.get("last_success_time") and entry.get("last_result") == "ok" and entry.get("last_run_time"):
        entry["last_success_time"] = entry.get("last_run_time")
    return entry


def record_backup_result(db: dict, success: bool, error: str = "", scheduled: bool = False,
                         when: datetime | None = None) -> None:
    now = when or datetime.now()
    state = load_scheduler_state()
    key = db_state_key(db)
    entry = get_db_state(db, state)
    entry["last_attempt_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
    entry["last_result"] = "ok" if success else "error"
    entry["last_error"] = "" if success else str(error)[:500]
    if success:
        entry["last_success_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if scheduled:
        # Mark a scheduled attempt for today even if it failed, so a broken base
        # does not retry every service poll cycle all night.
        entry["last_scheduled_date"] = now.strftime("%Y-%m-%d")
    state[key] = entry
    save_scheduler_state(state)


def format_ui_time(value: str) -> str:
    if not value:
        return "—"
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
    except Exception:
        return value


def version_key(path: Path):
    parts = path.parent.parent.name.split(".")
    try:
        return tuple(int(x) for x in parts)
    except ValueError:
        return (0,)


def autodetect_platform_bin() -> Path:
    """Find latest 1cv8.exe including platform 8.3 and 8.5 releases."""
    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "1cv8",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "1cv8",
    ]
    found = []
    for root in roots:
        if root.exists():
            for exe in root.glob("8.*/bin/1cv8.exe"):
                if re.fullmatch(r"8\.\d+\.\d+\.\d+", exe.parent.parent.name):
                    found.append(exe)
    if not found:
        raise RuntimeError("Не найден 1cv8.exe. Укажите файл платформы вручную.")
    found.sort(key=version_key, reverse=True)
    return found[0].parent


def split_host_port(endpoint: str, default_port: int):
    endpoint = endpoint.strip()
    if ":" in endpoint:
        host, port = endpoint.rsplit(":", 1)
        return host.strip(), int(port)
    return endpoint, default_port


def wait_tcp(endpoint: str, timeout: int = 10) -> bool:
    host, port = split_host_port(endpoint, 1545)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "base"


def parse_rac_records(text: str) -> list[dict]:
    records = []
    current = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip().lower()] = value.strip()
    if current:
        records.append(current)
    return records


def bool_value(value: str) -> bool:
    return str(value).strip().lower() in {"on", "yes", "true", "1", "да"}


def file_sha256(path: Path, chunk_size=8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def looks_like_auth_error(text: str) -> bool:
    s = text.lower()
    patterns = [
        "не подходит логин", "неправиль", "не верн", "неверн",
        "authentication", "auth", "access denied", "login failed", "18456", "28000",
        "пользователь не зарегистрирован", "не удалось аутентифицировать",
        "недостаточно прав", "в доступе отказано",
        "неверный пароль", "имя или пароль", "логин или пароль"
    ]
    return any(p in s for p in patterns)


def tail_text(path: Path, max_chars=8000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[-max_chars:]
        return text
    except Exception:
        return ""


def normalize_time_hhmm(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        h, m = value.split(":")
        h = int(h)
        m = int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    raise RuntimeError(f"Неверное время '{value}'. Используйте формат ЧЧ:ММ, например 02:30.")


def friendly_error_message(exc: Exception, details: str = "") -> str:
    msg = str(exc).strip()
    low = (msg + "\n" + details).lower()
    if looks_like_auth_error(low):
        return "Не подошёл логин и/или пароль базы."
    if "1cv8.exe" in low and ("не найден" in low or "not found" in low):
        return "Не найден 1cv8.exe. Укажите папку bin платформы 1С."
    if "ras" in low and ("unavailable" in low or "не удалось" in low or "could not start" in low):
        return "Не удалось подключиться к RAS. Проверьте адрес RAS, агент кластера и наличие ras.exe."
    if "not found" in low and "infobase" in low:
        return "База не найдена в кластере. Проверьте поле Ref."
    if "cannot decrypt" in low or "расшифровать" in low:
        return "Не удалось расшифровать сохранённые пароли. Запустите программу под тем же Windows-пользователем."
    if "неверное время" in low:
        return msg
    return msg or "Операция завершилась ошибкой. Подробности смотрите в журнале."


class CommandError(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


class BackupEngine:
    def __init__(self, cfg: dict, sec: dict, log_cb=None):
        self.cfg = cfg
        self.sec = sec
        self.log_cb = log_cb or print
        self.ras_process = None
        self.bin_dir = None
        self.onecv8 = None
        self.rac = None
        self.ras = None

    def log(self, msg: str):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
        self.log_cb(line)
        try:
            root = Path(self.cfg.get("backup_root", r"D:\Backup\1C_DT"))
            log_dir = root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / f"manager_{datetime.now():%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def prepare(self):
        configured = str(self.cfg.get("platform_bin", "")).strip()
        if configured:
            p = Path(configured)
            self.bin_dir = p.parent if p.suffix.lower() == ".exe" else p
        else:
            self.bin_dir = autodetect_platform_bin()
        self.onecv8 = self.bin_dir / "1cv8.exe"
        self.rac = self.bin_dir / "rac.exe"
        self.ras = self.bin_dir / "ras.exe"
        for p in (self.onecv8, self.rac, self.ras):
            if not p.exists():
                raise RuntimeError(f"Не найден обязательный файл платформы 1С: {p}")
        self.log(f"1C bin: {self.bin_dir}")
        self.ensure_ras()

    def run(self, args: list[str], timeout=120, allow_error=False):
        self.log("CMD: " + self.mask_command(args))
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        out = (cp.stdout or "") + (("\n" + cp.stderr) if cp.stderr else "")
        if cp.returncode != 0 and not allow_error:
            raise CommandError(f"Command failed, code={cp.returncode}\n{out.strip()}")
        return cp.returncode, out.strip()

    @staticmethod
    def mask_command(args: list[str]) -> str:
        hidden_next = False
        result = []
        for a in args:
            low = a.lower()
            if hidden_next:
                result.append("***")
                hidden_next = False
                continue
            if low in {"--cluster-pwd", "--infobase-pwd"}:
                result.append(a)
                hidden_next = True
            elif low.startswith("--cluster-pwd="):
                result.append("--cluster-pwd=***")
            elif low.startswith("--infobase-pwd="):
                result.append("--infobase-pwd=***")
            elif low.startswith("/p"):
                result.append("/P***")
            else:
                result.append(a)
        return subprocess.list2cmdline(result)

    def ensure_ras(self):
        endpoint = self.cfg.get("ras_endpoint", "127.0.0.1:1545")
        if wait_tcp(endpoint, 2):
            self.log(f"RAS available: {endpoint}")
            return
        host, port = split_host_port(endpoint, 1545)
        if host.lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(
                f"RAS недоступен по адресу {endpoint}. Автозапуск возможен только для локального RAS."
            )
        cluster_agent = self.cfg.get("cluster_agent", f"{self.cfg.get('server','SRV1C')}:1540")
        cmd = [str(self.ras), "cluster", f"--port={port}", cluster_agent]
        self.log("Запуск временного RAS...")
        self.ras_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        if not wait_tcp(endpoint, 10):
            raise RuntimeError(f"Не удалось запустить RAS на {endpoint}.")
        self.log(f"Temporary RAS started at {endpoint}")

    def close(self):
        if self.ras_process and self.ras_process.poll() is None:
            try:
                self.ras_process.terminate()
                self.ras_process.wait(timeout=5)
            except Exception:
                try:
                    self.ras_process.kill()
                except Exception:
                    pass

    def cluster_auth(self) -> list[str]:
        user = str(self.cfg.get("cluster_user", "")).strip()
        pwd = self.sec.get("cluster_password", "")
        result = []
        if user:
            result.append(f"--cluster-user={user}")
            if pwd:
                result.append(f"--cluster-pwd={pwd}")
        return result

    @property
    def ras_endpoint(self):
        return str(self.cfg.get("ras_endpoint", "127.0.0.1:1545"))

    def rac_cmd(self, *parts: str, timeout=120, allow_error=False):
        args = [str(self.rac), self.ras_endpoint, *parts]
        return self.run(args, timeout=timeout, allow_error=allow_error)

    def get_cluster_uuid(self) -> str:
        configured = str(self.cfg.get("cluster_uuid", "")).strip()
        if configured:
            return configured
        _, out = self.rac_cmd("cluster", "list")
        records = parse_rac_records(out)
        ids = [rec.get("cluster") for rec in records if rec.get("cluster")]
        if not ids:
            raise RuntimeError("RAC не вернул UUID кластера.")
        if len(ids) > 1:
            raise RuntimeError("Найдено несколько кластеров. Укажите UUID кластера в настройках.")
        return ids[0]

    def find_infobase(self, cluster_uuid: str, ref: str) -> tuple[str, dict]:
        args = [
            "infobase", "summary", "list",
            f"--cluster={cluster_uuid}",
            *self.cluster_auth(),
        ]
        _, out = self.rac_cmd(*args)
        records = parse_rac_records(out)
        wanted = ref.strip().lower()
        for rec in records:
            if rec.get("name", "").strip().lower() == wanted:
                ibid = rec.get("infobase")
                if ibid:
                    return ibid, rec
        names = ", ".join(r.get("name", "?") for r in records)
        raise RuntimeError(f"Infobase '{ref}' not found. RAC returned: {names}")

    def ib_auth(self, db: dict) -> list[str]:
        user = str(db.get("ib_user", "")).strip()
        pwd = self.sec.get("db_passwords", {}).get(job_key(db), self.sec.get("db_passwords", {}).get(db.get("ref", ""), ""))
        result = []
        if user:
            result.append(f"--infobase-user={user}")
            if pwd:
                result.append(f"--infobase-pwd={pwd}")
        return result

    def get_infobase_info(self, cluster_uuid: str, ib_uuid: str, db: dict) -> dict:
        args = [
            "infobase", "info",
            f"--cluster={cluster_uuid}",
            f"--infobase={ib_uuid}",
            *self.cluster_auth(),
            *self.ib_auth(db),
        ]
        code, out = self.rac_cmd(*args, allow_error=True)
        if code != 0:
            if looks_like_auth_error(out):
                raise AuthError("Не подошёл логин и/или пароль базы.")
            raise RuntimeError(out or "Не удалось получить информацию о базе.")
        records = parse_rac_records(out)
        return records[0] if records else {}

    def update_infobase(self, cluster_uuid: str, ib_uuid: str, db: dict, extra: list[str]):
        args = [
            "infobase", "update",
            f"--cluster={cluster_uuid}",
            f"--infobase={ib_uuid}",
            *self.cluster_auth(),
            *self.ib_auth(db),
            *extra,
        ]
        code, out = self.rac_cmd(*args, allow_error=True)
        if code != 0:
            if looks_like_auth_error(out):
                raise AuthError("Не подошёл логин и/или пароль базы.")
            raise RuntimeError(out or "Не удалось обновить настройки блокировки базы.")

    def list_sessions(self, cluster_uuid: str, ib_uuid: str) -> list[dict]:
        args = [
            "session", "list",
            f"--cluster={cluster_uuid}",
            f"--infobase={ib_uuid}",
            *self.cluster_auth(),
        ]
        _, out = self.rac_cmd(*args)
        return parse_rac_records(out)

    def terminate_session(self, cluster_uuid: str, session_uuid: str):
        args = [
            "session", "terminate",
            f"--cluster={cluster_uuid}",
            f"--session={session_uuid}",
            *self.cluster_auth(),
        ]
        self.rac_cmd(*args)

    def block_infobase(self, cluster_uuid: str, ib_uuid: str, db: dict, permission_code: str):
        message = "Резервное копирование. Подключение временно запрещено."
        self.update_infobase(
            cluster_uuid, ib_uuid, db,
            [
                "--sessions-deny=on",
                "--scheduled-jobs-deny=on",
                f"--denied-message={message}",
                f"--permission-code={permission_code}",
            ],
        )

    def unblock_infobase(self, cluster_uuid: str, ib_uuid: str, db: dict, scheduled_was_denied=False):
        jobs = "on" if scheduled_was_denied else "off"
        self.update_infobase(
            cluster_uuid, ib_uuid, db,
            [
                "--sessions-deny=off",
                f"--scheduled-jobs-deny={jobs}",
            ],
        )

    def wait_and_kick(self, cluster_uuid: str, ib_uuid: str):
        wait_seconds = max(0, int(self.cfg.get("wait_seconds", 120)))
        force = bool(self.cfg.get("force_terminate", True))
        deadline = time.time() + wait_seconds
        last_count = None
        while time.time() < deadline:
            sessions = self.list_sessions(cluster_uuid, ib_uuid)
            count = len(sessions)
            if count == 0:
                self.log("Сеансов больше нет.")
                return
            if count != last_count:
                self.log(f"Активных сеансов: {count}. Ожидание выхода пользователей...")
                last_count = count
            time.sleep(min(5, max(1, deadline - time.time())))
        sessions = self.list_sessions(cluster_uuid, ib_uuid)
        if not sessions:
            return
        if not force:
            raise RuntimeError(f"Остались активные сеансы: {len(sessions)}.")
        self.log(f"Принудительное завершение оставшихся сеансов: {len(sessions)}")
        for rec in sessions:
            sid = rec.get("session")
            if not sid:
                continue
            user = rec.get("user-name", "")
            app_id = rec.get("app-id", "")
            self.log(f"Terminate session {sid} user='{user}' app='{app_id}'")
            self.terminate_session(cluster_uuid, sid)
        deadline = time.time() + 30
        while time.time() < deadline:
            sessions = self.list_sessions(cluster_uuid, ib_uuid)
            if not sessions:
                self.log("Все сеансы завершены.")
                return
            time.sleep(2)
        raise RuntimeError("Некоторые сеансы остались после принудительного завершения.")

    def run_dump(self, db: dict, permission_code: str) -> Path:
        root = Path(self.cfg.get("backup_root", r"D:\Backup\1C_DT"))
        folder_cfg = str(db.get("folder", "")).strip()
        folder = Path(folder_cfg) if folder_cfg else root / safe_name(db.get("ref", db.get("name", "base")))
        folder.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        basename = f"{safe_name(db.get('archive_prefix') or db.get('ref', db.get('name', 'base')))}_{stamp}"
        dt_path = folder / f"{basename}.dt"
        designer_log = folder / f"{basename}.log"

        server = str(self.cfg.get("server", "SRV1C")).strip()
        ref = str(db.get("ib_ref") or db.get("ref", "")).strip()
        conn = f"{server}\\{ref}"

        args = [str(self.onecv8), "DESIGNER", f"/S{conn}"]
        user = str(db.get("ib_user", "")).strip()
        pwd = self.sec.get("db_passwords", {}).get(job_key(db), self.sec.get("db_passwords", {}).get(ref, ""))
        if user:
            args.append(f"/N{user}")
            if pwd:
                args.append(f"/P{pwd}")
        args += [
            f"/UC{permission_code}",
            "/DumpIB", str(dt_path),
            "/Out", str(designer_log),
            "-NoTruncate",
            "/DisableStartupMessages",
            "/DisableStartupDialogs",
        ]

        self.log(f"DumpIB start: {db.get('name')} -> {dt_path}")
        code, out = self.run(args, timeout=limit_seconds(24 * 3600), allow_error=True)

        log_tail = tail_text(designer_log)
        combined = (out or "") + "\n" + log_tail
        if code != 0:
            if looks_like_auth_error(combined):
                raise AuthError("Не подошёл логин и/или пароль базы.")
            raise RuntimeError(f"1cv8.exe вернул код {code}. См. лог: {designer_log}")
        if looks_like_auth_error(combined):
            raise AuthError("Не подошёл логин и/или пароль базы.")
        if not dt_path.exists():
            raise RuntimeError(f"DT-файл не был создан. См. лог: {designer_log}")
        size = dt_path.stat().st_size
        if size <= 0:
            raise RuntimeError(f"DT-файл пустой: {dt_path}")
        self.log(f"DT created: {size / 1024 / 1024:.2f} MB")

        self.log("Расчёт SHA256...")
        digest = file_sha256(dt_path)
        hash_path = Path(str(dt_path) + ".sha256")
        hash_path.write_text(f"{digest}  {dt_path.name}\n", encoding="ascii")
        self.log(f"SHA256: {digest}")

        # v4: retention is handled after optional ZIP/7z and FTP delivery.
        return dt_path

    def apply_retention(self, db: dict, folder: Path):
        keep = int(db.get("keep") or self.cfg.get("default_keep", 7))
        keep = max(1, keep)
        files = sorted(
            folder.glob(f"{safe_name(db.get('archive_prefix') or db.get('ref', db.get('name', 'base')))}_*.dt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[keep:]:
            self.log(f"Delete old backup: {old}")
            try:
                old.unlink()
            except FileNotFoundError:
                pass
            for side in [Path(str(old) + ".sha256"), old.with_suffix(".log")]:
                try:
                    side.unlink()
                except FileNotFoundError:
                    pass

    def backup_one(self, db: dict) -> Path:
        if not db.get("enabled", True):
            raise RuntimeError(f"База {db.get('name')} отключена.")
        cluster_uuid = self.get_cluster_uuid()
        ib_uuid, _ = self.find_infobase(cluster_uuid, str(db.get("ib_ref") or db.get("ref", "")))
        info = self.get_infobase_info(cluster_uuid, ib_uuid, db)

        sessions_were_denied = bool_value(info.get("sessions-deny", "off"))
        scheduled_was_denied = bool_value(info.get("scheduled-jobs-deny", "off"))

        if sessions_were_denied:
            raise RuntimeError(
                f"База {db.get('name')} уже была заблокирована до старта backup. "
                "Для безопасности программа не меняет уже существующую блокировку."
            )

        permission_code = "DT_" + secrets.token_hex(12)
        blocked_by_us = False
        try:
            self.log(f"=== BACKUP START: {db.get('name')} ({db.get('ref')}) ===")
            self.log("Запрет новых сеансов и регламентных заданий...")
            self.block_infobase(cluster_uuid, ib_uuid, db, permission_code)
            blocked_by_us = True

            self.wait_and_kick(cluster_uuid, ib_uuid)
            result = self.run_dump(db, permission_code)
            self.log(f"=== BACKUP SUCCESS: {result} ===")
            return result
        finally:
            if blocked_by_us:
                self.log("Снятие блокировки обслуживания...")
                self.unblock_infobase(cluster_uuid, ib_uuid, db, scheduled_was_denied=scheduled_was_denied)
                self.log("Блокировка обслуживания снята.")

    def emergency_unblock(self, db: dict):
        cluster_uuid = self.get_cluster_uuid()
        ib_uuid, _ = self.find_infobase(cluster_uuid, str(db.get("ib_ref") or db.get("ref", "")))
        self.log(f"Emergency unblock: {db.get('name')}")
        self.update_infobase(
            cluster_uuid, ib_uuid, db,
            ["--sessions-deny=off", "--scheduled-jobs-deny=off"],
        )
        self.log(f"Emergency unblock complete: {db.get('name')}")

    def test(self):
        self.prepare()
        try:
            cluster = self.get_cluster_uuid()
            self.log(f"Cluster UUID: {cluster}")
            for db in self.cfg.get("databases", []):
                if not db.get("enabled", True):
                    continue
                ib_uuid, _ = self.find_infobase(cluster, db.get("ref", ""))
                self.log(f"{db.get('name')}: infobase UUID={ib_uuid}")
                info = self.get_infobase_info(cluster, ib_uuid, db)
                self.log(
                    f"{db.get('name')}: sessions-deny={info.get('sessions-deny','?')}, "
                    f"scheduled-jobs-deny={info.get('scheduled-jobs-deny','?')}"
                )
        finally:
            self.close()


def _file_1c_log(path):
    """Decode 1C diagnostic log, which may use cp1251 on Windows."""
    try:
        payload = Path(path).read_bytes()
    except OSError:
        return ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig", errors="replace")
    for codec in ("utf-8", "cp1251", "cp866"):
        try:
            return payload.decode(codec)
        except UnicodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _is_file_1c_exclusive_error(message):
    t = str(message or "").casefold()
    return any(word in t for word in (
        "исключительной блокировки", "исключительная блокировка", "активны сеансы",
        "exclusive lock", "exclusive access", "infobase is locked", "database is in use"))


def run_file_1c_backup(cfg: dict, sec: dict, job: dict, log=print) -> Path:
    """File-based DumpIB, optionally evicting positively identified Windows clients."""
    from file_session_control import validate_file_disconnect_settings, evict_targeted_clients
    from file_lock_diagnostics import diagnose_file_1c_lock
    mode, grace_seconds, _ = validate_file_disconnect_settings(job)
    folder = make_target_folder(cfg, job)
    base = Path(str(job.get("one_c_file", "")).strip())
    if not base.is_dir():
        raise RuntimeError(f"Каталог файловой базы 1С не найден: {base}")
    if not (base / "1Cv8.1CD").is_file():
        raise RuntimeError(f"В каталоге нет файла 1Cv8.1CD: {base}")
    supplied = str(cfg.get("platform_bin", "")).strip()
    bin_dir = Path(supplied) if supplied else autodetect_platform_bin()
    if bin_dir.suffix.lower() == ".exe":
        bin_dir = bin_dir.parent
    exe = bin_dir / "1cv8.exe"
    if not exe.exists():
        raise RuntimeError(f"Не найден исполняемый файл Конфигуратора: {exe}")
    from backup_extensions import stamp
    dest = folder / f"{safe_component(job.get('archive_prefix') or job.get('ref') or job.get('name'))}_{stamp()}.dt"
    outlog = dest.with_suffix(".log")
    args = [str(exe), "DESIGNER", f"/F{base}"]
    user = str(job.get("ib_user", "")).strip()
    pwd = sec.get("db_passwords", {}).get(job_key(job), sec.get("db_passwords", {}).get(job.get("ref", ""), ""))
    if user:
        args.append(f"/N{user}")
        if pwd:
            args.append(f"/P{pwd}")
    args += ["/DumpIB", str(dest), "/Out", str(outlog), "-NoTruncate",
             "/DisableStartupMessages", "/DisableStartupDialogs"]
    def dump_attempt(index, actual_base=None):
        source = actual_base or base
        log(f"Файловая 1С: запуск DumpIB (попытка {index}) для {source}")
        actual_args = list(args)
        actual_args[2] = f"/F{source}"
        result = subprocess.run(actual_args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", creationflags=CREATE_NO_WINDOW,
                                timeout=limit_seconds(24 * 3600))
        success = result.returncode == 0 and dest.exists() and dest.stat().st_size > 0
        details = _file_1c_log(outlog) or (result.stdout or "") + "\n" + (result.stderr or "")
        if not success:
            # Do not leave a partial .dt that users might mistake for a backup.
            if dest.exists():
                try:
                    dest.unlink()
                except OSError as exc:
                    log(f"Предупреждение: не удалось удалить неполную копию {dest}: {exc}")
            if details.strip():
                log("Сообщение 1С: " + details.strip()[-1500:])
        return success, result.returncode, details

    success, code, details = dump_attempt(1)
    if success:
        log(f"Файловая 1С: создан {dest.name}")
        return dest
    if not _is_file_1c_exclusive_error(details):
        raise RuntimeError(f"Выгрузка файловой 1С завершилась с кодом {code}: "
                           f"{details.strip()[-1200:]}. Журнал: {outlog}")

    log(f"Файловая база занята. Ожидание завершения работы: {grace_seconds} сек.")
    diagnose_file_1c_lock(job, log=log)
    if grace_seconds:
        time.sleep(grace_seconds)
    success, code, details = dump_attempt(2)
    if success:
        log(f"Файловая 1С: создан {dest.name}")
        return dest
    if not _is_file_1c_exclusive_error(details):
        raise RuntimeError(f"Выгрузка файловой 1С завершилась с кодом {code}: "
                           f"{details.strip()[-1200:]}. Журнал: {outlog}")
    if mode == "wait_only":
        raise RuntimeError(f"Файловая база осталась занята после {grace_seconds} сек. "
                           f"Включите отключение пользователей в настройках задания. {outlog}")

    if job.get("file_web_maintenance_enabled"):
        # The checkbox grants permission to temporarily stop verified web
        # hosts. No pool/service name or hours are required in the job.
        # Nothing is stopped unless default.vrd matches this exact file base.
        import tempfile
        from web_maintenance import web_targets_for_base, stage_with_web_pause
        pools, services, _ = web_targets_for_base(str(base))
        if pools or services:
            with tempfile.TemporaryDirectory(prefix="1cbm_stage_", dir=str(folder)) as stage:
                stage_with_web_pause(job, Path(stage), log=log)
                success, code, details = dump_attempt(3, actual_base=Path(stage))
                if not success:
                    raise RuntimeError(f"DumpIB из временной копии завершился с кодом {code}: "
                                       f"{details.strip()[-1200:]}. Журнал: {outlog}")
            log(f"Файловая 1С: создан {dest.name}; IIS/Apache восстановлены")
            return dest
        log("Файловая 1С: подходящие публикации IIS/Apache не найдены; "
            "проверяем локальные и известные удалённые процессы 1С")

    terminated = evict_targeted_clients(job, log=log)
    log(f"Завершено клиентских процессов 1С: {terminated}. Повтор DumpIB.")
    time.sleep(3)
    success, code, details = dump_attempt(3)
    if not success:
        raise RuntimeError(f"Выгрузка после отключения сеансов не удалась (код {code}): "
                           f"{details.strip()[-1200:]}. Журнал: {outlog}")
    log(f"Файловая 1С: создан {dest.name}")
    return dest


def _run_core_backup(cfg: dict, sec: dict, job: dict, log=print) -> Path:
    """Single orchestrator used by service, CLI and GUI.

    Heavy packaging/upload only begins AFTER the 1C exclusive-access block
    has been released by backup_one's finally clause.
    """
    kind = job_type(job)
    if kind == "commands":
        run_commands(job.get("command_body", ""), "Произвольные команды", job, log)
        return None
    if kind == "report":
        from report_delivery import send_summary_task
        return send_summary_task(cfg, sec, job, load_scheduler_state(), log)
    if kind in {"1c_check", "1c_procedure"}:
        # Maintenance tasks use the same global lock from run_any_backup.
        # Unlike DumpIB, a test/repair task produces a log, and a procedure
        # requires a supplied external .epf for configuration-specific logic.
        from onec_tasks_8 import run_1c_check, run_1c_procedure
        if kind == "1c_check":
            return run_1c_check(cfg, sec, job, sys.modules[__name__], log)
        return run_1c_procedure(cfg, sec, job, sys.modules[__name__], log)
    if job.get("ftp_enabled") and not next((p for p in cfg.get("ftp_profiles", []) if p.get("name") == job.get("ftp_profile")), cfg.get("ftp", {})).get("tls", True):
        log("ПРЕДУПРЕЖДЕНИЕ: используется FTP без TLS; пароль передаётся без шифрования")
    with (nullcontext() if cfg.get("_already_locked") else global_backup_lock(Path(cfg["backup_root"]))):
        if kind == "1c":
            conn_kind = job.get("connection_kind", "server")
            if conn_kind == "web":
                raise RuntimeError("Веб-подключение Ws не поддерживает DumpIB через rac. Нужен доступ к серверу 1С.")
            effective = dict(cfg)
            mode = job.get("platform_mode", "inherit")
            if mode == "auto":
                # Explicit auto overrides an old globally pinned platform path.
                effective["platform_bin"] = ""
            manual_exe = str(job.get("one_c_exe", "")).strip()
            if mode == "manual":
                if Path(manual_exe).name.casefold() != "1cv8.exe":
                    raise RuntimeError("Для пакетной выгрузки .dt нужен 1cv8.exe, не 1cestart.exe")
                effective["platform_bin"] = str(Path(manual_exe).parent)
            if conn_kind == "file":
                raw_path = run_file_1c_backup(effective, sec, job, log)
                return finalize_backup(job, raw_path, cfg, sec, log)
            server = str(job.get("one_c_server", "")).strip()
            if server:
                effective["server"] = server
                old_server = str(cfg.get("server", "")).strip()
                if server.casefold() != old_server.casefold():
                    # New installations do not need a global "SRV1C" placeholder.
                    # A local RAS is started on demand against the server agent.
                    # Existing installations with a saved server retain their
                    # previous RAS behavior for backwards compatibility.
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
                effective["cluster_user"] = str(job["cluster_user"]).strip()
                sec_for_job["cluster_password"] = sec.get("cluster_job_passwords", {}).get(
                    job_key(job), sec.get("cluster_password", ""))
            engine = BackupEngine(effective, sec_for_job, log)
            try:
                engine.prepare()
                raw_path = engine.backup_one(job)
            finally:
                engine.close()
            return finalize_backup(job, raw_path, cfg, sec, log)
        if kind == "mssql":
            raw_path = backup_mssql(job, sec, cfg, log)
            dest_folder = make_target_folder(cfg, job)
            destination = dest_folder / raw_path.name
            if raw_path.resolve() != destination.resolve():
                if destination.exists():
                    raise RuntimeError(f"Локальный SQL-бэкап уже существует: {destination}")
                stage = destination.with_name(destination.name + ".copying")
                try:
                    shutil.copy2(raw_path, stage)
                    if sha256_file(stage) != sha256_file(raw_path):
                        raise RuntimeError("Контрольная сумма при копировании SQL .bak не совпала")
                    os.replace(stage, destination)
                finally:
                    stage.unlink(missing_ok=True)
                log(f"SQL: копия .bak сохранена в {destination}")
                if job.get("sql_remove_stage", False):
                    raw_path.unlink(missing_ok=True)
                    log("SQL: исходный .bak удалён после проверенного копирования")
            else:
                destination = raw_path
            return finalize_backup(job, destination, cfg, sec, log)
        if kind in {"files", "extra_copy"}:
            return backup_files(job, cfg, sec, log)
        raise RuntimeError(f"Неизвестный тип задания: {kind}")



def run_any_backup(cfg: dict, sec: dict, job: dict, log=print) -> Path:
    """Run optional pre/post commands once, under the same global task lock.

    If the backup or the pre-command fails, the post-command still executes,
    allowing e.g. external services to be resumed. Original errors are retained.
    """
    with global_backup_lock(Path(cfg["backup_root"])):
        with task_deadline(job):
            primary_error = None
            result = None
            try:
                if job.get("commands_enabled", False):
                    run_commands(job.get("command_before", ""), "Перед заданием", job, log)
                effective = dict(cfg, _already_locked=True)
                result = _run_core_backup(effective, sec, job, log)
            except Exception as exc:
                primary_error = exc
            # Post-command runs after RAC/DumpIB's finally unblocks the base.
            try:
                if job.get("commands_enabled", False):
                    run_commands(job.get("command_after", ""), "После задания", job, log,
                                 status="failed" if primary_error else "success", ignore_deadline=True)
            except Exception as post_exc:
                if primary_error is None:
                    raise
                log(f"Дополнительная ошибка команды после задания: {post_exc}")
            if primary_error:
                raise primary_error
            return result


def execute_job(cfg: dict, sec: dict, job: dict, log=print, scheduled=False):
    """One execution path for GUI/service, with durable per-run journal."""
    from report_delivery import notify_task_result
    from run_journal import begin_task_run

    history = None
    try:
        history = begin_task_run(job, scheduled=scheduled)
    except OSError as exc:
        log(f"Предупреждение: журнал запусков недоступен: {exc}")

    def task_log(line):
        if history is not None:
            try:
                history.log(line)
            except OSError:
                pass
        log(line)

    try:
        artifact = run_any_backup(cfg, sec, job, task_log)
    except Exception as exc:
        msg = friendly_error_message(exc, str(exc))
        task_log("ОШИБКА: " + msg)
        if history is not None:
            try:
                history.log(traceback.format_exc())
            except OSError:
                pass
        try:
            record_backup_result(job, False, error=msg, scheduled=scheduled)
            try:
                notify_task_result(cfg, sec, job, False, error=msg, log=task_log)
            except Exception:
                task_log("Предупреждение: не удалось отправить отчёт об ошибке")
        finally:
            if history is not None:
                try:
                    history.finish(False, message=msg)
                except OSError:
                    pass
        raise
    try:
        record_backup_result(job, True, scheduled=scheduled)
        try:
            notify_task_result(cfg, sec, job, True, artifact=artifact, log=task_log)
        except Exception:
            task_log("Предупреждение: не удалось отправить уведомление об успешной копии")
    finally:
        if history is not None:
            try:
                history.finish(True, message="Успешное выполнение задания", artifact=artifact)
            except OSError:
                pass
    return artifact


def retry_ftp_last(cfg: dict, sec: dict, job: dict, log=print) -> Path:
    """Re-upload newest finished local backup without repeating source backup."""
    with global_backup_lock(Path(cfg["backup_root"])):
        folder = make_target_folder(cfg, job)
        prefix = safe_component(job.get("archive_prefix") or job.get("ref") or job.get("name")) + "_"
        choices = [p for p in folder.iterdir()
                   if p.is_file() and p.name.startswith(prefix)
                   and p.suffix.lower() in {".dt", ".bak", ".zip", ".7z"}]
        if not choices:
            raise RuntimeError("Нет готового локального архива для FTP")
        latest = max(choices, key=lambda p: p.stat().st_mtime)
        from backup_extensions import ftp_targets_for_job
        for ftp_cfg, ftp_pwd in ftp_targets_for_job(cfg, sec, job):
            ftp_upload(latest, ftp_cfg, ftp_pwd, log)
        latest.with_name(latest.name + ".pending-ftp").unlink(missing_ok=True)
        return latest


def service_log(msg: str):
    raw = str(msg or "")
    # BackupEngine already prepends a timestamp for GUI/file logging. When its
    # callback is service_log, remove that prefix so the service log gets only
    # one timestamp.
    raw = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s{2}", "", raw)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  [SERVICE] {raw}"
    try:
        root = load_config().get("backup_root", r"D:\Backup\1C_DT")
        log_dir = Path(root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"service_{datetime.now():%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        try:
            if SERVICE_AVAILABLE:
                servicemanager.LogErrorMsg(line)
        except Exception:
            pass


def should_run_now(db: dict, state: dict, now: datetime) -> bool:
    """Run once today when scheduled time has arrived.

    There is intentionally no short time window. If an earlier database takes
    longer than expected or the service was temporarily stopped, an overdue
    database is started at the first available opportunity on the same day.
    """
    if not db.get("enabled", True):
        return False
    sch = normalize_time_hhmm(db.get("schedule_time", ""))
    if not sch:
        return False

    hh, mm = sch.split(":")
    target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if now < target:
        return False

    entry = get_db_state(db, state)
    return entry.get("last_scheduled_date", "") != now.strftime("%Y-%m-%d")


def prune_manager_logs(cfg: dict, log=print, now=None) -> int:
    """Delete only application service/manager log files older than configured days.

    This intentionally never touches backup artifacts, 1C designer logs, or the
    scheduler state JSON. 0 disables automatic deletion.
    """
    from datetime import timedelta
    days = max(0, int(cfg.get("journal_retention_days", 60) or 0))
    if days == 0:
        return 0
    limit = (now or datetime.now()) - timedelta(days=days)
    base = Path(cfg.get("backup_root") or "") / "logs"
    if not base.is_dir():
        return 0
    removed = 0
    for pattern in ("manager_*.log", "service_*.log"):
        for file in base.glob(pattern):
            if file.is_file() and datetime.fromtimestamp(file.stat().st_mtime) < limit:
                try:
                    file.unlink()
                    removed += 1
                except OSError as exc:
                    log(f"Журнал: не удалось удалить старый лог {file.name}: {exc}")
    return removed


def scheduler_loop(stop_checker, logger=service_log):
    logger("Scheduler loop started.")
    last_log_cleanup = None
    while not stop_checker():
        try:
            cfg = load_config()
            sec = load_secrets()
            state = load_scheduler_state()
            now = datetime.now()
            if last_log_cleanup != now.date():
                prune_manager_logs(cfg, logger, now)
                try:
                    from run_journal import prune_task_runs
                    prune_task_runs(cfg.get("journal_retention_days", 60), now=now)
                except OSError as exc:
                    logger(f"Предупреждение: очистка журнала запусков не выполнена: {exc}")
                last_log_cleanup = now.date()

            # Sort by configured time so overdue databases are processed in the
            # intended nightly order.
            due = []
            for db in cfg.get("databases", []):
                try:
                    if should_run_now(db, state, now):
                        due.append(db)
                except Exception as exc:
                    logger(f"Schedule error for {db.get('name')}: {exc}")
            due.sort(key=lambda x: normalize_time_hhmm(x.get("schedule_time", "")) or "99:99")

            for db in due:
                try:
                    logger(f"Scheduled backup start: {db.get('name')} at {db.get('schedule_time')}")
                    execute_job(cfg, sec, db, logger, scheduled=True)
                    logger(f"Scheduled backup success: {db.get('name')}")
                except Exception as exc:
                    logger(f"Scheduled backup error for {db.get('name')}: {exc}")

                # Reload state after each database because it was changed above.
                state = load_scheduler_state()

        except Exception as exc:
            logger(f"Scheduler loop outer error: {exc}")

        sleep_sec = max(10, int(load_config().get("service_poll_seconds", 30)))
        for _ in range(sleep_sec):
            if stop_checker():
                break
            time.sleep(1)
    logger("Scheduler loop stopped.")


if SERVICE_AVAILABLE:
    class OneCBackupService(win32serviceutil.ServiceFramework):
        _svc_name_ = "OneCBackupManager"
        _svc_display_name_ = "1C Backup Manager Service"
        _svc_description_ = "Schedules and runs 1C DT backups according to config.json"

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            self.stop_requested = False

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_requested = True
            win32event.SetEvent(self.hWaitStop)

        def SvcDoRun(self):
            servicemanager.LogInfoMsg("1C Backup Manager Service started")
            try:
                scheduler_loop(lambda: self.stop_requested, service_log)
            except Exception:
                servicemanager.LogErrorMsg(traceback.format_exc())
            finally:
                servicemanager.LogInfoMsg("1C Backup Manager Service stopped")


# v4 GUI replaces only the presentation layer of v3.
class App(tk.Tk):
    TYPE_LABELS = {"1c": "1С:Предприятие", "mssql": "Microsoft SQL Server", "files": "Файлы и папки"}

    def __init__(self):
        super().__init__()
        self.title(f"1C Backup Manager v{APP_VERSION} — резервное копирование")
        self.geometry("1420x875")
        self.minsize(1070, 680)
        self.configure(background="#eef2f7")
        self.configure_style()
        self.q = queue.Queue()
        self.worker = None
        self.config_data = load_config()
        try:
            self.sec = load_secrets()
        except Exception as exc:
            self.secrets_read_error = str(exc)
            messagebox.showerror("Пароли", "Не удалось расшифровать secrets.dat.\n"
                "Запустите программу под исходной учётной записью Windows.\n"
                "Сохранение отключено, чтобы не затереть существующие пароли.")
            self.sec = {"cluster_password": "", "db_passwords": {},
                        "sql_passwords": {}, "ftp_password": ""}
        if not hasattr(self, "secrets_read_error"):
            self.secrets_read_error = None
        self.jobs = [normalize_db(x) for x in self.config_data.get("databases", [])]
        self.selected = None
        self.selection_change = False
        self.service_status = tk.StringVar(value="Проверка службы...")
        self.summary = tk.StringVar(value="")
        self._make_vars()
        self._build_ui()
        self._fill_tree()
        self.after(300, self._poll)
        self.after(3000, self._periodic)
        self.refresh_view()

    def configure_style(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("Small.TLabel", font=("Segoe UI", 9))
        style.configure("Job.Treeview", rowheight=30, font=("Segoe UI", 10))
        style.configure("Job.Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("Page.TFrame", padding=12)
        style.configure("Section.TLabelframe", padding=10)
        style.configure("Primary.TButton", padding=(12, 6))

    def _make_vars(self):
        def S(value=""): return tk.StringVar(value=str(value))
        def B(value=False): return tk.BooleanVar(value=bool(value))
        self.v = {}
        fields = ["name", "ref", "ib_user", "sql_server", "sql_database", "sql_backup_dir",
                  "sql_driver", "sql_user", "folder", "keep", "hour", "minute"]
        for key in fields: self.v[key] = S()
        self.v["ib_password"] = S()
        self.v["sql_password"] = S()
        self.v["enabled"] = B(True)
        self.v["ftp_enabled"] = B(False)
        self.v["archive_format"] = S("none")
        self.v["remove_raw_after_pack"] = B(False)
        self.v["sql_remove_stage"] = B(False)
        self.v["sql_windows_auth"] = B(True)
        self.v["sql_trust_cert"] = B(False)
        cfg = self.config_data
        self.g = {k: S(cfg.get(k, "")) for k in
                  ["server", "ras_endpoint", "cluster_agent", "cluster_uuid", "cluster_user",
                   "platform_bin", "backup_root", "wait_seconds", "service_poll_seconds"]}
        self.g["cluster_password"] = S(self.sec.get("cluster_password", ""))
        self.g["force_terminate"] = B(cfg.get("force_terminate", True))
        ftp = cfg.get("ftp", {})
        self.ftp = {k: S(ftp.get(k, "")) for k in ["host", "port", "user", "path"]}
        self.ftp["tls"] = B(ftp.get("tls", True))
        self.ftp["password"] = S(self.sec.get("ftp_password", ""))

    def _build_ui(self):
        header = ttk.Frame(self, padding=(14, 11))
        header.pack(fill="x")
        ttk.Label(header, text="Резервное копирование", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"   v{APP_VERSION}  •  1С / MS SQL / файлы", style="Small.TLabel").pack(side="left", padx=10)
        ttk.Button(header, text="Сохранить настройки", command=self.save, style="Primary.TButton").pack(side="right", padx=3)
        ttk.Button(header, text="▶ Выполнить", command=self.run_selected, style="Primary.TButton").pack(side="right", padx=3)
        ttk.Button(header, text="▶ Все активные", command=self.run_all).pack(side="right", padx=3)

        topbar = ttk.Frame(self, padding=(14, 0, 14, 8))
        topbar.pack(fill="x")
        ttk.Label(topbar, textvariable=self.summary).pack(side="left")
        ttk.Label(topbar, text="Служба:").pack(side="right", padx=(8, 2))
        ttk.Label(topbar, textvariable=self.service_status).pack(side="right")

        splitter = ttk.Panedwindow(self, orient="horizontal")
        splitter.pack(fill="both", expand=True, padx=12, pady=4)
        left = ttk.Frame(splitter, padding=(8, 8))
        right = ttk.Frame(splitter, padding=(8, 8))
        splitter.add(left, weight=2)
        splitter.add(right, weight=5)

        tools = ttk.Frame(left)
        tools.pack(fill="x", pady=(0, 8))
        ttk.Label(tools, text="Задания", font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(tools, text="＋ Добавить", command=self.add_job_menu).pack(side="right", padx=2)
        ttk.Button(tools, text="Удалить", command=self.delete_job).pack(side="right", padx=2)
        ttk.Button(tools, text="Копия", command=self.clone_job).pack(side="right", padx=2)

        treeframe = ttk.Frame(left)
        treeframe.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(treeframe, columns=("time", "last", "status"),
                                 displaycolumns=("time", "last", "status"), show="tree headings",
                                 selectmode="browse", style="Job.Treeview")
        self.tree.heading("#0", text="Название / тип")
        self.tree.heading("time", text="Запуск")
        self.tree.heading("last", text="Последний")
        self.tree.heading("status", text="Результат")
        self.tree.column("#0", width=190, stretch=True)
        self.tree.column("time", width=66, stretch=False, anchor="center")
        self.tree.column("last", width=103, stretch=False, anchor="center")
        self.tree.column("status", width=84, stretch=False, anchor="center")
        scroll = ttk.Scrollbar(treeframe, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.tag_configure("fail", foreground="#a12424")
        self.tree.tag_configure("success", foreground="#166534")
        self.tree.tag_configure("disabled", foreground="#8795a7")

        footerleft = ttk.Frame(left, padding=(2, 8))
        footerleft.pack(fill="x")
        ttk.Button(footerleft, text="⇧ FTP: повторить отправку", command=self.retry_ftp).pack(fill="x", pady=3)
        ttk.Button(footerleft, text="Проверить выбранное задание", command=self.test_selected).pack(fill="x", pady=3)
        ttk.Label(footerleft, text="Контакт: Telegram @dek76", foreground="#1674b8", cursor="hand2").pack(anchor="w", pady=8)
        footerleft.winfo_children()[-1].bind("<Button-1>", lambda e: webbrowser.open("https://t.me/dek76"))

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.source_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        self.archive_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        self.ftp_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        self.schedule_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        self.system_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        for title, page in [
            ("Источник", self.source_tab), ("Архив и хранение", self.archive_tab),
            ("FTP / FTPS", self.ftp_tab), ("Расписание", self.schedule_tab),
            ("Система и служба", self.system_tab),
        ]:
            self.tabs.add(page, text=title)
        self._build_source_tab()
        self._build_archive_tab()
        self._build_ftp_tab()
        self._build_schedule_tab()
        self._build_system_tab()

        log_frame = ttk.LabelFrame(self, text="Журнал операций", padding=6)
        log_frame.pack(fill="x", padx=12, pady=(0, 9))
        self.logbox = tk.Text(log_frame, height=8, wrap="word", font=("Consolas", 9), bg="#f8fafc")
        self.logbox.pack(fill="x")
        self.logbox.configure(state="disabled")

    def _line(self, parent, label, var, row, *, width=45, password=False, col=0):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=5, pady=7)
        entry = ttk.Entry(parent, textvariable=var, width=width, show="*" if password else "")
        entry.grid(row=row, column=col+1, sticky="ew", padx=5, pady=7)
        parent.columnconfigure(col+1, weight=1)
        return entry

    def _build_source_tab(self):
        self._line(self.source_tab, "Название задания", self.v["name"], 0)
        self._line(self.source_tab, "Идентификатор / Ref", self.v["ref"], 1)
        ttk.Checkbutton(self.source_tab, text="Задание активно", variable=self.v["enabled"]).grid(
            row=2, column=1, sticky="w", padx=5, pady=7)
        self.type_info = ttk.Label(self.source_tab, text="", foreground="#62748a")
        self.type_info.grid(row=3, column=0, columnspan=2, sticky="w", padx=5, pady=(3, 14))

        self.src_frames = {}
        for kind in ("1c", "mssql", "files"):
            self.src_frames[kind] = ttk.LabelFrame(self.source_tab, text=self.TYPE_LABELS[kind], padding=12)
            self.src_frames[kind].grid(row=4, column=0, columnspan=2, sticky="nsew", padx=4, pady=3)

        c = self.src_frames["1c"]
        self._line(c, "Администратор базы 1С", self.v["ib_user"], 0)
        self._line(c, "Пароль 1С", self.v["ib_password"], 1, password=True)
        ttk.Label(c, text="Сервер и RAS задаются на вкладке «Система и служба».", foreground="#62748a").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=8)

        c = self.src_frames["mssql"]
        self._line(c, "Сервер SQL (server\\instance)", self.v["sql_server"], 0)
        self._line(c, "Имя базы SQL", self.v["sql_database"], 1)
        self._line(c, "Каталог .bak на SQL-сервере", self.v["sql_backup_dir"], 2)
        self._line(c, "ODBC драйвер", self.v["sql_driver"], 3)
        ttk.Checkbutton(c, text="Windows-аутентификация (учётная запись службы)",
                        variable=self.v["sql_windows_auth"]).grid(row=4, column=0, columnspan=2, sticky="w", pady=5)
        self._line(c, "Логин SQL", self.v["sql_user"], 5)
        self._line(c, "Пароль SQL", self.v["sql_password"], 6, password=True)
        ttk.Checkbutton(c, text="Доверять сертификату SQL-сервера (только для доверенных сетей)",
                        variable=self.v["sql_trust_cert"]).grid(row=7, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Checkbutton(c, text="Удалять промежуточный .bak после копирования в хранилище",
                        variable=self.v["sql_remove_stage"]).grid(row=8, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(c, text="SQL Server создаёт .bak от своего имени. Его службе нужны права записи в указанный каталог.\n"
                          "При удалённом SQL-сервере используйте UNC-путь, доступный обеим службам.",
                  foreground="#62748a", justify="left").grid(row=9, column=0, columnspan=2, sticky="w", pady=8)

        c = self.src_frames["files"]
        ttk.Label(c, text="Источники (один путь к файлу или папке в каждой строке):").pack(anchor="w", pady=6)
        self.filebox = tk.Text(c, height=10, width=65, font=("Consolas", 10), wrap="none")
        self.filebox.pack(fill="both", expand=True)
        actions = ttk.Frame(c)
        actions.pack(fill="x", pady=5)
        ttk.Button(actions, text="＋ Файл", command=self.add_file_path).pack(side="left", padx=3)
        ttk.Button(actions, text="＋ Папка", command=self.add_directory_path).pack(side="left", padx=3)
        ttk.Label(c, text="Полный бэкап; открытые файлы не копируются через VSS в этой версии.",
                  foreground="#62748a").pack(anchor="w", pady=8)

    def _build_archive_tab(self):
        group = ttk.LabelFrame(self.archive_tab, text="Локальное хранение", padding=12)
        group.pack(fill="x", pady=6)
        self._line(group, "Папка (пусто = автоматически)", self.v["folder"], 0)
        ttk.Button(group, text="Выбрать папку...", command=self.choose_job_folder).grid(
            row=1, column=1, sticky="w", padx=5, pady=4)
        self._line(group, "Сколько копий хранить", self.v["keep"], 2, width=10)
        ttk.Label(group, text="Упаковка").grid(row=3, column=0, sticky="w", padx=5, pady=7)
        self.format_box = ttk.Combobox(group, textvariable=self.v["archive_format"],
                                       values=["none", "zip", "7z"], state="readonly", width=12)
        self.format_box.grid(row=3, column=1, sticky="w", padx=5, pady=7)
        ttk.Label(group, text="none — .dt / .bak без упаковки; zip — обычный ZIP; 7z — архив 7-Zip.",
                  foreground="#62748a").grid(row=4, column=0, columnspan=2, sticky="w", padx=5, pady=6)
        ttk.Checkbutton(group, text="После успешной упаковки/передачи удалять исходный .dt / .bak",
                        variable=self.v["remove_raw_after_pack"]).grid(row=5, column=0, columnspan=2, sticky="w", padx=5, pady=6)
        ttk.Label(group, text="Защита: старые локальные копии удаляются только после успешного выполнения\n"
                  "всей задачи, включая передачу на FTP. Неудачная передача не удаляет локальный архив.",
                  foreground="#62748a").grid(row=6, column=0, columnspan=2, sticky="w", padx=5, pady=10)

    def _build_ftp_tab(self):
        ttk.Checkbutton(self.ftp_tab, text="Отправлять архив этого задания на сервер FTP/FTPS",
                        variable=self.v["ftp_enabled"]).pack(anchor="w", pady=(4, 12))
        group = ttk.LabelFrame(self.ftp_tab, text="Общее FTP-хранилище для всех заданий", padding=10)
        group.pack(fill="x")
        self._line(group, "Хост", self.ftp["host"], 0)
        self._line(group, "Порт", self.ftp["port"], 1, width=10)
        self._line(group, "Пользователь", self.ftp["user"], 2)
        self._line(group, "Пароль", self.ftp["password"], 3, password=True)
        self._line(group, "Каталог на сервере", self.ftp["path"], 4)
        ttk.Checkbutton(group, text="FTPS: шифровать соединение и передаваемые данные (рекомендуется)",
                        variable=self.ftp["tls"]).grid(row=5, column=0, columnspan=2, sticky="w", padx=5, pady=7)
        ttk.Button(group, text="Проверить FTP / FTPS", command=self.test_ftp).grid(
            row=6, column=1, sticky="w", pady=8)
        ttk.Label(group, text="Передача выполняется во временное имя *.uploading, затем переименовывается.\n"
                              "Обычный FTP без TLS передаёт пароль открытым текстом. SFTP здесь не поддерживается.",
                  foreground="#62748a", justify="left").grid(row=7, column=0, columnspan=2, sticky="w", pady=6)

    def _build_schedule_tab(self):
        group = ttk.LabelFrame(self.schedule_tab, text="Ежедневно, по времени Windows-сервера", padding=12)
        group.pack(fill="x", pady=6)
        ttk.Label(group, text="Часы").grid(row=0, column=0, padx=5, pady=7)
        ttk.Spinbox(group, from_=0, to=23, wrap=True, width=5,
                    textvariable=self.v["hour"], format="%02.0f").grid(row=0, column=1, padx=5, pady=7)
        ttk.Label(group, text="Минуты").grid(row=0, column=2, padx=5, pady=7)
        ttk.Spinbox(group, from_=0, to=59, wrap=True, width=5,
                    textvariable=self.v["minute"], format="%02.0f").grid(row=0, column=3, padx=5, pady=7)
        ttk.Button(group, text="Очистить время", command=lambda: (self.v["hour"].set(""), self.v["minute"].set(""))).grid(
            row=0, column=4, padx=10)
        ttk.Label(group, text="Время можно выбрать стрелками или вписать вручную.\n"
                  "Если предыдущая копия затянулась, следующая задача стартует после её завершения.\n"
                  "Если время пустое, задание выполняется только вручную.",
                  foreground="#62748a").grid(row=1, column=0, columnspan=5, sticky="w", padx=4, pady=15)

    def _build_system_tab(self):
        group = ttk.LabelFrame(self.system_tab, text="1С / служба администрирования", padding=8)
        group.pack(fill="x", pady=(0, 7))
        for n, (label, key) in enumerate([
            ("Сервер 1С", "server"), ("RAS", "ras_endpoint"),
            ("Агент кластера", "cluster_agent"), ("UUID кластера", "cluster_uuid"),
            ("Администратор кластера", "cluster_user"),
        ]): self._line(group, label, self.g[key], n)
        self._line(group, "Пароль администратора", self.g["cluster_password"], 5, password=True)
        self._line(group, "Каталог bin 1С", self.g["platform_bin"], 6)
        self._line(group, "Корень копий", self.g["backup_root"], 7)
        self._line(group, "Ожидание сеансов, сек", self.g["wait_seconds"], 8, width=7)
        ttk.Checkbutton(group, text="Принудительно завершать сеансы после ожидания",
                        variable=self.g["force_terminate"]).grid(row=9, column=0, columnspan=2, sticky="w", pady=4)
        self._line(group, "Опрос расписания, сек", self.g["service_poll_seconds"], 10, width=7)
        svc = ttk.LabelFrame(self.system_tab, text="Windows-служба", padding=8)
        svc.pack(fill="x", pady=6)
        btn = ttk.Frame(svc)
        btn.pack(fill="x")
        for label, cmd in [("Установить / обновить", "install"), ("Запустить", "start"),
                           ("Остановить", "stop"), ("Удалить", "remove")]:
            ttk.Button(btn, text=label, command=lambda c=cmd: self.service_command(c)).pack(side="left", padx=3)
        ttk.Label(svc, text="Для установки нужны права администратора. Учётная запись службы должна совпадать\n"
                  "с Windows-пользователем, сохранившим DPAPI-пароли.", foreground="#62748a").pack(anchor="w", pady=6)
        ttk.Button(svc, text="Аварийно снять блокировки 1С", command=self.unblock_all).pack(anchor="w")

    def _fill_tree(self):
        self.selection_change = True
        self.tree.delete(*self.tree.get_children())
        self.nodes = {}
        self.groups = {}
        for kind, name in self.TYPE_LABELS.items():
            group = self.tree.insert("", "end", text=name, open=True, values=("", "", ""))
            self.groups[kind] = group
        for idx, job in enumerate(self.jobs):
            kind = job_type(job)
            node = self.tree.insert(self.groups.get(kind, self.groups["1c"]), "end",
                                    text=job.get("name") or job.get("ref") or "Новое задание")
            self.nodes[node] = idx
        self.selection_change = False
        if self.jobs:
            self.select_index(0)
        else:
            self.selected = None

    def select_index(self, idx: int):
        self.selection_change = True
        for node, i in self.nodes.items():
            if i == idx:
                self.tree.selection_set(node)
                self.tree.see(node)
                self._load_job(idx)
                break
        self.selection_change = False

    def _load_job(self, idx):
        job = self.jobs[idx]
        self.selected = idx
        self.selection_change = True
        for key in ["name", "ref", "ib_user", "sql_server", "sql_database", "sql_backup_dir",
                    "sql_driver", "sql_user", "folder", "keep"]:
            self.v[key].set(job.get(key, ""))
        for key in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth",
                    "sql_trust_cert", "sql_remove_stage"]:
            self.v[key].set(job.get(key, False))
        self.v["archive_format"].set(job.get("archive_format", "none"))
        t = job.get("schedule_time", "") or ""
        if t:
            try:
                h, m = normalize_time_hhmm(t).split(":")
            except Exception:
                h, m = "", ""
        else:
            h, m = "", ""
        self.v["hour"].set(h)
        self.v["minute"].set(m)
        if job_type(job) == "1c":
            self.v["ib_password"].set(self.sec.get("db_passwords", {}).get(job.get("ref", ""), ""))
        else:
            self.v["ib_password"].set("")
        self.v["sql_password"].set(self.sec.get("sql_passwords", {}).get(job_key(job), ""))
        self.filebox.delete("1.0", "end")
        self.filebox.insert("1.0", job.get("file_paths", ""))
        for kind, frame in self.src_frames.items():
            if kind == job_type(job):
                frame.grid()
            else:
                frame.grid_remove()
        self.type_info.configure(text="Тип: " + self.TYPE_LABELS.get(job_type(job), job_type(job)))
        if job_type(job) == "files" and self.v["archive_format"].get() == "none":
            self.v["archive_format"].set("zip")
        self.selection_change = False

    def _commit_selected(self):
        if self.selected is None:
            return
        job = self.jobs[self.selected]
        old_ref = job.get("ref", "")
        for key in ["name", "ref", "ib_user", "sql_server", "sql_database", "sql_backup_dir",
                    "sql_driver", "sql_user", "folder"]:
            job[key] = self.v[key].get().strip()
        job["keep"] = self.v["keep"].get().strip()
        for key in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth",
                    "sql_trust_cert", "sql_remove_stage"]:
            job[key] = bool(self.v[key].get())
        job["archive_format"] = self.v["archive_format"].get()
        h, m = self.v["hour"].get().strip(), self.v["minute"].get().strip()
        job["schedule_time"] = (h + ":" + m) if (h or m) else ""
        job["file_paths"] = self.filebox.get("1.0", "end-1c").strip()
        if job_type(job) == "1c":
            self.sec.setdefault("db_passwords", {})[job["ref"]] = self.v["ib_password"].get()
            # Old mapping is retained intentionally so an accidental Ref edit cannot drop a password.
        if job_type(job) == "mssql":
            self.sec.setdefault("sql_passwords", {})[job_key(job)] = self.v["sql_password"].get()

    def _on_tree_select(self, _event=None):
        if self.selection_change:
            return
        selected = self.tree.selection()
        if not selected or selected[0] not in self.nodes:
            self._commit_selected()
            self.selected = None
            return
        try:
            self._commit_selected()
        except Exception as exc:
            self._log("Редактирование: " + str(exc))
        self._load_job(self.nodes[selected[0]])

    def add_job_menu(self):
        menu = tk.Menu(self, tearoff=0)
        for kind, label in self.TYPE_LABELS.items():
            menu.add_command(label=label, command=lambda k=kind: self.add_job(k))
        menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def add_job(self, kind):
        self._commit_selected()
        n = len(self.jobs) + 1
        ref = f"{kind}_{n}" if kind != "1c" else f"base_{n}"
        job = normalize_db({
            "id": uuid.uuid4().hex if kind != "1c" else "",
            "type": kind,
            "name": f"Новое задание {n}",
            "ref": ref,
            "enabled": True,
            "keep": 7,
            "schedule_time": "",
            "archive_format": "zip" if kind == "files" else "none",
            "sql_server": ".",
        })
        self.jobs.append(job)
        self._fill_tree()
        self.select_index(len(self.jobs)-1)
        self.refresh_view()

    def delete_job(self):
        if self.selected is None:
            return
        name = self.jobs[self.selected].get("name", "")
        if not messagebox.askyesno("Удаление задания", f"Удалить задание «{name}» из настроек?\nСами архивы останутся на диске."):
            return
        self.jobs.pop(self.selected)
        self.selected = None
        self._fill_tree()
        self.refresh_view()

    def clone_job(self):
        if self.selected is None:
            return
        self._commit_selected()
        old = dict(self.jobs[self.selected])
        old["id"] = uuid.uuid4().hex
        old["name"] = old["name"] + " (копия)"
        old["ref"] = old["ref"] + "_copy"
        old["schedule_time"] = ""
        self.jobs.append(old)
        self._fill_tree()
        self.select_index(len(self.jobs)-1)

    def add_file_path(self):
        result = filedialog.askopenfilename(title="Выберите файл")
        if result:
            self.filebox.insert("end", ("\n" if self.filebox.get("1.0", "end-1c").strip() else "") + result)

    def add_directory_path(self):
        result = filedialog.askdirectory(title="Выберите каталог")
        if result:
            self.filebox.insert("end", ("\n" if self.filebox.get("1.0", "end-1c").strip() else "") + result)

    def choose_job_folder(self):
        result = filedialog.askdirectory(title="Каталог локальных резервных копий")
        if result:
            self.v["folder"].set(result)

    def _collect(self):
        self._commit_selected()
        jobs = []
        identifiers = set()
        for job in self.jobs:
            ref = str(job.get("ref", "")).strip()
            if not ref:
                raise RuntimeError("Есть задание без идентификатора Ref")
            if ref.casefold() in identifiers:
                raise RuntimeError(f"Идентификатор задания уже используется: {ref}")
            identifiers.add(ref.casefold())
            normalized = normalize_db(job)
            normalized["schedule_time"] = normalize_time_hhmm(normalized["schedule_time"])
            if normalized["type"] == "files" and normalized["archive_format"] == "none":
                raise RuntimeError("Для задания файлов выберите ZIP или 7z")
            jobs.append(normalized)
        cfg = dict(self.config_data)
        cfg["databases"] = jobs
        for key in ["server", "ras_endpoint", "cluster_agent", "cluster_uuid", "cluster_user",
                    "platform_bin", "backup_root"]:
            cfg[key] = self.g[key].get().strip()
        for key in ["wait_seconds", "service_poll_seconds"]:
            cfg[key] = max(0, int(self.g[key].get().strip()))
        cfg["force_terminate"] = bool(self.g["force_terminate"].get())
        cfg["ftp"] = {key: self.ftp[key].get().strip() for key in ["host", "user", "path"]}
        cfg["ftp"]["port"] = int(self.ftp["port"].get().strip() or 21)
        cfg["ftp"]["tls"] = bool(self.ftp["tls"].get())
        self.sec["cluster_password"] = self.g["cluster_password"].get()
        self.sec["ftp_password"] = self.ftp["password"].get()
        return cfg, self.sec

    def save(self, quiet=False):
        try:
            if self.secrets_read_error:
                raise RuntimeError("Не удалось прочесть сохранённые пароли Windows DPAPI. "
                                   "Сохранение запрещено, чтобы не затереть secrets.dat.")
            cfg, sec = self._collect()
            # Do not discard existing credentials when a job has not been selected yet.
            save_secrets(sec.get("cluster_password", ""), sec.get("db_passwords", {}),
                         sec.get("sql_passwords", {}), sec.get("ftp_password", ""))
            save_config(cfg)
            self.config_data = cfg
            self.jobs = [dict(x) for x in cfg["databases"]]
            self.refresh_view()
            if not quiet:
                messagebox.showinfo(APP_NAME, "Настройки сохранены. Служба автоматически считает изменения по расписанию.")
            return True
        except Exception as exc:
            messagebox.showerror("Настройки", friendly_error_message(exc, str(exc)))
            return False

    def _log(self, text):
        self.q.put(("log", text))

    def _poll(self):
        try:
            while True:
                what, payload = self.q.get_nowait()
                if what == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", str(payload) + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif what == "done":
                    self.worker = None
                    self.refresh_view()
                    messagebox.showinfo(APP_NAME, payload)
                elif what == "error":
                    self.worker = None
                    self.refresh_view()
                    messagebox.showerror(APP_NAME, payload)
                elif what == "refresh":
                    self.refresh_view()
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _periodic(self):
        self.refresh_view()
        self.after(5000, self._periodic)

    def refresh_view(self):
        state = load_scheduler_state()
        for node, idx in list(self.nodes.items()):
            try:
                job = self.jobs[idx]
                entry = get_db_state(job, state)
                result = entry.get("last_result", "")
                status = "✓ OK" if result == "ok" else "✗ Ошибка" if result == "error" else "—"
                tags = ("disabled",) if not job.get("enabled", True) else (
                    ("success",) if result == "ok" else ("fail",) if result == "error" else ())
                self.tree.item(node, text=job.get("name") or job.get("ref"),
                               values=(job.get("schedule_time") or "ручной",
                                       format_ui_time(entry.get("last_success_time", "")), status), tags=tags)
            except (KeyError, IndexError, tk.TclError):
                pass
        complete = sum(1 for x in state.values() if isinstance(x, dict) and x.get("last_result") == "ok")
        self.summary.set(f"Всего заданий: {len(self.jobs)}   •   Включено: {sum(bool(x.get('enabled', True)) for x in self.jobs)}   •   Выполнено успешно: {complete}")
        self.refresh_service_status()

    def refresh_service_status(self):
        if not SERVICE_AVAILABLE:
            self.service_status.set("нет поддержки pywin32")
            return
        try:
            status = win32serviceutil.QueryServiceStatus("OneCBackupManager")[1]
            mapping = {
                win32service.SERVICE_STOPPED: "Остановлена", win32service.SERVICE_RUNNING: "Работает",
                win32service.SERVICE_START_PENDING: "Запускается", win32service.SERVICE_STOP_PENDING: "Останавливается",
            }
            self.service_status.set(mapping.get(status, f"Статус {status}"))
        except Exception:
            self.service_status.set("Не установлена")

    def _start_worker(self, func):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_NAME, "Операция уже выполняется")
            return
        if not self.save(quiet=True):
            return
        def runner():
            try:
                message = func()
                self.q.put(("done", message or "Операция завершена"))
            except Exception as exc:
                self._log("ОШИБКА: " + traceback.format_exc())
                self.q.put(("error", friendly_error_message(exc, str(exc))))
        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    def run_selected(self):
        if self.selected is None:
            messagebox.showwarning(APP_NAME, "Выберите задание слева")
            return
        index = self.selected
        def work():
            job = dict(self.config_data["databases"][index])
            job["enabled"] = True
            try:
                result = run_any_backup(self.config_data, self.sec, job, self._log)
                record_backup_result(job, True, scheduled=False)
            except Exception as exc:
                record_backup_result(job, False, error=friendly_error_message(exc, str(exc)), scheduled=False)
                raise
            return f"Готово: {job['name']}\n{result}"
        self._start_worker(work)

    def run_all(self):
        def work():
            errors = []
            successes = 0
            for job in self.config_data["databases"]:
                if not job.get("enabled", True):
                    continue
                try:
                    run_any_backup(self.config_data, self.sec, job, self._log)
                    record_backup_result(job, True, scheduled=False)
                    successes += 1
                except Exception as exc:
                    error = friendly_error_message(exc, str(exc))
                    record_backup_result(job, False, error=error, scheduled=False)
                    errors.append(f"{job.get('name')}: {error}")
            if errors:
                raise RuntimeError(f"Успешно: {successes}. Ошибки:\n" + "\n".join(errors))
            return f"Все задания выполнены. Успешно: {successes}"
        self._start_worker(work)

    def test_selected(self):
        if self.selected is None:
            return
        index = self.selected
        def work():
            job = self.config_data["databases"][index]
            typ = job_type(job)
            if typ == "1c":
                engine = BackupEngine(self.config_data, self.sec, self._log)
                engine.prepare()
                try:
                    clu = engine.get_cluster_uuid()
                    uuid_value, _ = engine.find_infobase(clu, job["ref"])
                    engine.get_infobase_info(clu, uuid_value, job)
                finally:
                    engine.close()
            elif typ == "mssql":
                from backup_extensions import sql_check_connection
                sql_check_connection(job, self.sec)
            elif typ == "files":
                sources = [s.strip() for s in job.get("file_paths", "").splitlines() if s.strip()]
                if not sources:
                    raise RuntimeError("Не заданы файлы или папки")
                for src in sources:
                    if not Path(src).exists():
                        raise RuntimeError(f"Файл или каталог не найден: {src}")
            return f"Проверка пройдена: {job['name']}"
        self._start_worker(work)

    def test_ftp(self):
        if not self.ftp["tls"].get() and not messagebox.askyesno(
            "Обычный FTP", "Соединение без TLS передаст логин и пароль открытым текстом. Продолжить?"
        ):
            return
        def work():
            ftp_check(self.config_data.get("ftp", {}), self.sec.get("ftp_password", ""))
            return "Соединение FTP/FTPS установлено, каталог доступен"
        self._start_worker(work)

    def retry_ftp(self):
        if self.selected is None:
            return
        index = self.selected
        def work():
            job = self.config_data["databases"][index]
            path = retry_ftp_last(self.config_data, self.sec, job, self._log)
            return f"Отправка на FTP выполнена: {path.name}"
        self._start_worker(work)

    def unblock_all(self):
        if not messagebox.askyesno(APP_NAME, "Аварийно снять блокировку подключений и регламентных заданий 1С?\nИзменяются только включённые задания 1С."):
            return
        def work():
            eng = BackupEngine(self.config_data, self.sec, self._log)
            try:
                eng.prepare()
                for job in self.config_data["databases"]:
                    if job_type(job) == "1c" and job.get("enabled", True):
                        eng.emergency_unblock(job)
            finally:
                eng.close()
            return "Блокировки 1С сняты"
        self._start_worker(work)

    def service_command(self, command):
        if not self.save(quiet=True):
            return
        if not SERVICE_AVAILABLE:
            messagebox.showerror(APP_NAME, "pywin32 недоступен в этой сборке")
            return
        exe = Path(sys.executable if getattr(sys, "frozen", False) else Path(__file__).resolve())
        actual = command
        if command == "install":
            try:
                win32serviceutil.QueryServiceStatus("OneCBackupManager")
                actual = "update"
            except Exception:
                actual = "install"
        try:
            done = subprocess.run([str(exe), actual], capture_output=True, text=False,
                                  creationflags=CREATE_NO_WINDOW)
            result = decode_service_output(done.stdout or b"") + "\n" + decode_service_output(done.stderr or b"")
            if done.returncode != 0:
                raise RuntimeError(friendly_service_error(result, done.returncode))
            if command == "install":
                sc = subprocess.run(["sc.exe", "config", "OneCBackupManager", "start=", "auto"],
                                    capture_output=True, text=False, creationflags=CREATE_NO_WINDOW)
                if sc.returncode:
                    raise RuntimeError("Служба установлена, но не удалось включить автоматический запуск")
            self._log(f"Служба: {command} — OK")
            self.refresh_service_status()
            messagebox.showinfo(APP_NAME, "Операция со службой выполнена")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))


# v4.1 classic task-manager presentation; backup engine and service are unchanged.
from effector_gui import create_app_class
App = create_app_class(sys.modules[__name__])


def cli_main(args):
    cfg = load_config()
    sec = load_secrets()
    engine = BackupEngine(cfg, sec, print)
    if args.test or args.unblock_all:
        engine.prepare()
    try:
        if args.test:
            cluster = engine.get_cluster_uuid()
            print("cluster:", cluster)
            for db in cfg.get("databases", []):
                if not db.get("enabled", True):
                    continue
                ib, _ = engine.find_infobase(cluster, db.get("ref", ""))
                print(db.get("name"), ib)
            return 0

        if args.unblock_all:
            failed = False
            for db in cfg.get("databases", []):
                if not db.get("enabled", True):
                    continue
                try:
                    engine.emergency_unblock(db)
                except Exception as exc:
                    failed = True
                    print(f"ERROR {db.get('name')}: {exc}", file=sys.stderr)
            return 2 if failed else 0

        selected = []
        if args.run:
            wanted = args.run.strip().lower()
            selected = [
                db for db in cfg.get("databases", [])
                if db.get("name", "").lower() == wanted or db.get("ref", "").lower() == wanted
            ]
            if not selected:
                raise RuntimeError(f"Database '{args.run}' not found in config.")
        elif args.run_all:
            selected = [db for db in cfg.get("databases", []) if db.get("enabled", True)]
        else:
            return 0

        failed = False
        for db in selected:
            try:
                execute_job(cfg, sec, db, print, scheduled=False)
            except Exception as exc:
                failed = True
                msg = friendly_error_message(exc, str(exc))
                print(f"ERROR {db.get('name')}: {msg}", file=sys.stderr)
        return 2 if failed else 0
    finally:
        engine.close()


def main():
    # IMPORTANT: SCM starts the registered service executable with no arguments.
    # The previous v2 therefore opened the Tk GUI in Session 0 and Windows returned error 1053.
    # In Session 0 we must connect to the Service Control Manager dispatcher instead.
    if SERVICE_AVAILABLE and len(sys.argv) == 1 and is_windows_service_session():
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(OneCBackupService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--run-all", action="store_true", help="Backup all enabled databases")
    parser.add_argument("--run", help="Backup one database by name/ref")
    parser.add_argument("--unblock-all", action="store_true", help="Emergency unblock all enabled databases")
    parser.add_argument("--test", action="store_true", help="Test RAS/cluster/infobase discovery")
    ns, _ = parser.parse_known_args()

    service_commands = {"install", "update", "remove", "start", "stop", "restart", "debug"}
    if len(sys.argv) > 1 and any(arg.lower() in service_commands for arg in sys.argv[1:]):
        if not SERVICE_AVAILABLE:
            print("pywin32 is not installed or service support is unavailable.", file=sys.stderr)
            return 3
        return win32serviceutil.HandleCommandLine(OneCBackupService) or 0

    if ns.run_all or ns.run or ns.unblock_all or ns.test:
        if os.name != "nt":
            print("This program is intended for Windows.", file=sys.stderr)
            return 3
        try:
            return cli_main(ns)
        except Exception:
            traceback.print_exc()
            return 1

    # Protect the interactive manager, not the independent Session 0 service.
    # A second launch sends an auto-reset event to the existing GUI instead of
    # creating another window. The existing instance can be hidden in the tray.
    from single_instance import GuiSingleInstance
    gui_instance = GuiSingleInstance()
    if not gui_instance.is_primary:
        try:
            gui_instance.activate_existing()
        finally:
            gui_instance.close()
        return 0
    try:
        app = App()
        def poll_gui_activation():
            if gui_instance.activation_requested():
                app.restore_from_tray()
            if app.winfo_exists():
                app.after(250, poll_gui_activation)
        app.after(250, poll_gui_activation)
        app.mainloop()
    finally:
        gui_instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
