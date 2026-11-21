from pathlib import Path
import tempfile
import time
import pytest

import task_runtime as rt
import onec_backup_manager as app
import backup_extensions as ext


def test_runtime_hook_pre_post(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(app, '_run_core_backup', lambda cfg, sec, job, log: order.append('core') or tmp_path/'test.dt')
    def mock_run(cmd, *args, **kwargs):
        order.append('command:' + ('before' if 'echo before' in cmd[-1] else 'after'))
        class Result:
            returncode = 0
            stdout = ''
            stderr = ''
        return Result()
    monkeypatch.setattr(rt.subprocess, 'run', mock_run)
    job={'type':'files','ref':'test','name':'test','commands_enabled':True, 'command_before':'echo before', 'command_after':'echo after'}
    output=app.run_any_backup({'backup_root':str(tmp_path)}, {}, job, lambda x:None)
    assert output == tmp_path/'test.dt'
    assert order == ['command:before','core','command:after']


def test_post_after_failure(monkeypatch, tmp_path):
    phase=[]
    def fail(*args):
        raise ValueError('backup failed')
    monkeypatch.setattr(app, '_run_core_backup', fail)
    def mock_run(cmd, *args, **kwargs):
        phase.append(cmd[-1])
        class Result:
            returncode=0
            stderr=''
        return Result()
    monkeypatch.setattr(rt.subprocess,'run',mock_run)
    job={'type':'files','ref':'j','name':'j','commands_enabled':True, 'command_after':'echo cleanup'}
    with pytest.raises(ValueError, match='backup failed'):
        app.run_any_backup({'backup_root':str(tmp_path)}, {}, job, lambda x:None)
    assert phase == ['echo cleanup']


def test_post_after_timeout(monkeypatch, tmp_path):
    phase=[]
    def fail(cfg,sec,job,log):
        raise TimeoutError('out of time')
    monkeypatch.setattr(app, '_run_core_backup', fail)
    def mock_run(cmd, *args, **kwargs):
        phase.append(cmd[-1])
        class Result:
            returncode=0
            stderr=''
        return Result()
    monkeypatch.setattr(rt.subprocess,'run',mock_run)
    job={'type':'files','ref':'j','name':'j','commands_enabled':True, 'command_after':'echo cleanup',
         'stop_after_enabled':True,'stop_after_hours':0,'stop_after_minutes':1}
    with pytest.raises(TimeoutError):
        app.run_any_backup({'backup_root':str(tmp_path)}, {}, job, lambda x:None)
    assert phase == ['echo cleanup']


def test_ftp_multiple_profiles(tmp_path, monkeypatch):
    source=tmp_path/'j_2026-11-21_01-00-00.dt'
    source.write_bytes(b'1c backup data')
    sent=[]
    monkeypatch.setattr(ext,'ftp_upload', lambda path, cfg, password, log: sent.append((cfg['name'],password)))
    cfg={'ftp_profiles':[{'name':'BP','tls':True},{'name':'BP2','tls':True}]}
    sec={'ftp_profile_passwords':{'BP':'p1','BP2':'p2'}}
    job={'ref':'j','name':'j','ftp_enabled':True,'ftp_targets':['BP','BP2'],
         'archive_format':'none','keep':5}
    assert ext.finalize_backup(job,source,cfg,sec,lambda _:None)==source
    assert sent==[('BP','p1'),('BP2','p2')]
    assert not Path(str(source)+'.pending-ftp').exists()


def test_ftp_one_failed_preserves_pending_archive(tmp_path,monkeypatch):
    source=tmp_path/'j_2026-11-21_01-00-00.dt'
    source.write_bytes(b'1c backup data')
    def upload(path,cfg,password,log):
        if cfg['name']=='BP2':
            raise RuntimeError('FTP lost')
    monkeypatch.setattr(ext,'ftp_upload',upload)
    cfg={'ftp_profiles':[{'name':'BP','tls':True},{'name':'BP2','tls':True}]}
    sec={'ftp_profile_passwords':{}}
    job={'ref':'j','name':'j','ftp_enabled':True,'ftp_targets':['BP','BP2'],
         'archive_format':'none','keep':5}
    with pytest.raises(RuntimeError, match='FTP lost'):
        ext.finalize_backup(job,source,cfg,sec,lambda _:None)
    assert source.exists()
    assert Path(str(source)+'.pending-ftp').exists()


def test_task_limit_validation():
    with pytest.raises(ValueError):
        with rt.task_deadline({'stop_after_enabled':True,'stop_after_hours':0,'stop_after_minutes':0}):
            pass


def test_folder_compatibility():
    job=app.normalize_db({'ref':'old','name':'old','enabled':True})
    assert job.get('task_group','')==''
    assert job['ftp_targets']==[]
