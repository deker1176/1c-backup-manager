"""Safe tests: no real 1C, SQL or FTP connections are made."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ibases_import import read_ibases, parse_connection, item_to_job, parse_ibases_text
import onec_backup_manager as app


class ImportTests(unittest.TestCase):
    def test_real_uploaded_ibases(self):
        src = Path('/mnt/data/ibases.v8i')
        if not src.is_file():
            self.skipTest('User upload only exists in current environment')
        rows = read_ibases(src)
        self.assertEqual(len(rows), 11)
        self.assertEqual({k: sum(x.kind == k for x in rows) for k in ('server','file','web')},
                         {'server': 4, 'file': 3, 'web': 4})
        jobs = []
        for row in rows:
            jobs.append(app.normalize_db(item_to_job(row, jobs)))
        self.assertTrue(all(not j['enabled'] for j in jobs))
        self.assertEqual(len({j['ref'].casefold() for j in jobs}), len(jobs))

    def test_parse_semicolon_in_quotes_and_unicode(self):
        d = parse_connection('Srvr="host;name";Ref="db";')
        self.assertEqual(d['srvr'], 'host;name')
        rows = parse_ibases_text('''[Клиника]\nConnect=Srvr="HOST";Ref="db;prod";\n[demo]\nConnect=File="C:\\1C\\local";\n[WEB]\nConnect=Ws="https://example.org/base";\n''')
        self.assertEqual([x.kind for x in rows], ['server','file','web'])
        self.assertEqual(rows[0].ref, 'db;prod')


class ConfigTests(unittest.TestCase):
    def test_clean_start_and_migration(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(app, 'CONFIG_PATH', Path(d) / 'config.json'):
                cfg = app.load_config()
                self.assertEqual(cfg['databases'], [])
                legacy = {'server':'SRV1C','databases':[{'name':'acc','ref':'acc','enabled':True,'keep':7}]}
                app.CONFIG_PATH.write_text(json.dumps(legacy), encoding='utf-8')
                loaded = app.load_config()
                self.assertEqual(loaded['databases'][0]['name'], 'acc')
                self.assertEqual(loaded['databases'][0]['platform_mode'], 'inherit')

    def test_auto_platform_override_keeps_old_pin_for_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'server':'SRV1C', 'ras_endpoint':'127.0.0.1:1545', 'platform_bin':'OLD/bin',
                   'backup_root':d, 'ftp': {'tls':True}}
            base_job = {'id':'abc','name':'test','ref':'test','ib_ref':'real_ref',
                        'type':'1c','enabled':True,'connection_kind':'server',
                        'one_c_server':'REMOTE1', 'one_c_ras':'REMOTE1:1555'}
            seen = []
            class FakeEngine:
                def __init__(self, cfg, sec, log):
                    seen.append(cfg)
                def prepare(self):
                    pass
                def backup_one(self, job):
                    return Path(d) / 'made.dt'
                def close(self):
                    pass
            with patch.object(app, 'BackupEngine', FakeEngine), patch.object(app, 'finalize_backup', lambda job,src,cfg,sec,log: src):
                app.run_any_backup(cfg, {}, {**base_job, 'platform_mode':'auto'})
                app.run_any_backup(cfg, {}, {**base_job, 'platform_mode':'inherit'})
            self.assertEqual(seen[0]['platform_bin'], '')
            self.assertEqual(seen[1]['platform_bin'], 'OLD/bin')
            self.assertEqual(seen[0]['server'], 'REMOTE1')
            self.assertEqual(seen[0]['ras_endpoint'], 'REMOTE1:1555')
            self.assertEqual(seen[0]['cluster_uuid'], '')

    def test_web_job_refused_without_touching_1c(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {'backup_root': d}
            with self.assertRaisesRegex(RuntimeError,'Ws'):
                app.run_any_backup(cfg, {}, {'type':'1c','connection_kind':'web','ref':'x'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
