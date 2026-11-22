"""v5.4.1: slow IIS stop/start must not produce spurious 45s errors."""
from unittest.mock import MagicMock, call, patch
import subprocess
import pytest
import iis_maintenance as m


def test_iis_stop_allows_180_seconds_then_checks_state():
    with patch.object(m, "_appcmd") as cmd, \
         patch.object(m, "wait_state") as wait:
        m.stop_pool("DefaultAppPool", log=lambda _: None)
    cmd.assert_called_once_with("stop", "apppool", "DefaultAppPool", timeout=180)
    wait.assert_called_once_with("DefaultAppPool", "Stopped", seconds=90)


def test_appcmd_stop_timeout_can_still_succeed_if_pool_stopped():
    logs = []
    with patch.object(m, "_appcmd",
                      side_effect=subprocess.TimeoutExpired(["appcmd", "stop"], 180)), \
         patch.object(m, "wait_state") as wait:
        m.stop_pool("DefaultAppPool", log=logs.append)
    assert wait.call_args_list == [
        call("DefaultAppPool", "Stopped", seconds=30),
        call("DefaultAppPool", "Stopped", seconds=90)]
    assert any("180" in line for line in logs)


def test_stop_timeout_without_stopped_state_fails_closed():
    with patch.object(m, "_appcmd",
                      side_effect=subprocess.TimeoutExpired(["appcmd", "stop"], 180)), \
         patch.object(m, "wait_state", side_effect=RuntimeError("Stopping")):
        with pytest.raises(RuntimeError, match="копирование отменено"):
            m.stop_pool("DefaultAppPool", log=lambda _: None)


def test_start_timeout_with_successful_state_is_not_error():
    with patch.object(m, "pool_state", side_effect=["Stopped", "Stopped"]), \
         patch.object(m, "_appcmd",
                      side_effect=subprocess.TimeoutExpired(["appcmd", "start"], 150)), \
         patch.object(m, "wait_state") as wait:
        m.start_pool("DefaultAppPool")
    assert wait.call_args_list == [
        call("DefaultAppPool", "Started", seconds=30),
        call("DefaultAppPool", "Started", seconds=90)]


def test_stop_failure_restores_pool_and_never_copies():
    guard = MagicMock()
    with patch.object(m, "pool_state", return_value="Started"), \
         patch.object(m, "IISWatchdog", return_value=guard), \
         patch.object(m, "stop_pool", side_effect=RuntimeError("IIS busy")), \
         patch.object(m, "start_pool") as starter:
        with pytest.raises(RuntimeError, match="IIS busy"):
            with m.pause_pool("DefaultAppPool", log=lambda _: None):
                pytest.fail("Unstopped pool must not reach database copying")
    starter.assert_called_once_with("DefaultAppPool")
    guard.disarm.assert_called_once()
