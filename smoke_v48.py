"""Smoke-test v4.8 MAX settings and task controls on a virtual display."""
import onec_backup_manager as core
from effector_gui import SystemSettingsDialog, EffectorTaskEditor

app=core.App()
app.withdraw()
app.update()
settings=SystemSettingsDialog(app)
app.update()
assert 'Отчеты' in [settings.nav.get(i) for i in range(settings.nav.size())]
settings.max_recipient_type.set('user_id')
settings.max_recipient_id.set('987654321')
settings.max_token.set('NONREAL-TEST-TOKEN')
cfg,sec=settings._settings_snapshot()
assert cfg['notifications']['max']=={'recipient_type':'user_id','recipient_id':'987654321'}
assert sec['max_bot_token']=='NONREAL-TEST-TOKEN'
settings.destroy()
job=core.normalize_db({'name':'Сводка MAX','ref':'maxreport','type':'report','enabled':False})
editor=EffectorTaskEditor(app, job, lambda *_:None)
app.update()
assert not editor.v['notify_max'].get()
editor.v['notify_max'].set(True)
assert editor.extract()['notify_max']
editor.destroy()
app.destroy()
print('v4.8 MAX GUI smoke test OK')
