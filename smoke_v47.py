import os
import sys
os.environ.setdefault('PYTHONPATH', os.path.dirname(__file__))
import tkinter as tk
import onec_backup_manager as core
from effector_gui import EffectorTaskEditor, SystemSettingsDialog, TaskTypeDialog
from ui_locale import tr, source_value

core.DEFAULT_CONFIG['ui_language'] = sys.argv[1] if len(sys.argv) > 1 else 'ru'
core.DEFAULT_CONFIG['server'] = ''
app = core.App()
app.update()
print('language', core.DEFAULT_CONFIG['ui_language'], 'heading', app.tree.heading('#0','text'))
print('new_default_server', core.DEFAULT_CONFIG['server'])
settings = SystemSettingsDialog(app)
app.update()
options = [settings.nav.get(i) for i in range(settings.nav.size())]
print('settings', options)
assert all(x not in options for x in ['Дополнительно', 'Advanced'])
assert 'cluster_uuid' not in settings.v
assert 'platform_bin' not in settings.v
assert 'backup_root' not in settings.v
assert 'Файлы архивов' not in options and 'Backup files' not in options
assert len(settings.PAGES)==7
settings.nav.selection_clear(0, 'end'); settings.nav.selection_set(len(options)-1)
settings._switch()
print('lang selector:', settings.language_var.get())
app.cfg['server'] = 'OLD-LEGACY'
app.cfg['cluster_uuid'] = 'saved-cluster-id'
app.cfg['platform_bin'] = 'C:/old/1cv8/bin'
settings.language_var.set('English')
snap, sec = settings._settings_snapshot()
assert snap['server'] == 'OLD-LEGACY'
assert snap['cluster_uuid'] == 'saved-cluster-id'
assert snap['platform_bin'] == 'C:/old/1cv8/bin'
assert snap['ui_language'] == 'en'
settings.destroy()
job = core.normalize_db({'id':'x1', 'type':'1c', 'ref':'newref', 'ib_ref':'newref', 'name':'Пробная', 'enabled':False, 'connection_kind':'server', 'one_c_server':''})
editor = EffectorTaskEditor(app, job, lambda *args: None)
app.update()
assert editor.v['one_c_server'].get()==''
print('editor page:', editor.nav.get(0), 'source value:',editor.kind_display.get())
if core.DEFAULT_CONFIG['ui_language']=='en':
    assert app.tree.heading('#0','text')=='Name'
    assert editor.nav.get(0)=='Connect to database'
    assert source_value(editor.kind_display.get())=='Клиент-серверная'
    assert options[-1]=='Program language'
editor.destroy()
app.destroy()
print('OK')
