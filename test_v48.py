"""MAX Bot API integration tests: no live credentials or network access."""
from __future__ import annotations
import json
import urllib.parse
import pytest

import onec_backup_manager as app
import report_delivery as delivery


def test_default_max_channel_disabled_and_compatible():
    assert app.DEFAULT_CONFIG['notifications']['max'] == {'recipient_type':'chat_id','recipient_id':''}
    j = app.normalize_db({'name':'db','ref':'db','type':'1c'})
    assert j['notify_max'] is False
    assert j['notify_email'] is False
    assert j['notify_telegram'] is False


@pytest.mark.parametrize('kind', ['chat_id', 'user_id'])
def test_max_sends_to_correct_id_with_token_in_header_not_url(monkeypatch, kind):
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n): return b'{"message":{"mid":"fake-123"}}'
    def fake_urlopen(req, timeout, context):
        assert timeout == 20 and context is not None
        captured.append(req)
        return Response()
    monkeypatch.setattr(delivery.urllib.request, 'urlopen', fake_urlopen)
    delivery._MAX_LAST_SENT.clear()
    delivery._send_max({'recipient_type':kind,'recipient_id':'123456'}, 'secret-token', 'Привет из 1С')
    req = captured[0]
    assert req.get_method() == 'POST'
    assert req.full_url == f'https://platform-api2.max.ru/messages?{kind}=123456'
    assert 'secret-token' not in req.full_url
    assert req.get_header('Authorization') == 'secret-token'
    assert 'application/json' in req.get_header('Content-type')
    assert json.loads(req.data.decode('utf-8')) == {'text':'Привет из 1С'}


@pytest.mark.parametrize('settings,token,match', [
    ({'recipient_type':'chat_id','recipient_id':''},'valid','ID получателя'),
    ({'recipient_type':'chat_id','recipient_id':'id_ABC'},'valid','целым числом'),
    ({'recipient_type':'anything','recipient_id':'25'},'valid','выберите'),
    ({'recipient_type':'chat_id','recipient_id':'25'},'','Bot Token'),
])
def test_max_rejects_bad_input_before_network(settings,token,match):
    with pytest.raises(ValueError, match=match):
        delivery._send_max(settings,token,'Hello')


def test_max_4000_character_limit_and_200_success_payload(monkeypatch):
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n): return b'{"message":{"mid":"x"}}'
    def fake_urlopen(req, timeout, context):
        seen.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr(delivery.urllib.request, 'urlopen', fake_urlopen)
    delivery._MAX_LAST_SENT.clear()
    delivery._send_max({'recipient_type':'chat_id','recipient_id':'1'},'secret','Т' * 7000)
    assert len(seen[0]['text']) == 4000


def test_max_token_does_not_leak_in_errors(monkeypatch):
    import io, urllib.error
    def fail(req, timeout, context):
        raise urllib.error.HTTPError(req.full_url, 401, 'unauthorized', {}, io.BytesIO(b'token: supersecret'))
    monkeypatch.setattr(delivery.urllib.request,'urlopen', fail)
    delivery._MAX_LAST_SENT.clear()
    with pytest.raises(RuntimeError, match='401') as exc:
        delivery._send_max({'recipient_type':'chat_id','recipient_id':'2'},'supersecret','hello')
    assert 'supersecret' not in str(exc.value)


def test_max_only_report_task_and_failure_do_not_change_existing_backup_status(monkeypatch):
    seen = []
    def send_max(settings, token, body):
        seen.append(body)
    monkeypatch.setattr(delivery,'_send_max', send_max)
    cfg = {'notifications': {'max': {'recipient_type':'chat_id','recipient_id':'345'}}}
    sec = {'max_bot_token':'token'}
    logs = []
    assert delivery.send_channels(cfg,sec,'subject','test',email=False,telegram=False,
                                  max_enabled=True,strict=True,log=logs.append)
    assert seen == ['test']
    assert any('MAX отправлено' in x for x in logs)

    job = {'name':'my 1c','ref':'acc','notify_max':True,
           'notify_on_success':True,'notify_on_failure':False}
    delivery.notify_task_result(cfg,sec,job,True,artifact='acc_2026.dt',log=logs.append)
    assert 'acc_2026.dt' in seen[-1]
    delivery.notify_task_result(cfg,sec,job,False,error='failed',log=logs.append)
    assert len(seen) == 2


def test_max_failure_non_strict_keeps_backup_success(monkeypatch):
    def fail(*a,**kw): raise RuntimeError('bad network')
    monkeypatch.setattr(delivery,'_send_max',fail)
    logs = []
    ok = delivery.send_channels({}, {}, 'subject','body',email=False,telegram=False,
                                max_enabled=True, strict=False, log=logs.append)
    assert ok is False
    assert any('MAX' in s for s in logs)
    with pytest.raises(RuntimeError, match='MAX'):
        delivery.send_channels({}, {}, 'subject','body',email=False,telegram=False,
                               max_enabled=True, strict=True, log=lambda m: None)


def test_max_token_encrypted_in_dpapi_secret_store(monkeypatch,tmp_path):
    monkeypatch.setattr(app,'SECRETS_PATH',tmp_path/'secrets.dat')
    monkeypatch.setattr(app,'dpapi_encrypt',lambda value: 'ENC:'+value if value else '')
    monkeypatch.setattr(app,'dpapi_decrypt',lambda value: value.removeprefix('ENC:') if value else '')
    app.save_secrets('clusterpass',{'acc':'ibpass'},smtp_password='smtp',telegram_bot_token='telegram',max_bot_token='MAX-SECRETTOKEN')
    raw=(tmp_path/'secrets.dat').read_text(encoding='utf-8')
    assert 'ENC:MAX-SECRETTOKEN' in raw and '"max_bot_token": "MAX-SECRETTOKEN"' not in raw
    loaded = app.load_secrets()
    assert loaded['max_bot_token'] == 'MAX-SECRETTOKEN'
    assert loaded['smtp_password'] == 'smtp'
    assert loaded['telegram_bot_token'] == 'telegram'


def test_legacy_secret_file_gets_empty_max_token(monkeypatch,tmp_path):
    monkeypatch.setattr(app, 'SECRETS_PATH', tmp_path/'secrets.dat')
    monkeypatch.setattr(app,'dpapi_decrypt',lambda val: val)
    (tmp_path/'secrets.dat').write_text(json.dumps({'cluster_password':'old'}))
    assert app.load_secrets()['max_bot_token'] == ''
