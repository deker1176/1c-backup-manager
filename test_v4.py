from pathlib import Path
import contextlib
import sys
import os
import types
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_extensions as ext
import onec_backup_manager as main


def test_old_v3_db_config_migrates_without_losing_auth():
    legacy = {"enabled": True, "name": "acc", "ref": "acc", "ib_user": "Админ",
              "keep": 7, "schedule_time": "02:00", "folder": ""}
    normalized = main.normalize_db(legacy)
    assert normalized["type"] == "1c"
    assert normalized["archive_format"] == "none"
    assert normalized["ref"] == "acc" and normalized["ib_user"] == "Админ"
    assert main.db_state_key(normalized) == "acc"


def test_scheduler_skipped_task_catches_up_same_day():
    from datetime import datetime
    db = {"name": "unf", "ref": "unf", "type": "1c", "enabled": True,
          "schedule_time": "02:30"}
    now = datetime(2026, 11, 21, 3, 55)
    assert main.should_run_now(db, {}, now)
    assert not main.should_run_now(db, {"unf": {"last_scheduled_date": "2026-11-21"}}, now)


def test_zip_files_tree_paths_and_verify(tmp_path):
    a = tmp_path / "source"
    a.mkdir()
    (a / "doc.txt").write_text("test", encoding="utf8")
    (a / "sub").mkdir()
    (a / "sub" / "data.bin").write_bytes(b"\x00\x01" * 100)
    target = tmp_path / "output" / "files_2026-11-21_10-00-00.zip"
    result = ext.pack_files([str(a)], target, "zip", forbidden=[target.parent])
    assert result.exists()
    with zipfile.ZipFile(result) as z:
        assert set(z.namelist()) == {"source/doc.txt", "source/sub/data.bin"}
        assert z.read("source/doc.txt") == b"test"
    ext.verify_archive(result, "zip")


def test_no_backup_folder_inside_source(tmp_path):
    src = tmp_path / "files"
    src.mkdir()
    (src / "test.txt").write_text("hi", encoding="utf8")
    sub = src / "BACKUP"
    sub.mkdir()
    with pytest.raises(RuntimeError, match="внутри источника"):
        ext.pack_files([str(src)], sub / "test.zip", "zip", forbidden=[sub])


def test_missing_source_does_not_leave_partial(tmp_path):
    target = tmp_path / "no.zip"
    with pytest.raises(RuntimeError, match="не найден"):
        ext.pack_files([str(tmp_path / "missing")], target, "zip")
    assert not target.with_name(target.name + ".partial").exists()


def test_collision_is_not_silently_overwritten(tmp_path):
    x = tmp_path / 'one' / 'dup.txt'
    y = tmp_path / 'two' / 'dup.txt'
    x.parent.mkdir()
    y.parent.mkdir()
    x.write_text('x')
    y.write_text('y')
    with pytest.raises(RuntimeError, match="Одинаковые пути"):
        ext.pack_files([str(x), str(y)], tmp_path/'dup.zip', 'zip')


def test_retention_keeps_backup_sets_and_pending_ftp(tmp_path):
    folder = tmp_path / 'backup'
    folder.mkdir()
    stems = [f'acc_2026-11-{d:02d}_00-00-00' for d in (18, 19, 20, 21)]
    for stem in stems:
        for extname in ['.dt', '.zip']:
            p = folder / (stem + extname)
            p.write_bytes(b'archive')
    newest = folder / (stems[-1] + '.zip')
    older = folder / (stems[0] + '.zip')
    older.with_name(older.name+'.pending-ftp').write_text('upload pending')
    ext.prune_local({'ref':'acc','name':'acc'}, newest, keep=2, log=lambda *_: None)
    assert newest.exists()
    assert older.exists()  # failed upload must never be silently purged
    assert (folder / (stems[0]+'.dt')).exists()
    assert not (folder / (stems[1]+'.zip')).exists()
    assert not (folder / (stems[1]+'.dt')).exists()


def test_pack_single_keeps_original_and_hash(tmp_path):
    src = tmp_path / 'acc_2026-11-21_02-00-00.dt'
    src.write_bytes(b'example dt' * 200)
    result = ext.finalize_backup({'name':'acc','ref':'acc','keep':7, 'archive_format':'zip',
                                  'ftp_enabled':False}, src,
                                 {'backup_root':str(tmp_path)}, {}, log=lambda *_: None)
    assert result.suffix=='.zip' and result.exists()
    assert src.exists()
    assert Path(str(result)+'.sha256').exists()


def test_ftp_failure_keeps_local_backup(tmp_path, monkeypatch):
    src = tmp_path / 'acc_2026-11-21_02-00-00.dt'
    src.write_bytes(b'test' * 200)
    def fail(*a, **kw):
        raise OSError('FTP offline')
    monkeypatch.setattr(ext, 'ftp_upload', fail)
    with pytest.raises(OSError, match='FTP offline'):
        ext.finalize_backup({'ref':'acc','keep':1,'archive_format':'zip',
                             'remove_raw_after_pack':True,'ftp_enabled':True}, src,
                            {'backup_root':str(tmp_path), 'ftp':{}}, {'ftp_password':'x'},
                            log=lambda *_:None)
    assert src.exists()
    packed = src.with_suffix('.zip')
    assert packed.exists()
    assert packed.with_name(packed.name+'.pending-ftp').exists()


def test_ftp_atomic_upload(tmp_path, monkeypatch):
    class FakeFTP:
        def __init__(self):
            self.files={}
            self.events=[]
        def storbinary(self, cmd, stream, blocksize):
            self.events.append(('STOR',cmd))
            self.files[cmd[5:]] = stream.read()
        def size(self,name):
            return len(self.files[name])
        def rename(self,old,new):
            self.events.append(('RENAME',old,new))
            self.files[new]=self.files.pop(old)
        def cwd(self,path):pass
        def mkd(self,path):pass
    ftp = FakeFTP()
    @contextlib.contextmanager
    def connect(*args,**kwargs):
        yield ftp
    monkeypatch.setattr(ext,'ftp_connection', connect)
    p = tmp_path/'test.zip'
    p.write_bytes(b'hello')
    ext.ftp_upload(p,{'host':'ftp.example','port':21,'path':'/test'}, 'test', log=lambda *_:None)
    assert ftp.files['test.zip']==b'hello'
    assert ftp.events[0][0]=='STOR'
    assert ftp.events[1][0]=='RENAME'


def test_mssql_query_uses_backup_and_verifyonly(tmp_path, monkeypatch):
    class FakeCursor:
        def __init__(self): self.queries=[]
        def execute(self,sql):
            self.queries.append(sql)
            if sql.startswith('BACKUP'):
                import re
                dest = re.search(r"TO DISK = N'([^']+)'", sql).group(1)
                Path(dest).write_bytes(b'backup-data')
        def nextset(self):return False
        def close(self): pass
    class FakeConn:
        def __init__(self):self.cur=FakeCursor()
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self):return self.cur
    conn=FakeConn()
    monkeypatch.setitem(sys.modules,'pyodbc',types.SimpleNamespace(connect=lambda *a,**k:conn))
    job={'type':'mssql','name':'SQL', 'ref':'sql_test', 'sql_database':'Test]DB',
         'sql_server':'.', 'sql_backup_dir':str(tmp_path), 'sql_windows_auth':True}
    result=ext.backup_mssql(job, {}, {'backup_root':str(tmp_path)}, log=lambda *_:None)
    assert result.exists()
    assert 'COPY_ONLY, CHECKSUM' in conn.cur.queries[0]
    assert '[Test]]DB]' in conn.cur.queries[0]
    assert 'RESTORE VERIFYONLY' in conn.cur.queries[1]


def test_lock_prevents_parallel_start(tmp_path):
    with ext.global_backup_lock(tmp_path):
        with pytest.raises(RuntimeError,match='уже выполняется'):
            with ext.global_backup_lock(tmp_path):
                pass
