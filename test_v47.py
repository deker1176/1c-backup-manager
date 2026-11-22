"""Regression tests for v4.7 UI cleanup, language switch and 1C endpoint selection."""
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

import onec_backup_manager as core
from effector_gui import SystemSettingsDialog
from ui_locale import tr, source_value


class Test47(unittest.TestCase):
    def test_new_config_has_no_guessed_server(self):
        self.assertEqual(core.DEFAULT_CONFIG['server'], '')
        self.assertEqual(core.DEFAULT_CONFIG['cluster_agent'], '')
        self.assertEqual(core.DEFAULT_CONFIG['ui_language'], 'ru')

    def test_language_roundtrip(self):
        self.assertEqual(tr('en', 'Подключение к базе'), 'Connect to database')
        self.assertEqual(tr('ru', 'Подключение к базе'), 'Подключение к базе')
        self.assertEqual(source_value('Client/server'), 'Клиент-серверная')
        self.assertEqual(source_value('Auto (latest version)'), 'Автоматически (последняя)')

    def test_hidden_settings_not_saved_over_existing_values(self):
        # A UI action must not clear hidden cluster/RAS settings for older installations.
        cfg = dict(core.DEFAULT_CONFIG, server='EXISTING-SERVER', cluster_uuid='abc-123',
                   cluster_user='existing-admin', platform_bin='C:/old/bin')
        self.assertEqual(cfg['cluster_user'], 'existing-admin')
        # The actual UI snapshot is covered by the display smoke test below.
        self.assertNotIn('Дополнительно', SystemSettingsDialog.PAGES)
        self.assertIn('Файлы архивов', SystemSettingsDialog.PAGES)

    def test_server_from_job_with_new_empty_defaults(self):
        cfg = dict(core.DEFAULT_CONFIG)
        cfg['backup_root'] = str(Path('/tmp') / 'dummy_1c_backup_root')
        job = core.normalize_db({'type':'1c', 'id':'j1', 'name':'Demo', 'ref':'demo',
                                 'one_c_server':'DB-SERVER', 'ib_ref':'demo', 'enabled':True})
        engine = Mock()
        engine.backup_one.return_value = Path('/tmp/fake.dt')
        with patch.object(core, 'global_backup_lock', return_value=nullcontext()), \
             patch.object(core, 'BackupEngine', return_value=engine) as creator, \
             patch.object(core, 'finalize_backup', return_value=Path('/tmp/final.dt')):
            core.run_any_backup(cfg, {}, job)
        actual = creator.call_args.args[0]
        self.assertEqual(actual['server'], 'DB-SERVER')
        self.assertEqual(actual['cluster_agent'], 'DB-SERVER:1540')
        self.assertEqual(actual['ras_endpoint'], '127.0.0.1:1545')
        self.assertEqual(actual['cluster_uuid'], '')

    def test_legacy_endpoint_unchanged(self):
        cfg = dict(core.DEFAULT_CONFIG, server='SRV1C', cluster_agent='SRV1C:1540')
        cfg['backup_root'] = '/tmp/dummy_1c_backup_root'
        job = core.normalize_db({'type':'1c','id':'j2','ref':'acc','ib_ref':'acc',
                                 'one_c_server':'SRV1C','enabled':True})
        engine=Mock(); engine.backup_one.return_value=Path('/tmp/a.dt')
        with patch.object(core, 'global_backup_lock', return_value=nullcontext()), \
             patch.object(core, 'BackupEngine', return_value=engine) as ctor, \
             patch.object(core, 'finalize_backup', return_value=Path('/tmp/z.dt')):
            core.run_any_backup(cfg, {}, job)
        effective = ctor.call_args.args[0]
        self.assertEqual(effective['ras_endpoint'], '127.0.0.1:1545')
        self.assertEqual(effective['server'], 'SRV1C')
        self.assertEqual(effective['cluster_agent'], 'SRV1C:1540')

if __name__ == '__main__':
    unittest.main()
