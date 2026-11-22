"""v5.3: IIS stop is explicit, time-limited, crash-guarded and reversible."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import inspect
import pytest
import iis_maintenance as m
from onec_backup_manager import APP_VERSION, run_file_1c_backup, normalize_db


def test_midnight_window():
    assert m.in_window(datetime(2026, 11, 23, 0, 45))
    assert m.in_window(datetime(2026, 11, 22, 23, 0))
    assert not m.in_window(datetime(2026, 11, 23, 6, 0))
    assert not m.in_window(datetime(2026, 11, 23, 12, 0))
    with pytest.raises(ValueError):
        m.in_window(datetime.now(), "00:00", "00:00")
    with pytest.raises(ValueError):
        m.in_window(datetime.now(), "wrong", "06:00")


def test_old_jobs_never_authorize_iis():
    job = normalize_db({"type": "1c", "name": "UNF"})
    assert job["file_iis_maintenance_enabled"] is False
    assert job["file_iis_pool_name"] == ""
    assert tuple(map(int, APP_VERSION.split("."))) >= (5, 3, 0)
    with pytest.raises(RuntimeError, match="разрешение"):
        m.maintenance_plan(job)


def test_exact_pool_and_publication_required(tmp_path):
    (tmp_path / "1Cv8.1CD").write_bytes(b"dummy")
    job = {"file_iis_maintenance_enabled": True, "file_force_confirmed": True,
           "file_iis_pool_name": "DefaultAppPool", "one_c_file": str(tmp_path)}
    pubs = [{"app": "Default Web Site/unf", "pool": "DefaultAppPool", "shared": True}]
    with patch.object(m, "os", SimpleNamespace(name="nt")):
        assert m.maintenance_plan(job, now=datetime(2026, 11, 23, 1),
                                  discover=lambda _: pubs)[0] == "DefaultAppPool"
        with pytest.raises(RuntimeError, match="разрешён только"):
            m.maintenance_plan(job, now=datetime(2026, 11, 23, 1),
                               discover=lambda _: [{"app": "/buh", "pool": "AnotherPool"}])
        with pytest.raises(RuntimeError, match="окне"):
            m.maintenance_plan(job, now=datetime(2026, 11, 23, 12),
                               discover=lambda _: pubs)
        with pytest.raises(RuntimeError, match="публикация"):
            m.maintenance_plan(job, now=datetime(2026, 11, 23, 1),
                               discover=lambda _: [])


def test_iis_restored_after_copy_failure():
    guard = MagicMock()
    with patch.object(m, "pool_state", return_value="Started"), \
         patch.object(m, "IISWatchdog", return_value=guard), \
         patch.object(m, "stop_pool") as stop, \
         patch.object(m, "start_pool") as resume:
        with pytest.raises(OSError):
            with m.pause_pool("DefaultAppPool", log=lambda _: None):
                raise OSError("Read failed")
    guard.arm.assert_called_once()
    resume.assert_called_once_with("DefaultAppPool")
    guard.disarm.assert_called_once()
    assert stop.call_count == 1
    assert stop.call_args.args == ("DefaultAppPool",)


def test_watchdog_arm_failure_must_not_stop_pool():
    guard = MagicMock()
    guard.arm.side_effect = RuntimeError("No Task Scheduler rights")
    with patch.object(m, "pool_state", return_value="Started"), \
         patch.object(m, "IISWatchdog", return_value=guard), \
         patch.object(m, "stop_pool") as stop:
        with pytest.raises(RuntimeError):
            with m.pause_pool("DefaultAppPool"):
                pass
    stop.assert_not_called()


def test_watchdog_remains_if_iis_restore_fails():
    guard = MagicMock()
    with patch.object(m, "pool_state", return_value="Started"), \
         patch.object(m, "IISWatchdog", return_value=guard), \
         patch.object(m, "stop_pool"), \
         patch.object(m, "start_pool", side_effect=RuntimeError("IIS unavailable")):
        with pytest.raises(RuntimeError, match="IIS unavailable"):
            with m.pause_pool("DefaultAppPool", log=lambda _: None):
                pass
    guard.disarm.assert_not_called()


def test_dumpib_uses_snapshot_not_live_base():
    source = inspect.getsource(run_file_1c_backup)
    assert "TemporaryDirectory" in source
    assert "stage_with_web_pause" in source
    assert "dump_attempt(3, actual_base=Path(stage))" in source
    assert "return dest" in source
