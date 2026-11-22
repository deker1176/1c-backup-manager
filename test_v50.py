import pytest
from file_session_control import (normalize_windows_path, commandline_matches_file_base,
   select_matching_processes, validate_file_disconnect_settings)

BASE=r'C:\Base\unf'

def test_exact():
    assert commandline_matches_file_base(r'"C:\1cv8c.exe" ENTERPRISE /F"C:\Base\unf" /NУправляющий', '1cv8c.exe', [BASE])
def test_normalize():
    assert normalize_windows_path(r'C:/Base/UNF/')==normalize_windows_path(BASE)
def test_other_base():
    assert not commandline_matches_file_base(r'1cv8c.exe /F"C:\Base\buh"', '1cv8c.exe', [BASE])
def test_designer_safe():
    assert not commandline_matches_file_base(r'1cv8.exe DESIGNER /F"C:\Base\unf"', '1cv8.exe', [BASE])
def test_unknown_no_kill():
    assert not commandline_matches_file_base('1cv8c.exe ENTERPRISE /IBName UNF', '1cv8c.exe', [BASE])
def test_alias():
    assert commandline_matches_file_base(r'1cv8c.exe /F"\\server\share\unf"', '1cv8c.exe', [BASE,r'\\server\share\unf'])
def test_process_selection():
    v=[dict(pid=23,name='1cv8c.exe',commandline=r'1cv8c.exe /F"C:\Base\unf"'),dict(pid=24,name='1cv8c.exe',commandline=r'1cv8c.exe /F"C:\Base\buh"')]
    assert [x['pid'] for x in select_matching_processes(v,[BASE])]==[23]
def test_only_confirmed_force():
    with pytest.raises(ValueError,match='Подтвердите'):
        validate_file_disconnect_settings({'file_disconnect_mode':'force_local'})
def test_force_local():
    assert validate_file_disconnect_settings({'file_disconnect_mode':'force_local','file_force_confirmed':True,'file_wait_seconds':20})==('force_local',20,[])
def test_remote_without_hosts():
    with pytest.raises(ValueError):validate_file_disconnect_settings({'file_disconnect_mode':'force_hosts','file_force_confirmed':True})
def test_injection_rejected():
    with pytest.raises(ValueError):validate_file_disconnect_settings({'file_disconnect_mode':'force_hosts','file_force_confirmed':True,'file_session_hosts':'PC1;$(Remove-Item)'})
def test_wait_bounded():
    with pytest.raises(ValueError):validate_file_disconnect_settings({'file_wait_seconds':9999})
