"""Read 1C launcher infobase lists (.v8i), without modifying originals.

Supports UTF-8 BOM, CP1251 fallback; client/server (Srvr/Ref), file (File),
and web (Ws). Web entries are visible but cannot run a local DumpIB job.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Literal


@dataclass(frozen=True)
class Infobase:
    name: str
    kind: Literal['server', 'file', 'web']
    server: str = ''
    ref: str = ''
    path: str = ''
    url: str = ''
    source: str = ''


def parse_connection(value: str) -> dict[str, str]:
    """Parse a 1C Connect= string, including quoted values with semicolons."""
    result: dict[str, str] = {}
    pos = 0
    while pos < len(value):
        m = re.match(r'\s*([A-Za-z][\w]*)\s*=\s*', value[pos:])
        if not m:
            stop = value.find(';', pos)
            if stop < 0:
                break
            pos = stop + 1
            continue
        key = m.group(1).casefold()
        pos += m.end()
        if pos < len(value) and value[pos] == '"':
            pos += 1
            chars = []
            while pos < len(value):
                c = value[pos]
                if c == '"':
                    if pos + 1 < len(value) and value[pos + 1] == '"':
                        chars.append('"')
                        pos += 2
                        continue
                    pos += 1
                    break
                chars.append(c)
                pos += 1
            val = ''.join(chars)
        else:
            stop = value.find(';', pos)
            if stop < 0:
                stop = len(value)
            val = value[pos:stop].strip()
            pos = stop
        result[key] = val
        if pos < len(value) and value[pos] == ';':
            pos += 1
    return result


def parse_ibases_text(text: str, source: str = '') -> list[Infobase]:
    items = []
    name = ''
    current: dict[str, str] = {}

    def flush():
        if not name or 'connect' not in current:
            return
        attrs = parse_connection(current['connect'])
        if attrs.get('srvr') and attrs.get('ref'):
            items.append(Infobase(name, 'server', server=attrs['srvr'], ref=attrs['ref'], source=source))
        elif attrs.get('file'):
            items.append(Infobase(name, 'file', path=attrs['file'], source=source))
        elif attrs.get('ws'):
            items.append(Infobase(name, 'web', url=attrs['ws'], source=source))

    for raw in text.lstrip('\ufeff').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            flush()
            name = line[1:-1].strip()
            current = {}
        elif '=' in line and name:
            k, v = line.split('=', 1)
            current[k.strip().casefold()] = v.strip()
    flush()
    return items


def read_ibases(path: str | Path) -> list[Infobase]:
    p = Path(path)
    raw = p.read_bytes()
    for encoding in ('utf-8-sig', 'utf-16', 'cp1251'):
        try:
            return parse_ibases_text(raw.decode(encoding), source=str(p))
        except UnicodeDecodeError:
            pass
    raise ValueError(f'Не удалось определить кодировку файла: {p}')


def default_ibases_candidates() -> list[Path]:
    roots: list[Path] = []
    roaming = os.environ.get('APPDATA')
    if roaming:
        roots.append(Path(roaming) / '1C' / '1CEStart' / 'ibases.v8i')
    profile = os.environ.get('USERPROFILE')
    if profile:
        roots.append(Path(profile) / 'AppData' / 'Roaming' / '1C' / '1CEStart' / 'ibases.v8i')
    if not roots:
        roots.append(Path.home() / 'AppData' / 'Roaming' / '1C' / '1CEStart' / 'ibases.v8i')
    # May contain shared lists; avoid assuming they exist.
    program_data = os.environ.get('PROGRAMDATA')
    if program_data:
        roots.append(Path(program_data) / '1C' / '1CEStart' / 'ibases.v8i')
    seen = set()
    return [p for p in roots if not (str(p).casefold() in seen or seen.add(str(p).casefold()))]


def item_to_job(item: Infobase, existing: list[dict] | None = None) -> dict:
    from uuid import uuid4
    existing = existing or []
    # Stable name/Ref from launcher; do not modify source list.
    if item.kind == 'server':
        identifier = item.ref
    elif item.kind == 'file':
        identifier = re.sub(r'[^\w-]', '_', item.name, flags=re.UNICODE)[:45] or 'file_1c'
    else:
        identifier = re.sub(r'[^\w-]', '_', item.name, flags=re.UNICODE)[:45] or 'web_1c'
    used = {str(j.get('ref', '')).casefold() for j in existing}
    ref = identifier
    idx = 2
    while ref.casefold() in used:
        ref = f'{identifier}_{idx}'
        idx += 1
    return {
        'id': uuid4().hex,
        'type': '1c',
        'name': item.name,
        'ref': ref,
        'ib_ref': item.ref if item.kind == 'server' else '',
        'connection_kind': item.kind,
        'one_c_server': item.server,
        'one_c_file': item.path,
        'one_c_url': item.url,
        'enabled': False,  # import is safe by default; user enables after checking connection
        'keep': 7,
        'schedule_time': '',
        'archive_format': 'none',
        'ftp_enabled': False,
        'platform_mode': 'auto',
        'one_c_exe': '',
    }
