from PIL import ImageGrab
import onec_backup_manager as backend
from effector_gui import TaskTypeDialog, EffectorTaskEditor

app=backend.App()
app.geometry('790x740+10+10')
app.update()
app.jobs=[backend.normalize_db({'type':'1c','name':'Бухгалтерия','ref':'acc','one_c_server':'SRV1C','ib_ref':'acc','enabled':True,'schedule_time':'01:00'}),
          backend.normalize_db({'type':'mssql','name':'SQL склад','ref':'sql','enabled':True,'schedule_time':'02:30'}),
          backend.normalize_db({'type':'extra_copy','name':'Документы','ref':'files','enabled':True,'schedule_time':'03:00'}),
          backend.normalize_db({'type':'report','name':'Отчёт администратору','ref':'report','enabled':True,'schedule_time':'04:00'})]
app.refresh_jobs()
app.update()
ImageGrab.grab().crop((0,0,810,780)).save('/mnt/data/1CBackupManager_v4_4_main.png')
win=TaskTypeDialog(app, lambda x: None)
win.geometry('500x420+820+80')
win.update()
ImageGrab.grab().crop((800,50,1360,560)).save('/mnt/data/1CBackupManager_v4_4_wizard.png')
win.destroy()
app.destroy()
