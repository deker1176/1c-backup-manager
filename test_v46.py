"""Safe integration/unit tests for new 1C8 task kinds (no real 1C needed)."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import onec_backup_manager as backend
import onec_tasks_8 as tasks
from effector_gui import TaskTypeDialog


def test_1c77_is_not_in_wizard():
    kinds = [type_ for _, _, type_ in TaskTypeDialog.CHOICES]
    labels = [label for _, label, _ in TaskTypeDialog.CHOICES]
    assert "1c_check" in kinds and "1c_procedure" in kinds
    assert not any("7.7" in x for x in labels)


def test_defaults_check_do_not_modify_base():
    job = backend.normalize_db({"type":"1c_check", "name":"Test", "ref":"test", "enabled": False})
    flags = tasks.build_check_options(job)
    assert "-TestOnly" in flags
    assert "-ReIndex" in flags and "-RecalcTotals" in flags
    assert "-BadDataDelete" not in flags and "-BadRefClear" not in flags


def test_check_logical_and_refs_mutually_exclusive():
    job = backend.normalize_db({"type": "1c_check", "check_refs": True})
    flags = tasks.build_check_options(job)
    assert "-LogAndRefsIntegrity" in flags and "-LogIntegrity" not in flags


def test_repair_requires_confirmation():
    job = backend.normalize_db({"type": "1c_check", "check_mode": "repair"})
    with pytest.raises(RuntimeError, match="подтвердить"):
        tasks.build_check_options(job)


def test_repair_accepts_safe_reference_policy_and_explicit_data_choice():
    job = backend.normalize_db({"type": "1c_check", "check_mode": "repair", "repair_confirmed": True,
                                "repair_bad_refs": "none", "repair_bad_data": "none"})
    flags = tasks.build_check_options(job)
    assert "-BadRefNone" in flags and "-TestOnly" not in flags
    assert "-BadDataDelete" not in flags


def test_file_repair_refuses_when_offline_not_confirmed():
    with TemporaryDirectory() as d:
        base = Path(d)/"base"
        base.mkdir()
        (base/"1Cv8.1CD").write_bytes(b"fake")
        job = backend.normalize_db({"type":"1c_check", "connection_kind": "file", "one_c_file": str(base),
                                    "check_mode":"repair", "repair_confirmed":True})
        with patch.object(tasks, "platform_exe", return_value=Path(d)/"1cv8.exe"):
            with pytest.raises(RuntimeError, match="все пользователи"):
                tasks.run_1c_check({"backup_root": d}, {}, job, backend, lambda x: None)


class FakeEngine:
    active = []
    def __init__(self, cfg, sec, log):
        self.__class__.active.append(self)
        self.locked = False
        self.unlocked = False
    def prepare(self): pass
    def get_cluster_uuid(self): return 'cluster'
    def find_infobase(self, clu, ref): return 'ib', {}
    def get_infobase_info(self, clu, ib, job): return {'sessions-deny': 'off', 'scheduled-jobs-deny': 'on'}
    def block_infobase(self, clu, ib, job, code): self.locked = True
    def wait_and_kick(self, clu, ib): pass
    def unblock_infobase(self, clu, ib, job, scheduled_was_denied=False):
        self.unlocked = True
        assert scheduled_was_denied is True
    def close(self): pass


def test_check_unlocks_on_1c_error_even_when_job_fails():
    FakeEngine.active.clear()
    with TemporaryDirectory() as d, patch.object(tasks, 'platform_exe', return_value=Path(d)/'1cv8.exe'), \
         patch.object(backend, 'BackupEngine', FakeEngine), patch.object(tasks, 'run_1cv8', side_effect=RuntimeError('1C failed')):
        job = backend.normalize_db({'type':'1c_check','connection_kind':'server',
                                    'one_c_server':'srv', 'ib_ref':'demo','ref':'demo'})
        with pytest.raises(RuntimeError, match='1C failed'):
            tasks.run_1c_check({'backup_root': d, 'server':'srv'}, {}, job, backend, lambda s: None)
        assert FakeEngine.active[-1].locked and FakeEngine.active[-1].unlocked


def test_repair_makes_copy_before_modification():
    FakeEngine.active.clear()
    events = []
    def fake_copy(*args, **kwargs):
        events.append('copy')
        return Path('/dummy.dt')
    def fake_run(*args, **kwargs):
        events.append('repair')
        assert '-TestOnly' not in args[0]
        assert '-BadRefNone' in args[0]
        return Path('/dummy.log')
    with TemporaryDirectory() as d, patch.object(tasks, 'platform_exe', return_value=Path(d)/'1cv8.exe'), \
         patch.object(backend, 'BackupEngine', FakeEngine), patch.object(tasks, 'run_1cv8', side_effect=fake_run), \
         patch.object(tasks, 'verified_repair_copy', side_effect=fake_copy):
        job = backend.normalize_db({'type':'1c_check','connection_kind':'server',
                                    'one_c_server':'srv','ib_ref':'demo','ref':'demo',
                                    'check_mode':'repair','repair_confirmed':True})
        tasks.run_1c_check({'backup_root':d,'server':'srv'}, {}, job, backend, lambda s: None)
        assert events == ['copy', 'repair']
        assert FakeEngine.active[-1].unlocked


def test_procedure_requires_supplied_epf():
    with TemporaryDirectory() as d, patch.object(tasks, 'platform_exe', return_value=Path(d)/'1cv8.exe'):
        job = backend.normalize_db({'type':'1c_procedure','one_c_server':'srv','ib_ref':'demo','ref':'demo'})
        with pytest.raises(RuntimeError, match=r'\.epf'):
            tasks.run_1c_procedure({'backup_root':d}, {}, job, backend, lambda s: None)


def test_procedure_receives_execute_and_c_parameter_and_does_not_lock_by_default():
    with TemporaryDirectory() as d:
        epf = Path(d)/'demo.epf'
        epf.write_bytes(b'test')
        params = {}
        def run(args, *other, **kwargs):
            params['args'] = args
            return Path(d)/'out.log'
        with patch.object(tasks,'platform_exe',return_value=Path(d)/'1cv8.exe'), patch.object(tasks,'run_1cv8',side_effect=run), \
             patch.object(tasks,'_with_ras_lock',side_effect=AssertionError('Should not lock')):
            job = backend.normalize_db({'type':'1c_procedure','one_c_server':'srv', 'ib_ref':'demo','ref':'demo',
                                        'procedure_epf': str(epf), 'procedure_parameter':'sync=1'})
            tasks.run_1c_procedure({'backup_root':d}, {}, job, backend, lambda s:None)
            args = params['args']
            assert args[1] == 'ENTERPRISE' and '/Execute' in args and str(epf) in args
            assert args[args.index('/C')+1] == 'sync=1'


def test_check_cli_option_time_limit():
    job=backend.normalize_db({'type':'1c_check','check_time_hours':2,'check_time_minutes':30})
    assert '-TimeLimit:002:30' in tasks.build_check_options(job)


def test_file_only_checks_missing_base_before_invoking_1c():
    job={'connection_kind':'file','one_c_file':'Z:/nonexistent_base'}
    with pytest.raises(RuntimeError, match='не найдена'):
        tasks.connection_args(job)
