"""Regression tests for the restored global backup root setting."""
import pytest
from effector_gui import SystemSettingsDialog
from backup_extensions import global_backup_lock

def test_global_backup_root_settings_visible():
    assert "Файлы архивов" in SystemSettingsDialog.PAGES
    assert SystemSettingsDialog.PAGES.index("Файлы архивов") == 3

def test_backup_root_validation_preserves_selected_path(tmp_path):
    path = str(tmp_path / "global")
    assert SystemSettingsDialog.validate_backup_root(path) == path

def test_blank_root_rejected():
    with pytest.raises(ValueError, match="корневую папку"):
        SystemSettingsDialog.validate_backup_root("  ")

def test_lock_uses_global_root(tmp_path):
    with global_backup_lock(tmp_path):
        assert (tmp_path / "backup.lock").exists()

def test_bad_root_reports_settings_location(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("not a directory")
    with pytest.raises(RuntimeError, match="Параметры программы.*Файлы архивов"):
        with global_backup_lock(bad):
            pass
