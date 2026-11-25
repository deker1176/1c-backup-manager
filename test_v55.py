"""v5.5: simultaneous schedule + manual queue, archive list and isolated restore."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import inspect
from pathlib import Path
import subprocess
import threading
import time
from unittest.mock import patch
import pytest

from backup_browser import (find_backup_artifacts, backup_locations,
                            validate_new_restore_target, verify_dt_checksum,
                            restore_dt_as_test_infobase)
from backup_extensions import global_backup_lock
from effector_gui import create_app_class
from onec_backup_manager import APP_VERSION, should_run_now


def test_archives_from_job_folder_not_inside_backup_root(tmp_path):
    global_root = tmp_path / "global_root"
    global_root.mkdir()
    custom = tmp_path / "other_drive" / "bcp1"
    custom.mkdir(parents=True)
    file = custom / "Davidenko_2026-11-26_00-40-38.dt"
    file.write_bytes(b"sample data")
    other = custom / "Davidenko_2026-11-26_00-40-38.dt.sha256"
    other.write_text("test")
    cfg = {"backup_root": str(global_root)}
    jobs = [{"name": "ДАВИДЕНКО", "folder": str(custom)}]
    discovered = find_backup_artifacts(cfg, jobs)
    assert len(discovered) == 1
    assert discovered[0]["path"] == str(file.resolve())
    assert discovered[0]["format"] == "dt"


def test_recover_archive_path_from_run_journal_artifact(tmp_path):
    path = tmp_path / "external" / "backup.dt"
    path.parent.mkdir()
    path.write_bytes(b"backup")
    results = find_backup_artifacts({"backup_root": str(tmp_path / "none")},
                                    [], [str(path)])
    assert [a["path"] for a in results] == [str(path)]


def test_missing_unc_folder_does_not_break_scanning(tmp_path):
    local = tmp_path / "good"
    local.mkdir()
    (local / "archive.dt").write_bytes(b"abc")
    jobs = [{"folder": str(tmp_path / "offline-network")},
            {"folder": str(local)}]
    assert len(find_backup_artifacts({"backup_root": ""}, jobs)) == 1


def test_schedule_same_midnight_time_runs_both_in_sequence():
    now = datetime(2026, 11, 26, 0, 0, 0)
    jobs = [{"id": "a", "enabled": True, "schedule_time": "00:00"},
            {"id": "b", "enabled": True, "schedule_time": "00:00"}]
    state = {}
    assert [x["id"] for x in jobs if should_run_now(x, state, now)] == ["a", "b"]


def test_global_lock_waits_instead_of_failing(tmp_path):
    timeline = []
    first_entered = threading.Event()
    release = threading.Event()

    def holder():
        with global_backup_lock(tmp_path, wait_seconds=5, poll_seconds=.02):
            timeline.append("first")
            first_entered.set()
            assert release.wait(5)
        timeline.append("released")

    def waiter():
        assert first_entered.wait(5)
        with global_backup_lock(tmp_path, wait_seconds=5, poll_seconds=.02):
            timeline.append("second")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(holder), pool.submit(waiter)
        assert first_entered.wait(5)
        time.sleep(.15)
        assert "second" not in timeline
        release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    assert timeline.index("released") < timeline.index("second")


def test_global_lock_wait_expires(tmp_path):
    with global_backup_lock(tmp_path, wait_seconds=2):
        with pytest.raises(RuntimeError, match="Истекло время ожидания"):
            with global_backup_lock(tmp_path, wait_seconds=.12, poll_seconds=.02):
                pass


def test_checksum_matches_archive_and_detects_corruption(tmp_path):
    dt = tmp_path / "backup.dt"
    dt.write_bytes(b"v55 archive")
    digest = hashlib.sha256(dt.read_bytes()).hexdigest()
    Path(str(dt) + ".sha256").write_text(f"{digest}  backup.dt\n")
    assert verify_dt_checksum(dt) == digest
    dt.write_bytes(b"CORRUPTED")
    with pytest.raises(RuntimeError, match="НЕ СОВПАДАЕТ"):
        verify_dt_checksum(dt)


def test_restoration_only_to_new_empty_base(tmp_path):
    archive = tmp_path / "backups" / "good.dt"
    archive.parent.mkdir()
    archive.write_bytes(b"data")
    prod = tmp_path / "prod" / "1C"
    prod.mkdir(parents=True)
    (prod / "1Cv8.1CD").write_bytes(b"production")
    jobs = [{"connection_kind": "file", "one_c_file": str(prod)}]
    with pytest.raises(RuntimeError, match="рабочей базой"):
        validate_new_restore_target(archive, prod, jobs)
    with pytest.raises(RuntimeError, match="рабочей базой"):
        validate_new_restore_target(archive, prod / "restored", jobs)
    with pytest.raises(RuntimeError, match="рабочей базой"):
        validate_new_restore_target(archive, prod.parent, jobs)
    other = tmp_path / "another"
    other.mkdir()
    (other / "1Cv8.1CD").write_bytes(b"not empty")
    with pytest.raises(RuntimeError, match="ПУСТЫМ"):
        validate_new_restore_target(archive, other, jobs)
    assert validate_new_restore_target(archive, tmp_path / "new", jobs)[1] == tmp_path / "new"


def test_restore_uses_createinfobase_template_in_new_folder(tmp_path):
    archive = tmp_path / "existing.dt"
    archive.write_bytes(b"archive")
    exe = tmp_path / "1cv8.exe"
    exe.write_bytes(b"test")
    target = tmp_path / "restore_test"
    def run(argv, **kwargs):
        assert argv[1] == "CREATEINFOBASE"
        assert argv[2] == f'File="{target}";'
        assert argv[3:5] == ["/UseTemplate", str(archive)]
        assert "/AddToList" not in argv
        (target / "1Cv8.1CD").write_bytes(b"restored")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    with patch("backup_browser.subprocess.run", side_effect=run):
        assert restore_dt_as_test_infobase(archive, target, [], exe) == target


def test_manual_gui_queue_and_archives_actions_are_wired():
    text = inspect.getsource(create_app_class)
    assert "self.pending_works.append" in text
    assert "self._start_next_worker()" in text
    assert 'В очереди (' in text
    assert 'find_backup_artifacts' in text
    assert 'restore_dt_as_test_infobase' in text
    assert 'Восстановить в тестовую базу' in text
    assert APP_VERSION == "5.5"
