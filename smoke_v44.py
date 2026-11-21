from pathlib import Path
from PIL import ImageGrab
import onec_backup_manager as backend
from effector_gui import EffectorTaskEditor, SystemSettingsDialog, TaskTypeDialog

app=backend.App()
app.update()
assert app.jobs == [], 'fresh app should start with no tasks'
for kind in ('1c','mssql','files','extra_copy','commands','report'):
    obj=backend.normalize_db({'name':kind+' demo','ref':kind+'demo','type':kind,'archive_format':'zip'})
    if kind == '1c': obj.update(one_c_server='SRV1C',ib_ref='acc')
    if kind == 'mssql': obj.update(sql_server='.',sql_database='demo')
    if kind in {'files','extra_copy'}: obj['file_paths']='C:\\Documents'
    dlg=EffectorTaskEditor(app,obj, lambda *args: None)
    dlg.update()
    expected=8 if kind in {'1c','mssql','files','extra_copy'} else 4 if kind=='commands' else 3
    assert len(dlg.pages)==expected,(kind,len(dlg.pages))
    for idx in range(expected):
        dlg.nav.selection_clear(0,'end'); dlg.nav.selection_set(idx); dlg._select_page(); dlg.update()
    if kind == 'report':
        dlg.v['notify_telegram'].set(True)
        assert dlg.extract()['notify_telegram']
    if kind == 'commands':
        dlg.commandbox.insert('1.0','echo hello')
        assert dlg.extract()['command_body']=='echo hello'
    dlg.destroy()
app.update()
ImageGrab.grab().crop((0,0,1280,870)).save('/mnt/data/1CBackupManager_v4_4_main.png')
setdlg=SystemSettingsDialog(app)
setdlg.update()
assert len(setdlg.pages)==9
for idx in range(9):
    setdlg.nav.selection_clear(0,'end'); setdlg.nav.selection_set(idx); setdlg._switch(); setdlg.update()
setdlg.nav.selection_clear(0,'end'); setdlg.nav.selection_set(7); setdlg._switch(); setdlg.update()
ImageGrab.grab().crop((0,0,1280,870)).save('/mnt/data/1CBackupManager_v4_4_reports.png')
setdlg.destroy()
app.destroy()
print('SMOKE V4.4 PASS')
