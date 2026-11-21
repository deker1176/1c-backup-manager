"""Execution of optional Windows task hooks and cooperative deadline checks.

This module intentionally does not change the proven RAC/DumpIB maintenance sequence.
A deadline is enforced at supported subprocess/ODBC stages; Python cannot safely
interrupt a synchronous filesystem write or an FTP data transfer mid-call.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import os
import subprocess
import sys
import time

_DEADLINE = ContextVar("backup_task_deadline", default=None)


@contextmanager
def task_deadline(job: dict):
    enabled = bool(job.get("stop_after_enabled", False))
    hours = int(job.get("stop_after_hours", 24) or 0)
    minutes = int(job.get("stop_after_minutes", 0) or 0)
    seconds = hours * 3600 + minutes * 60
    if enabled and seconds <= 0:
        raise ValueError("Лимит времени должен быть больше нуля")
    token = _DEADLINE.set(time.monotonic() + seconds if enabled else None)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def limit_seconds(default: int | float):
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    remain = deadline - time.monotonic()
    if remain <= 0:
        raise TimeoutError("Превышено заданное время выполнения резервного копирования")
    return max(0.1, min(float(default), remain))


def check_deadline():
    limit_seconds(24 * 3600)


def run_commands(script: str, phase: str, job: dict, log=print, status="success",
                 ignore_deadline: bool = False):
    lines = [v.strip() for v in str(script or "").splitlines()
             if v.strip() and not v.lstrip().startswith(("#", "::"))]
    if not lines:
        return
    env = os.environ.copy()
    env["BACKUP_TASK"] = str(job.get("name", ""))
    env["BACKUP_REF"] = str(job.get("ref", ""))
    env["BACKUP_STATUS"] = status
    log(f"{phase}: выполнение системных команд ({len(lines)})")
    for idx, line in enumerate(lines, 1):
        # The commands can contain credentials, so do not write them to logs.
        if os.name == "nt":
            argv = ["cmd.exe", "/d", "/s", "/c", line]
        else:
            argv = ["/bin/sh", "-c", line]
        timeout = 15 * 60 if ignore_deadline else limit_seconds(15 * 60)
        result = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
        log(f"{phase}: команда {idx}, код возврата {result.returncode}")
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise RuntimeError(f"{phase}: команда {idx} завершилась с кодом {result.returncode}: {tail}")
