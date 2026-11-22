"""v5.2.2 regression: backup jobs never interrupt with modal result windows."""
import inspect
from effector_gui import create_app_class
from onec_backup_manager import APP_VERSION
from unittest.mock import patch
import pytest
import file_session_control


def test_background_job_results_are_not_modal():
    source = inspect.getsource(create_app_class)
    assert 'messagebox.showerror("Резервное копирование"' not in source
    assert 'messagebox.showinfo("Резервное копирование"' not in source
    assert 'self.refresh_journal()' in source
    assert tuple(map(int, APP_VERSION.split("."))) >= (5, 2, 2)


def test_iis_shared_pool_failure_is_specific_and_safe():
    job = {
        "one_c_file": r"C:\Base\unf",
        "file_disconnect_mode": "force_local",
        "file_force_confirmed": True,
    }
    pubs = [{"app": "Default Web Site/unf", "pool": "DefaultAppPool",
             "shared": True}]
    with patch.object(file_session_control, "rm_locking_pids", create=True), \
         patch("file_lock_diagnostics.rm_locking_pids", return_value={11808}), \
         patch("file_session_control._powershell", return_value=[]), \
         patch("file_lock_diagnostics.iis_publications_for_base", return_value=pubs):
        with pytest.raises(RuntimeError, match="Файловая база занята веб-публикацией IIS") as exc:
            file_session_control.evict_targeted_clients(job, log=lambda _: None)
    assert "DefaultAppPool" in str(exc.value)
    assert "общий" in str(exc.value)
