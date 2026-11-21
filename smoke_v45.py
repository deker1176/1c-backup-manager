"""GUI sanity-check for use with xvfb-run; no real FTP/SQL/Windows service is touched."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import onec_backup_manager as backend
import effector_gui


class FakeTray:
    def __init__(self, cb):
        self.cb = cb
        self.icon = None
    def start(self):
        self.icon = type('Icon', (), {'visible': True})()
    def stop(self):
        self.icon = None


def main():
    with TemporaryDirectory() as d:
        backend.CONFIG_PATH = Path(d) / 'config.json'
        backend.SECRETS_PATH = Path(d) / 'secrets.dat'
        backend.SVC_STATE_PATH = Path(d) / 'scheduler_state.json'
        with patch.object(effector_gui, 'tray_available', lambda: True), patch.object(effector_gui, 'TrayController', FakeTray):
            app = backend.App()
            app.geometry('850x610+30+30')
            app.update()
            assert app.agent_status_label.winfo_ismapped() == 1
            assert app.agent_status_label.winfo_rooty() < app.winfo_rooty() + app.winfo_height()
            assert 'Агент' in app.agent_status.get()
            app.hide_to_tray()
            app.update()
            assert app.state() == 'withdrawn'
            app.tray_events.put('show')
            app.poll_tray()
            app.update()
            assert app.state() == 'normal'
            app.exit_app()
            print('PASS: status visible, hide to tray, restore from event, exit')


if __name__ == '__main__':
    main()
