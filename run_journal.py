"""Persistent per-attempt journal for GUI and Windows service runs.

Every run owns a unique JSON metadata record and a UTF-8 text log. Completion
updates metadata atomically. No user passwords or DPAPI secrets are stored.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sys
import uuid

_VALID_ID = re.compile(r"^\d{8}_\d{12}_[0-9a-f]{32}$")


def history_dir(root=None):
    if root is not None:
        return Path(root)
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "task_history"


class TaskRun:
    def __init__(self, job, scheduled=False, root=None, now=None):
        self.folder = history_dir(root)
        self.folder.mkdir(parents=True, exist_ok=True)
        start = now or datetime.now()
        self.run_id = start.strftime("%Y%m%d_%H%M%S%f") + "_" + uuid.uuid4().hex
        self.meta_file = self.folder / (self.run_id + ".json")
        self.log_file = self.folder / (self.run_id + ".log")
        self.info = {
            "run_id": self.run_id,
            "job_key": str(job.get("id") or job.get("ref") or job.get("name") or ""),
            "job_name": str(job.get("name") or job.get("ref") or "Задание"),
            "job_type": str(job.get("type") or "1c"),
            "scheduled": bool(scheduled),
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": "",
            "status": "running",
            "message": "",
            "artifact": "",
        }
        self._write_meta()

    def _write_meta(self):
        temporary = self.meta_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.meta_file)

    def log(self, message):
        with self.log_file.open("a", encoding="utf-8") as stream:
            stream.write(str(message).rstrip("\n") + "\n")

    def finish(self, success, message="", artifact=None, now=None):
        self.info["end"] = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        self.info["status"] = "ok" if success else "error"
        self.info["message"] = str(message or "")[:3000]
        self.info["artifact"] = str(artifact or "")
        self._write_meta()


def begin_task_run(job, scheduled=False, root=None, now=None):
    return TaskRun(job, scheduled=scheduled, root=root, now=now)


def list_task_runs(limit=500, root=None):
    folder = history_dir(root)
    if not folder.is_dir():
        return []
    result = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        if not _VALID_ID.fullmatch(path.stem):
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("run_id") != path.stem:
                continue
            if item.get("status") == "running":
                try:
                    start = datetime.strptime(item["start"], "%Y-%m-%d %H:%M:%S")
                    if datetime.now() - start > timedelta(days=2):
                        item["status"] = "interrupted"
                except (ValueError, TypeError, KeyError):
                    pass
            result.append(item)
        except (OSError, ValueError, TypeError):
            continue
        if len(result) >= max(1, int(limit)):
            break
    return result


def read_task_log(record, root=None, max_chars=250000):
    run_id = str(record.get("run_id") or "")
    if not _VALID_ID.fullmatch(run_id):
        return "Подробный журнал этого запуска недоступен."
    path = history_dir(root) / (run_id + ".log")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read(max_chars + 1)
            return text[:max_chars] + ("\n… журнал ограничен при просмотре" if len(text) > max_chars else "")
    except OSError:
        return "Файл подробного журнала не найден."


def prune_task_runs(days, root=None, now=None):
    days = int(days or 0)
    if days <= 0:
        return 0
    folder = history_dir(root)
    if not folder.is_dir():
        return 0
    cutoff = (now or datetime.now()) - timedelta(days=days)
    removed = 0
    for path in folder.glob("*.json"):
        if not _VALID_ID.fullmatch(path.stem):
            continue
        try:
            stamp = datetime.strptime(path.stem[:21], "%Y%m%d_%H%M%S%f")
            if stamp < cutoff:
                path.unlink()
                path.with_suffix(".log").unlink(missing_ok=True)
                removed += 1
        except (ValueError, OSError):
            pass
    return removed


def next_scheduled_start(schedule_time, last_scheduled_date="", now=None):
    """The service runs overdue enabled tasks; after an attempt it runs tomorrow."""
    now = now or datetime.now()
    value = str(schedule_time or "")
    try:
        h, minute = map(int, value.split(":"))
        if not (0 <= h <= 23 and 0 <= minute <= 59):
            return None
    except (ValueError, TypeError):
        return None
    due = now.replace(hour=h, minute=minute, second=0, microsecond=0)
    if last_scheduled_date == now.strftime("%Y-%m-%d"):
        due += timedelta(days=1)
    return due
