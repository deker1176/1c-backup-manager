from PIL import ImageGrab
from onec_backup_manager import App, normalize_db
from classic_gui import TaskEditor, InfobaseImportDialog
import time
app=App(); app.update(); time.sleep(.15)
ImageGrab.grab().crop((0,0,1235,855)).save('/mnt/data/1CBackupManager_v4_1_main.png')
job=normalize_db({'id':'demo','type':'1c','name':'Бухгалтерия','ref':'buh','ib_ref':'buh','one_c_server':'SRV1C','enabled':False,'connection_kind':'server'})
ed=TaskEditor(app,job,lambda *_:None)
ed.nav.selection_clear(0,'end'); ed.nav.selection_set(1); ed.nav.event_generate('<<ListboxSelect>>')
ed.update(); time.sleep(.1)
ImageGrab.grab().crop((0,0,1060,730)).save('/mnt/data/1CBackupManager_v4_1_settings.png')
ed.destroy(); app.destroy()
