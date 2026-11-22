"""Regression: the active Effector UI displays the real backend version."""
import inspect
from effector_gui import create_app_class
from onec_backup_manager import APP_VERSION

def test_window_title_is_not_a_stale_hardcoded_version():
    source = inspect.getsource(create_app_class)
    assert 'backend.APP_VERSION' in source
    assert 'self.title("1C Backup Manager v5.1' not in source
    assert tuple(map(int, APP_VERSION.split("."))) >= (5, 2, 1)
