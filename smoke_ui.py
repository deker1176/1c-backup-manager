import tkinter as tk
from onec_backup_manager import App, normalize_db
from classic_gui import TaskEditor, InfobaseImportDialog, SettingsDialog
from pathlib import Path

app = App()
app.update_idletasks()
print('main initialized:', app.title(), 'jobs',len(app.jobs), 'size',app.winfo_width(),app.winfo_height())
assert not app.jobs
job = normalize_db({'id':'test-j1','type':'1c','name':'Тестовая база','ref':'test','enabled':False, 'connection_kind':'server','one_c_server':'server','ib_ref':'test'})
editor=TaskEditor(app,job,lambda *_:None)
editor.update()
print('editor initialized',editor.winfo_width(),editor.winfo_height())
editor.destroy()
imp=InfobaseImportDialog(app)
imp.path.set('/mnt/data/ibases.v8i')
imp.load_file()
imp.update()
print('import dialog items', len(imp.items))
assert len(imp.items)==11
imp.destroy()
sets = SettingsDialog(app)
sets.update()
print('settings dialog initialized')
sets.destroy()
app.update()
app.destroy()
print('PASS')
