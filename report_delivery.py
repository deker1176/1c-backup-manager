"""Opt-in SMTP, Telegram and MAX Bot API delivery for task status reports.

Nothing is sent unless channels are configured and enabled per task. Credentials
stay in the existing Windows-DPAPI secrets.dat managed by the main application.
"""
from __future__ import annotations
from datetime import datetime
from email.message import EmailMessage
import smtplib
import ssl
import urllib.parse
import urllib.request
import json
import re
import threading
import time
import urllib.error


def format_result(job: dict, success: bool, error: str = '', artifact=None) -> tuple[str, str]:
    name = str(job.get('name') or job.get('ref') or 'Задание')
    state = 'Успешно' if success else 'Ошибка'
    subject = f'1C Backup Manager — {state}: {name}'
    body = '\n'.join([
        'Отчёт о выполнении резервного копирования',
        f'Задание: {name}',
        f'Дата: {datetime.now():%d.%m.%Y %H:%M:%S}',
        f'Результат: {state}',
        f'Файл: {artifact}' if artifact else '',
        f'Сообщение: {error}' if error else '',
        '', 'Отправлено приложением 1C Backup Manager.'
    ])
    return subject, body


def build_summary(jobs: list[dict], state: dict) -> tuple[str, str]:
    subject = f'1C Backup Manager — сводка {datetime.now():%d.%m.%Y}'
    lines = [subject, '', 'Последние результаты заданий:']
    for job in jobs:
        key = str(job.get('id') or job.get('ref') or job.get('name') or '')
        entry = state.get(key, {})
        last = entry.get('last_attempt_time') or entry.get('last_run_time') or 'не запускалось'
        result = entry.get('last_result', '')
        status = 'Успешно' if result == 'ok' else ('Ошибка' if result == 'error' else 'Нет данных')
        err = str(entry.get('last_error') or '')
        lines.append(f"• {job.get('name')}: {status} — {last}{(' — ' + err) if err else ''}")
    if not jobs:
        lines.append('Нет настроенных заданий.')
    return subject, '\n'.join(lines)


def _send_smtp(settings: dict, password: str, subject: str, body: str):
    host = str(settings.get('host') or '').strip()
    recipients = [s.strip() for s in str(settings.get('to') or '').replace(';', ',').split(',') if s.strip()]
    sender = str(settings.get('from') or '').strip()
    if not host or not sender or not recipients:
        raise ValueError('Почта: укажите SMTP сервер, адрес отправителя и получателя.')
    mode = str(settings.get('security') or 'STARTTLS').upper()
    if mode not in {'STARTTLS', 'SSL/TLS'}:
        raise ValueError('Почта: используйте STARTTLS или SSL/TLS.')
    port = int(settings.get('port') or (465 if mode == 'SSL/TLS' else 587))
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = ', '.join(recipients)
    msg['Subject'] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if mode == 'SSL/TLS':
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as smtp:
            if settings.get('user'):
                smtp.login(str(settings['user']), password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            if settings.get('user'):
                smtp.login(str(settings['user']), password)
            smtp.send_message(msg)


def _send_telegram(settings: dict, bot_token: str, body: str):
    chat_id = str(settings.get('chat_id') or '').strip()
    token = str(bot_token or '').strip()
    if not chat_id or not token:
        raise ValueError('Telegram: укажите токен бота и Chat ID.')
    # POST keeps credentials out of the application logs. Never log the URL.
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': body[:4000]}).encode('utf-8')
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',
                                 data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
        obj = json.loads(resp.read(65536))
        if not obj.get('ok'):
            raise RuntimeError('Telegram отклонил отправку сообщения.')


# MAX limits messages to <=2/second per recipient. A lock also protects
# concurrent UI and scheduler notification attempts inside the same process.
_MAX_SEND_LOCK = threading.Lock()
_MAX_LAST_SENT: dict[tuple[str, int], float] = {}


def _send_max(settings: dict, bot_token: str, body: str):
    """Send a simple MAX Bot API message using HTTPS and system TLS trust.

    https://dev.max.ru/docs-api/methods/POST/messages
    MAX uses `Authorization: <token>` (not Bearer and not a query arg).
    """
    token = str(bot_token or '').strip()
    destination = str(settings.get('recipient_id') or '').strip()
    kind = str(settings.get('recipient_type') or 'chat_id').strip()
    if not token or not destination:
        raise ValueError('MAX: укажите Bot Token и ID получателя.')
    if kind not in ('chat_id', 'user_id'):
        raise ValueError('MAX: выберите ID чата/канала или ID пользователя.')
    if not re.fullmatch(r'-?[0-9]+', destination) or int(destination) == 0:
        raise ValueError('MAX: ID получателя должен быть целым числом.')
    recipient_id = int(destination)
    # MAX text API limit: 4000 characters.
    content = str(body or '').strip()
    if not content:
        raise ValueError('MAX: текст сообщения не должен быть пустым.')
    data = json.dumps({'text': content[:4000]}, ensure_ascii=False).encode('utf-8')
    url = 'https://platform-api2.max.ru/messages?' + urllib.parse.urlencode({kind: recipient_id})
    req = urllib.request.Request(url, data=data, method='POST', headers={
        'Authorization': token,
        'Content-Type': 'application/json; charset=utf-8',
        'Accept': 'application/json',
    })
    key = (kind, recipient_id)
    with _MAX_SEND_LOCK:
        since = time.monotonic() - _MAX_LAST_SENT.get(key, 0.0)
        if since < 0.55:
            time.sleep(0.55 - since)
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
                answer = json.loads(resp.read(65536).decode('utf-8'))
                if not isinstance(answer, dict) or not isinstance(answer.get('message'), dict):
                    raise RuntimeError('MAX: API не подтвердил создание сообщения.')
        except urllib.error.HTTPError as exc:
            # Do not include headers, request objects, or tokens in logs.
            if exc.code == 401:
                raise RuntimeError('MAX: токен бота недействителен (HTTP 401).') from None
            if exc.code == 403:
                raise RuntimeError('MAX: у бота нет доступа к получателю (HTTP 403).') from None
            if exc.code == 429:
                raise RuntimeError('MAX: превышен лимит запросов (HTTP 429).') from None
            raise RuntimeError(f'MAX: ошибка HTTP {exc.code}.') from None
        _MAX_LAST_SENT[key] = time.monotonic()


def send_channels(cfg: dict, sec: dict, subject: str, body: str, *, email: bool, telegram: bool,
                  max_enabled: bool = False, log=print, strict=False) -> bool:
    settings = cfg.get('notifications') or {}
    errors = []
    sent = 0
    if email:
        try:
            _send_smtp(settings.get('smtp') or {}, sec.get('smtp_password', ''), subject, body)
            sent += 1
            log('Отчёты: письмо SMTP отправлено')
        except Exception as exc:
            errors.append(f'SMTP: {type(exc).__name__}: {exc}')
    if telegram:
        try:
            _send_telegram(settings.get('telegram') or {}, sec.get('telegram_bot_token', ''), body)
            sent += 1
            log('Отчёты: сообщение Telegram отправлено')
        except Exception as exc:
            # Error messages from HTTPError may include URL with a bot token.
            errors.append('Telegram: не удалось отправить сообщение; проверьте токен, Chat ID и соединение')
    if max_enabled:
        try:
            _send_max(settings.get('max') or {}, sec.get('max_bot_token', ''), body)
            sent += 1
            log('Отчёты: сообщение MAX отправлено')
        except ValueError as exc:
            errors.append(str(exc))
        except Exception:
            errors.append('MAX: отправка не удалась; проверьте токен, ID, доступ бота и TLS-сертификаты')
    for error in errors:
        log(error)
    if strict and errors:
        raise RuntimeError('Отчёт не отправлен по всем выбранным каналам. ' + '; '.join(errors))
    if strict and not (email or telegram or max_enabled):
        raise ValueError('Для задания отправки отчёта выберите почту, Telegram и/или MAX.')
    return sent > 0


def notify_task_result(cfg: dict, sec: dict, job: dict, success: bool, artifact=None,
                       error='', log=print):
    if job.get('type') == 'report':
        return
    if success and not job.get('notify_on_success', False):
        return
    if not success and not job.get('notify_on_failure', False):
        return
    subject, body = format_result(job, success, error, artifact)
    # Delivery failure never invalidates an already-created backup.
    send_channels(cfg, sec, subject, body, email=bool(job.get('notify_email')),
                  telegram=bool(job.get('notify_telegram')), max_enabled=bool(job.get('notify_max')), log=log, strict=False)


def send_summary_task(cfg: dict, sec: dict, job: dict, state: dict, log=print):
    subject, body = build_summary([j for j in cfg.get('databases', []) if j.get('type') != 'report'], state)
    send_channels(cfg, sec, subject, body, email=bool(job.get('notify_email')),
                  telegram=bool(job.get('notify_telegram')), max_enabled=bool(job.get('notify_max')), log=log, strict=True)
    return None
