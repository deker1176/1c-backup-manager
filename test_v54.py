"""v5.4: two-box UI, safe automatic IIS/Apache discovery, no guessed services."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import inspect
import os
import pytest

import web_maintenance as web
from effector_gui import EffectorTaskEditor
from onec_backup_manager import APP_VERSION, normalize_db, run_file_1c_backup


def demo_apache(tmp_path, file_base=r"C:\Base\unf"):
    root = tmp_path / "Apache24"
    conf = root / "conf" / "httpd.conf"
    included = root / "conf" / "extra" / "unf.conf"
    included.parent.mkdir(parents=True)
    conf.write_text('Include "conf/extra/unf.conf"\n', encoding="utf-8")
    descriptor = tmp_path / "web" / "unf" / "default.vrd"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        f'<point base="/unf" ib="File=&quot;{file_base}&quot;;"/>', encoding="utf-8")
    included.write_text(
        f'Alias "/unf" "{descriptor.parent}"\n'
        f'<Directory "{descriptor.parent}">\n'
        f'ManagedApplicationDescriptor "{descriptor}"\n'
        '</Directory>\n', encoding="utf-8")
    image = f'"{root / "bin" / "httpd.exe"}" -k runservice -n "Apache2.4"'
    return conf, image


def test_apache_1c_publication_verified_by_file_connection(tmp_path):
    conf, image = demo_apache(tmp_path)
    assert web.apache_service_config(image) == conf
    assert web.apache_config_publishes_base(conf, r"C:\Base\unf")
    assert not web.apache_config_publishes_base(conf, r"C:\Base\buh")


def test_apache_service_matching_only_exact_base(tmp_path):
    conf, image = demo_apache(tmp_path)
    services = [{"Name":"Apache2.4", "PathName": image, "State":"Running"},
                {"Name":"Suspicious;Stop-Process", "PathName": image, "State":"Running"}]
    assert [s["name"] for s in web.apache_services_for_base(r"C:\Base\unf", services)] == ["Apache2.4"]
    assert web.apache_services_for_base(r"C:\Base\zup", services) == []


def test_apache_ambiguous_service_is_never_guessed():
    services = [{"Name": "Apache2.4",
                 "PathName": '"C:/Apache24/bin/httpd.exe" -k runservice'}]
    assert web.apache_services_for_base(r"C:\Base\unf", services) == []


def test_apache_service_restored_on_error():
    guard = MagicMock()
    with patch.object(web, "apache_service_state", return_value="Running"), \
         patch.object(web, "ApacheWatchdog", return_value=guard), \
         patch.object(web, "apache_stop") as stop, \
         patch.object(web, "apache_start") as start:
        with pytest.raises(RuntimeError):
            with web.pause_apache_service("Apache2.4", log=lambda _: None):
                raise RuntimeError("disk failed")
    guard.arm.assert_called_once()
    stop.assert_called_once_with("Apache2.4")
    start.assert_called_once_with("Apache2.4")
    guard.disarm.assert_called_once()


def test_apache_guard_failure_never_stops_service():
    guard = MagicMock()
    guard.arm.side_effect = RuntimeError("Scheduler unavailable")
    with patch.object(web, "apache_service_state", return_value="Running"), \
         patch.object(web, "ApacheWatchdog", return_value=guard), \
         patch.object(web, "apache_stop") as stop:
        with pytest.raises(RuntimeError):
            with web.pause_apache_service("Apache2.4"):
                pass
    stop.assert_not_called()


def test_mixed_iis_apache_restored_on_snapshot_failure(tmp_path):
    source = tmp_path / "unf"
    source.mkdir()
    (source / "1Cv8.1CD").write_bytes(b"data")
    stage = tmp_path / "stage"
    events = []

    @contextmanager
    def fake_pause(kind, name):
        events.append(f"stop:{kind}:{name}")
        try:
            yield
        finally:
            events.append(f"start:{kind}:{name}")

    job = {"one_c_file":str(source), "file_web_maintenance_enabled": True,
           "file_force_confirmed": True}

    with patch.object(web, "os", SimpleNamespace(name="nt", path=os.path)), \
         patch.object(web, "web_targets_for_base", return_value=(
             ["DefaultAppPool"], ["Apache2.4"],
             [{"pool":"DefaultAppPool", "app":"Default Web Site/unf",
               "shared":True,"pool_apps":["Default Web Site/","Default Web Site/unf"]}])), \
         patch.object(web, "pause_pool", side_effect=lambda name, log: fake_pause("iis", name)), \
         patch.object(web, "pause_apache_service",
                      side_effect=lambda name, log: fake_pause("apache", name)), \
         patch.object(web, "copy_exclusive", side_effect=OSError("broken disk")):
        with pytest.raises(OSError):
            web.stage_with_web_pause(job, stage, log=lambda _: None)
    assert events == ["stop:iis:DefaultAppPool", "stop:apache:Apache2.4",
                      "start:apache:Apache2.4", "start:iis:DefaultAppPool"]


def test_legacy_settings_do_not_grant_new_permission():
    legacy = normalize_db({"type": "1c", "name": "UNF",
                           "file_iis_maintenance_enabled": True,
                           "file_iis_pool_name": "DefaultAppPool"})
    assert legacy["file_web_maintenance_enabled"] is False
    assert APP_VERSION == "5.4"


def test_gui_hides_pool_name_and_time_fields():
    src = inspect.getsource(EffectorTaskEditor.page_1c_disconnect)
    assert "file_web_maintenance_enabled" in src
    assert "file_auto_disconnect" in src
    assert "file_iis_pool_name" not in src
    assert "file_iis_window_start" not in src
    assert "file_session_hosts" not in src


def test_backend_uses_automatic_detection_and_safely_falls_back_to_local():
    src = inspect.getsource(run_file_1c_backup)
    assert "web_targets_for_base" in src
    assert "stage_with_web_pause" in src
    assert 'file_iis_maintenance_enabled' not in src
    assert "evict_targeted_clients" in src
