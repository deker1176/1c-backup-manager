"""v5.2: configuration-independent diagnostics, no unsafe IIS process kills."""
from file_lock_diagnostics import (norm_path, publication_uses_file_base,
    find_iis_publications, rm_locking_pids, smb_open_files_for_base)
from file_session_control import select_matching_processes

BASE = r"C:\Base\unf"
VRD = r'<point base="/unf" ib="File=&quot;C:\Base\unf&quot;;"/>'
APPS = '\n'.join((
    'APP "Default Web Site/" (applicationPool:DefaultAppPool)',
    'APP "Default Web Site/unf" (applicationPool:DefaultAppPool)',
    'APP "Default Web Site/buh" (applicationPool:OtherPool)',
))
VDIRS = '\n'.join((
    'VDIR "Default Web Site/" (physicalPath:C:\\inetpub\\wwwroot)',
    'VDIR "Default Web Site/unf/" (physicalPath:C:\\inetpub\\wwwroot\\unf)',
    'VDIR "Default Web Site/buh/" (physicalPath:C:\\inetpub\\wwwroot\\buh)',
))


def test_vrd_parses_xml_escaped_connection_string():
    assert publication_uses_file_base(VRD, BASE)
    assert publication_uses_file_base(VRD, r"c:/base/UNF/")
    assert not publication_uses_file_base(VRD, r"C:\Base\buh")
    assert not publication_uses_file_base('<point ib="Srvr=&quot;server&quot;;Ref=&quot;unf&quot;;"/>', BASE)
    assert not publication_uses_file_base('<not xml', BASE)


def test_discover_iis_shared_pool_without_modifying_it():
    def read(path):
        if path.lower().endswith(r"\unf\default.vrd"):
            return VRD
        return r'<point ib="File=&quot;C:\Base\buh&quot;;"/>'
    matches = find_iis_publications(BASE, APPS, VDIRS, read)
    assert len(matches) == 1
    assert matches[0]["app"] == "Default Web Site/unf"
    assert matches[0]["pool"] == "DefaultAppPool"
    assert matches[0]["shared"]
    assert matches[0]["pool_apps"] == ["Default Web Site/", "Default Web Site/unf"]


def test_discover_iis_dedicated_pool():
    apps = APPS.replace('APP "Default Web Site/unf" (applicationPool:DefaultAppPool)',
                        'APP "Default Web Site/unf" (applicationPool:OneC_UNF)')
    pubs = find_iis_publications(BASE, apps, VDIRS, lambda _: VRD)
    assert len(pubs) == 1
    assert not pubs[0]["shared"]


def test_rm_pid_can_identify_local_client_with_no_file_argument():
    processes = [
        dict(pid=42, name="1cv8c.exe", commandline="1cv8c.exe ENTERPRISE /IBName UNF"),
        dict(pid=43, name="1cv8.exe", commandline=r'1cv8.exe DESIGNER /F"C:\Base\unf"'),
        dict(pid=44, name="w3wp.exe", commandline="w3wp.exe"),
        dict(pid=45, name="1cv8c.exe", commandline=r'1cv8c.exe /F"C:\Base\buh"'),
    ]
    selected = select_matching_processes(processes, [BASE], locked_pids={42, 43, 44})
    assert [p["pid"] for p in selected] == [42]


def test_do_not_kill_unrelated_client_based_on_restart_manager_other_pid():
    processes = [
        dict(pid=1, name="1cv8c.exe", commandline="1cv8c.exe /IBName Buh"),
        dict(pid=2, name="w3wp.exe", commandline="w3wp.exe"),
    ]
    assert select_matching_processes(processes, [BASE], locked_pids={2}) == []


def test_rm_and_smb_are_inert_on_non_windows():
    import os
    if os.name != "nt":
        assert rm_locking_pids(r"Z:\unknown\1Cv8.1CD") == set()
        assert smb_open_files_for_base(BASE) == []
