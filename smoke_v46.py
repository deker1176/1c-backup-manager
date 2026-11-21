"""Run with: xvfb-run -a python smoke_v46.py"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import onec_backup_manager as backend
import effector_gui
from PIL import ImageGrab

class FakeTray:
    def __init__(self, cb): self.cb=cb; self.icon=None
    def start(self): self.icon=type('Icon', (), {'visible':True})()
    def stop(self): self.icon=None

with TemporaryDirectory() as d:
    backend.CONFIG_PATH = Path(d)/'config.json'
    backend.SECRETS_PATH = Path(d)/'secrets.dat'
    backend.SVC_STATE_PATH = Path(d)/'scheduler_state.json'
    with patch.object(effector_gui, 'tray_available', lambda:True), patch.object(effector_gui, 'TrayController', FakeTray):
        app = backend.App()
        app.geometry('900x650+20+20')
        app.update()
        assert app.agent_status_label.winfo_ismapped()
        for kind in ('1c_check', '1c_procedure'):
            job = backend.normalize_db({'id':kind, 'type':kind, 'name':kind, 'ref':kind,
                                        'one_c_server':'SRV1C','ib_ref':'demo','enabled':False})
            window=effector_gui.EffectorTaskEditor(app, job, lambda *args:None)
            window.geometry('890x680+940+20')
            window.update()
            assert len(window.page_names()) == 6
            window.nav.selection_clear(0,'end')
            window.nav.selection_set(2)
            window._select_page()
            window.update()
            if kind == '1c_check':
                assert window.v['check_mode'].get() == 'test'
                assert window.extract()['check_mode'] == 'test'
                ImageGrab.grab().save('/mnt/data/1CBackupManager_v4_6_check_editor.png')
            else:
                assert window.v['procedure_name'].get()
                epf = Path(d)/'test.epf'
                epf.write_bytes(b'epf_placeholder')
                window.v['procedure_epf'].set(str(epf))
                assert window.extract()['procedure_epf'] == str(epf)
                ImageGrab.grab().save('/mnt/data/1CBackupManager_v4_6_procedure_editor.png')
            window.destroy()
            app.update()
        wizard = effector_gui.TaskTypeDialog(app, lambda kind:None)
        wizard.update()
        assert wizard.tree.exists('1c_check') and wizard.tree.exists('1c_procedure')
        assert not wizard.tree.exists('1c77')
        wizard.destroy()
        app.exit_app()
print('PASS: app/maintenance/procedure/task-wizard GUI')
