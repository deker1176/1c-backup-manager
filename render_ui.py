from pathlib import Path
from PIL import ImageGrab
from onec_backup_manager import App, normalize_db
from classic_gui import TaskEditor
import time
app = App()
app.update()
time.sleep(.5)
ImageGrab.grab().crop((0,0,1235,855)).save('/mnt/data/1CBackupManager_v4_1_main_preview.png')
app.jobs = [normalize_db({'id':'t1', 'name':'Бухгалтерия','ref':'accounting','ib_ref':'accounting','type':'1c', 'enabled': True, 'connection_kind':'server','one_c_server':'SRV1C'})]
app.refresh_jobs(); app.update()
editor=TaskEditor(app,app.jobs[0], lambda *_:None)
editor.update(); time.sleep(.5)
# Window positioning may differ in VNC. grab full desktop 1600x1000.
ImageGrab.grab().save('/mnt/data/1CBackupManager_v4_1_editor_preview.png')
editor.destroy();app.destroy()
