from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest

import onec_backup_manager as app
import report_delivery as delivery
import task_runtime


def test_task_defaults_are_opt_in():
    for kind in ['1c','mssql','files','commands','report','extra_copy']:
        j=app.normalize_db({'type':kind,'name':'Example','ref':'e'})
        assert not j['notify_email']
        assert not j['notify_telegram']
        assert not j['notify_on_success']
        assert not j['notify_on_failure']


def test_command_task_dispatch(monkeypatch, tmp_path):
    calls=[]
    monkeypatch.setattr(app,'run_commands',lambda body,phase,job,log: calls.append((body,phase)))
    got=app.run_any_backup({'backup_root':str(tmp_path)},{},
                           {'type':'commands','name':'c','ref':'c','command_body':'echo okay'},lambda _:None)
    assert got is None
    assert calls==[('echo okay','Произвольные команды')]


def test_report_task_dispatch_without_backup(monkeypatch,tmp_path):
    seen=[]
    monkeypatch.setattr(delivery,'send_summary_task', lambda cfg,sec,job,state,log: seen.append(job['ref']))
    got=app.run_any_backup({'backup_root':str(tmp_path)},{},
                           {'type':'report','name':'report','ref':'report','notify_telegram':True},lambda _:None)
    assert got is None
    assert seen==['report']


def test_extra_copy_uses_existing_file_backup(monkeypatch,tmp_path):
    calls=[]
    out=tmp_path/'file.zip'
    monkeypatch.setattr(app,'backup_files',lambda job,cfg,sec,log: calls.append(job['ref']) or out)
    got=app.run_any_backup({'backup_root':str(tmp_path)},{},
                           {'type':'extra_copy','ref':'x','name':'extra'},lambda _:None)
    assert got==out and calls==['x']


def test_notifications_only_on_selected_result(monkeypatch):
    sends=[]
    monkeypatch.setattr(delivery,'send_channels',lambda cfg,sec,subject,body,**kw: sends.append((subject,kw)))
    j={'name':'db','ref':'db','notify_email':True,'notify_telegram':False,
       'notify_on_success':False,'notify_on_failure':True}
    delivery.notify_task_result({}, {}, j, True)
    assert sends==[]
    delivery.notify_task_result({}, {}, j, False, error='timeout')
    assert len(sends)==1 and sends[0][1]['email'] is True
    assert sends[0][1]['telegram'] is False


def test_notification_failure_does_not_erase_completed_backup(monkeypatch,tmp_path):
    from report_delivery import notify_task_result
    monkeypatch.setattr(app,'run_any_backup', lambda cfg,sec,job,log: tmp_path/'ok.dt')
    recorded=[]
    monkeypatch.setattr(app,'record_backup_result',lambda job,success,**kw:recorded.append(success))
    def fail(*args,**kwargs):
        raise TimeoutError('SMTP not reachable')
    monkeypatch.setattr(delivery,'notify_task_result',fail)
    result=app.execute_job({'backup_root':str(tmp_path)}, {}, {'ref':'db','name':'db','type':'1c'}, lambda _:None)
    assert result==tmp_path/'ok.dt'
    assert recorded==[True]


def test_report_summary_uses_stable_task_id_when_present():
    j=[{'id':'unique-task-123','ref':'acc','name':'Accounting'}]
    _, text=delivery.build_summary(j,{'unique-task-123':{'last_result':'ok','last_attempt_time':'2026-11-21 02:00:00'}})
    assert 'Accounting: Успешно' in text


def test_report_summary_uses_existing_state_only():
    j=[{'ref':'acc','name':'Accounting'}, {'ref':'hrm','name':'Payroll'}]
    subject, text=delivery.build_summary(j,{'acc':{'last_attempt_time':'2026-11-20 02:00:00','last_result':'ok'}})
    assert 'Accounting: Успешно' in text
    assert 'Payroll: Нет данных' in text
    assert '2026-11-20 02:00:00' in text


def test_email_smtp_uses_tls(monkeypatch):
    actions=[]
    class SMTP:
        def __init__(self,host,port,timeout=None):
            actions.append(('connect',host,port))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def ehlo(self): actions.append('ehlo')
        def starttls(self,context=None): assert context; actions.append('starttls')
        def login(self,user,passw): actions.append(('login',user,passw))
        def send_message(self,msg): actions.append(('send',msg['Subject']))
    monkeypatch.setattr(delivery.smtplib,'SMTP', SMTP)
    delivery._send_smtp({'host':'smtp.example.com','port':587,'security':'STARTTLS','user':'backup',
                         'from':'a@example.com','to':'b@example.com'},'password','hello','body')
    assert 'starttls' in actions
    assert ('login','backup','password') in actions
    assert ('send','hello') in actions


def test_telegram_chat_id_is_required():
    with pytest.raises(ValueError,match='Chat ID'):
        delivery._send_telegram({'chat_id':''},'test-token','body')


def test_remote_report_strict_rejects_no_channel():
    with pytest.raises(ValueError,match='выберите'):
        delivery.send_channels({}, {}, 'test','body',email=False,telegram=False,strict=True)


def test_journal_cleanup_does_not_touch_backups(tmp_path):
    logs=tmp_path/'logs'
    logs.mkdir()
    old_time=(datetime.now()-timedelta(days=90)).timestamp()
    for name in ['service_old.log','manager_old.log','1c_important.log','copy.zip']:
        p=logs/name
        p.write_text('x',encoding='utf-8')
        import os
        os.utime(p, (old_time,old_time))
    deleted=app.prune_manager_logs({'backup_root':str(tmp_path),'journal_retention_days':60},lambda _:None)
    assert deleted==2
    assert (logs/'1c_important.log').exists()
    assert (logs/'copy.zip').exists()


def test_compat_config_still_has_empty_list_and_single_ftp():
    assert app.DEFAULT_CONFIG['databases']==[]
    assert app.DEFAULT_CONFIG['ftp']['tls'] is True
    assert app.DEFAULT_CONFIG['ui_close_action']=='ask'
    assert app.DEFAULT_CONFIG['ui_minimize_to_tray']


def test_new_notification_secrets_round_trip_without_real_dpapi(monkeypatch,tmp_path):
    monkeypatch.setattr(app,'SECRETS_PATH',tmp_path/'secrets.dat')
    monkeypatch.setattr(app,'dpapi_encrypt',lambda value: 'ENC:' + value if value else '')
    monkeypatch.setattr(app,'dpapi_decrypt',lambda value: value.removeprefix('ENC:') if value else '')
    app.save_secrets('clustersecret',{'base':'ibsecret'},smtp_password='mail-secret',telegram_bot_token='bot-secret')
    contents=(tmp_path/'secrets.dat').read_text(encoding='utf-8')
    import json
    wrapped=json.loads(contents)
    assert wrapped['smtp_password']=='ENC:mail-secret'
    assert wrapped['telegram_bot_token']=='ENC:bot-secret' 
    loaded=app.load_secrets()
    assert loaded['cluster_password']=='clustersecret'
    assert loaded['db_passwords']['base']=='ibsecret'
    assert loaded['smtp_password']=='mail-secret'
    assert loaded['telegram_bot_token']=='bot-secret'


def test_old_secrets_without_notification_fields_still_load(monkeypatch,tmp_path):
    import json
    monkeypatch.setattr(app,'SECRETS_PATH',tmp_path/'secrets.dat')
    monkeypatch.setattr(app,'dpapi_decrypt',lambda value: value)
    (tmp_path/'secrets.dat').write_text(json.dumps({'cluster_password':'oldpwd','db_passwords':{'acc':'ibpwd'}}))
    loaded=app.load_secrets()
    assert loaded['cluster_password']=='oldpwd'
    assert loaded['db_passwords']['acc']=='ibpwd'
    assert loaded['smtp_password']==''
    assert loaded['telegram_bot_token']==''
