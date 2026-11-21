import sys, time
from pathlib import Path
from PIL import ImageGrab
import onec_backup_manager as core
from effector_gui import SystemSettingsDialog, EffectorTaskEditor
lang = sys.argv[1]
mode = sys.argv[2]
core.DEFAULT_CONFIG['ui_language']=lang
core.DEFAULT_CONFIG['server']=''
app=core.App()
app.geometry('800x650+30+40')
app.update()
if mode=='settings':
    dlg=SystemSettingsDialog(app)
    dlg.geometry('925x660+80+80')
    dlg.nav.selection_clear(0,'end')
    dlg.nav.selection_set(dlg.nav.size()-1)
    dlg._switch()
else:
    job=core.normalize_db({'type':'1c','id':'t','name':'Новая база' if lang=='ru' else 'New database', 'ref':'', 'ib_ref':'','connection_kind':'server','one_c_server':'','enabled':False,'platform_mode':'auto'})
    dlg=EffectorTaskEditor(app,job,lambda *x: None)
    dlg.geometry('930x660+70+80')
for i in range(5):
    app.update(); time.sleep(0.1)
p=Path('/mnt/data') / ('1CBackupManager_v4_7_'+lang+'_'+mode+'.png')
ImageGrab.grab(bbox=(40,60,1070,790)).save(p)
print('created:',p)
dlg.destroy(); app.destroy()
