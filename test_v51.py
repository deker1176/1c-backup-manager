"""Regression tests for per-run history and active task schedule (v5.1)."""
from datetime import datetime, timedelta
import json
import pytest
from run_journal import begin_task_run, list_task_runs, read_task_log, prune_task_runs, next_scheduled_start


def test_journal_records_different_attempts(tmp_path):
    job = {"id": "unf-id", "name": "UNF", "type": "1c"}
    first = begin_task_run(job, root=tmp_path)
    first.log("1С: начало выгрузки")
    first.log("Ошибка исключительной блокировки")
    first.finish(False, "База занята")
    second = begin_task_run(job, root=tmp_path)
    second.log("1С: успешно")
    second.finish(True, artifact=r"C:\BCP\UNF.dt")
    runs = list_task_runs(root=tmp_path)
    assert len(runs) == 2
    assert {r["status"] for r in runs} == {"ok", "error"}
    assert all(r["job_name"] == "UNF" for r in runs)
    failed = next(r for r in runs if r["status"] == "error")
    assert "исключительной" in read_task_log(failed, root=tmp_path)
    assert failed["message"] == "База занята"


def test_journal_in_progress(tmp_path):
    record = begin_task_run({"ref": "A", "name": "A"}, scheduled=True, root=tmp_path)
    item = list_task_runs(root=tmp_path)[0]
    assert item["scheduled"] is True
    assert item["status"] == "running"
    record.finish(True)
    assert list_task_runs(root=tmp_path)[0]["status"] == "ok"


def test_journal_only_uses_safe_id(tmp_path):
    assert "недоступен" in read_task_log({"run_id": "../secrets.dat"}, root=tmp_path)


def test_journal_ignores_other_json(tmp_path):
    (tmp_path / "secrets.json").write_text('{"token":"sensitive"}')
    assert list_task_runs(root=tmp_path) == []


def test_journal_prunes_old_runs_without_touching_backups(tmp_path):
    old = datetime(2026, 1, 1, 0, 0, 0)
    rec = begin_task_run({"name": "Old"}, root=tmp_path, now=old)
    rec.log("old log")
    rec.finish(True, now=old)
    backup = tmp_path / "UNF.dt"
    backup.write_bytes(b"backup")
    deleted = prune_task_runs(30, root=tmp_path, now=old + timedelta(days=90))
    assert deleted == 1
    assert not rec.log_file.exists()
    assert backup.read_bytes() == b"backup"


def test_next_scheduled_start_preserves_overdue_until_attempt():
    now = datetime(2026, 11, 22, 23, 45)
    expected = datetime(2026, 11, 22, 0, 0)
    assert next_scheduled_start("00:00", "", now) == expected
    assert next_scheduled_start("00:00", "2026-11-22", now) == expected + timedelta(days=1)
    assert next_scheduled_start("", "", now) is None
