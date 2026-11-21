import time
from pathlib import Path
from onec_backup_manager import App, normalize_db
from effector_gui import (EffectorTaskEditor, StorageProfilesDialog, StorageProfileEditor,
                          TaskTypeDialog, SystemSettingsDialog)
from PIL import ImageGrab

app=App()
app.update()
assert not app.jobs, 'fresh start should have empty tasks'
app.cfg['task_folders']=['Компания','Компания/Клиенты']
app.jobs=[normalize_db({'id':'d1','type':'1c','name':'Бухгалтерия','ref':'acc','ib_ref':'acc',
                        'one_c_server':'SRV1C','enabled':False,'connection_kind':'server', 'task_group':'Компания'}),
          normalize_db({'id':'d2','type':'mssql','name':'SQL склад','ref':'sql_1','sql_database':'bp','sql_server':'.',
                        'enabled':True, 'task_group':'Компания/Клиенты'}),
          normalize_db({'id':'d3','type':'files','name':'Каталог документов','ref':'file_1',
                        'file_paths':'C:\\Docs','enabled':True})]
app.refresh_jobs(); app.update()
assert app.tree.exists('folder::Компания')
assert app.tree.exists('job_2')
# Screenshot of actual running GUI
ImageGrab.grab().crop((0,0,950,780)).save('/mnt/data/1CBackupManager_v4_3_main.png')
for job in app.jobs:
    dlg=EffectorTaskEditor(app, job, lambda *_:None)
    dlg.update()
    assert len(dlg.pages)==7
    for n in range(7):
        dlg.nav.selection_clear(0,'end'); dlg.nav.selection_set(n)
        dlg._select_page(); dlg.update_idletasks()
    if job['type']=='1c':
        dlg.nav.selection_clear(0,'end'); dlg.nav.selection_set(2)
        dlg._select_page(); dlg.update()
        ImageGrab.grab().crop((0,0,950,780)).save('/mnt/data/1CBackupManager_v4_3_storage.png')
    dlg.destroy()

p=StorageProfilesDialog(app); p.update(); assert p.tree.get_children(); p.destroy()
p=StorageProfileEditor(app, {'name':'BP','host':'ftp.example.net','path':'/backup','port':21}, 'test', lambda *_:None)
p.update(); p.destroy()
p=TaskTypeDialog(app, lambda kind:None); p.update(); p.destroy()
p=SystemSettingsDialog(app); p.update(); p.destroy()
app.destroy()
print('V4.3 GUI SMOKE PASS')
