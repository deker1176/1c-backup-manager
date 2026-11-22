"""Shared 1C/MSSQL/file backup packaging and FTP transport.

No Windows dependencies in this module, so the pure-data paths can be tested.
Archive and FTP operations are fail-closed: no retention on an unsuccessful upload.
"""
from __future__ import annotations

from contextlib import contextmanager
from task_runtime import check_deadline, limit_seconds
from datetime import datetime
from ftplib import FTP, FTP_TLS, error_perm
import hashlib
import os
from pathlib import Path
import re
import ssl
import sys
from tempfile import TemporaryDirectory
import time
import zipfile


def safe_component(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", str(value)).strip(" .") or "backup"


def job_key(job: dict) -> str:
    if job.get("id"):
        return str(job["id"])
    return str(job.get("ref") or job.get("name") or "")


def job_type(job: dict) -> str:
    return job.get("type", "1c")


def make_target_folder(cfg: dict, job: dict) -> Path:
    base = job.get("folder") or str(Path(cfg["backup_root"]) / safe_component(job.get("archive_prefix") or job.get("ref") or job.get("name") or "backup"))
    folder = Path(base)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def verify_archive(path: Path, fmt: str) -> None:
    if fmt == "zip":
        with zipfile.ZipFile(path) as z:
            if not z.namelist():
                raise RuntimeError("Создан пустой ZIP-архив")
            damaged = z.testzip()
            if damaged:
                raise RuntimeError(f"Ошибка CRC в ZIP: {damaged}")
    elif fmt == "7z":
        try:
            import py7zr
        except ImportError as exc:
            raise RuntimeError("Для формата 7z установите py7zr и пересоберите программу") from exc
        with py7zr.SevenZipFile(path, mode="r") as z:
            if not z.getnames():
                raise RuntimeError("Создан пустой 7z-архив")
            test_result = z.testzip()
            if test_result not in (None, True):
                raise RuntimeError(f"Ошибка контрольной суммы 7z: {test_result}")
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("Файл архива пустой")


def _archive_tree_entries(paths: list[str], forbidden: list[Path] | None = None):
    """Yield (source file, relative archive name), detecting name collisions.

    No symlink traversal; backing up linked/virtual folders requires separate task.
    Any inaccessible/missing source fails instead of creating a misleading archive.
    """
    forbidden = [p.resolve() for p in (forbidden or [])]
    seen = set()
    count = 0
    for source in paths:
        p = Path(source).expanduser()
        if not p.exists():
            raise RuntimeError(f"Источник не найден: {p}")
        if p.is_symlink():
            raise RuntimeError(f"Ссылки в источниках не поддерживаются: {p}")
        rp = p.resolve()
        for f in forbidden:
            if rp == f or f in rp.parents:
                raise RuntimeError(f"Каталог копий не может быть источником: {p}")
        if p.is_file():
            names = [(p, p.name)]
        elif p.is_dir():
            names = []
            # Do not inadvertently archive the destination folder if it is inside source.
            for f in forbidden:
                if rp == f or rp in f.parents:
                    raise RuntimeError(f"Каталог копий находится внутри источника: {p} -> {f}")
            for root, dirs, files in os.walk(p, followlinks=False):
                dirs.sort()
                files.sort()
                for dirname in dirs:
                    if (Path(root) / dirname).is_symlink():
                        raise RuntimeError(f"Найдена символьная ссылка в каталоге: {Path(root) / dirname}")
                for filename in files:
                    fp = Path(root) / filename
                    if fp.is_symlink():
                        raise RuntimeError(f"Найдена символьная ссылка в каталоге: {fp}")
                    names.append((fp, f"{p.name}/{fp.relative_to(p).as_posix()}"))
        else:
            raise RuntimeError(f"Источник не файл и не папка: {p}")
        for path, name in names:
            key = name.casefold()
            if key in seen:
                raise RuntimeError(f"Одинаковые пути внутри архива: {name}. Укажите разные источники.")
            seen.add(key)
            count += 1
            yield path, name
    if count == 0:
        raise RuntimeError("В выбранных источниках нет файлов")


def pack_files(sources: list[str], target: Path, fmt: str, forbidden: list[Path] | None = None, log=print) -> Path:
    if fmt not in {"zip", "7z"}:
        raise RuntimeError("Файлы и каталоги упаковываются только в ZIP или 7z")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        raise RuntimeError(f"Файл временного архива уже существует: {partial}")
    try:
        entries = _archive_tree_entries(sources, forbidden)
        if fmt == "zip":
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=5, allowZip64=True) as z:
                for path, arcname in entries:
                    check_deadline()
                    z.write(path, arcname)
        else:
            try:
                import py7zr
            except ImportError as exc:
                raise RuntimeError("Установите py7zr для создания 7z") from exc
            with py7zr.SevenZipFile(partial, "w") as z:
                for path, arcname in entries:
                    z.write(path, arcname=arcname)
        verify_archive(partial, fmt)
        os.replace(partial, target)
        log(f"Архив готов: {target} ({target.stat().st_size} байт)")
        return target
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def pack_single(source: Path, fmt: str, log=print) -> Path:
    if fmt == "none":
        return source
    if fmt not in {"zip", "7z"}:
        raise RuntimeError(f"Неизвестный формат архива: {fmt}")
    target = source.with_suffix("." + fmt)
    if target.exists():
        raise RuntimeError(f"Архив уже существует: {target}")
    result = pack_files([str(source)], target, fmt, forbidden=[], log=log)
    return result


def _sql_quote_ident(value: str) -> str:
    if not value or "\0" in value:
        raise ValueError("Неверное имя SQL-базы")
    return "[" + value.replace("]", "]]" ) + "]"


def _sql_quote_literal(value: str) -> str:
    if not value or "\0" in value:
        raise ValueError("Неверный путь SQL")
    return "N'" + value.replace("'", "''") + "'"


def backup_mssql(job: dict, sec: dict, cfg: dict, log=print) -> Path:
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("Для MS SQL нужен pyodbc и Microsoft ODBC Driver for SQL Server") from exc

    name = str(job.get("sql_database", "")).strip()
    server = str(job.get("sql_server", ".")).strip()
    sql_output = str(job.get("sql_backup_dir", "")).strip()
    if not (name and server and sql_output):
        raise RuntimeError("Для MS SQL заполните сервер, базу и каталог .bak на SQL-сервере")
    out_dir = Path(sql_output)
    # SQL server service writes to this path. App service must then read same file.
    # Cannot create directory on a remote SQL host from here.
    filename = f"{safe_component(job.get('archive_prefix') or job.get('ref') or name)}_{stamp()}.bak"
    result = out_dir / filename
    if result.exists():
        raise RuntimeError(f"Файл SQL уже существует: {result}")

    driver = str(job.get("sql_driver") or "ODBC Driver 18 for SQL Server")
    if "}" in driver or ";" in driver:
        raise RuntimeError("Некорректное имя ODBC-драйвера")
    parts = [f"DRIVER={{{driver}}}", f"SERVER={{{server}}}", "DATABASE=master", "Encrypt=yes"]
    parts.append("TrustServerCertificate=yes" if job.get("sql_trust_cert", False) else "TrustServerCertificate=no")
    if job.get("sql_windows_auth", True):
        parts.append("Trusted_Connection=yes")
    else:
        user = str(job.get("sql_user", ""))
        password = sec.get("sql_passwords", {}).get(job_key(job), "")
        if not user or not password:
            raise RuntimeError("Укажите логин и пароль SQL Server")
        # pyodbc connection strings use braces to escape embedded delimiters.
        parts.extend(["UID={" + user.replace("}", "}}") + "}",
                      "PWD={" + password.replace("}", "}}") + "}"])

    log(f"MS SQL BACKUP DATABASE: {name} @ {server} -> {result}")
    with pyodbc.connect(";".join(parts) + ";", timeout=20, autocommit=True) as conn:
        cur = conn.cursor()
        cur.timeout = max(1, int(limit_seconds(12 * 3600)))
        sql = (
            f"BACKUP DATABASE {_sql_quote_ident(name)} "
            f"TO DISK = {_sql_quote_literal(str(result))} "
            f"WITH INIT, COPY_ONLY, CHECKSUM, STATS = 10"
        )
        cur.execute(sql)
        while cur.nextset():
            pass
        log("SQL: BACKUP завершён; RESTORE VERIFYONLY WITH CHECKSUM...")
        cur.execute(f"RESTORE VERIFYONLY FROM DISK = {_sql_quote_literal(str(result))} WITH CHECKSUM")
        while cur.nextset():
            pass
        cur.close()
    # If remote database server has a different filesystem, this check will
    # accurately detect that the .bak is not accessible to the backup manager.
    if not result.is_file() or result.stat().st_size <= 0:
        raise RuntimeError(
            "SQL создал .bak на своём сервере, но служба бэкапов не может открыть файл. "
            "Укажите общий UNC-путь, доступный SQL Server и службе, и проверьте права. "
            f"Ожидаемый файл: {result}"
        )
    log(f"MS SQL: .bak проверен, {result.stat().st_size} байт")
    return result


@contextmanager
def ftp_connection(ftp_cfg: dict, password: str):
    host = str(ftp_cfg.get("host", "")).strip()
    if not host:
        raise RuntimeError("FTP: не указан сервер")
    use_tls = bool(ftp_cfg.get("tls", True))
    port = int(ftp_cfg.get("port") or 21)
    user = str(ftp_cfg.get("user", ""))
    client = FTP_TLS(context=ssl.create_default_context(), timeout=60) if use_tls else FTP(timeout=60)
    try:
        client.connect(host, port, timeout=60)
        client.login(user, password)
        if use_tls:
            client.prot_p()  # Encrypt data channel too
        client.set_pasv(bool(ftp_cfg.get("passive", True)))
        yield client
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def ftp_ensure_dir(client, remote_dir: str) -> None:
    # FTP path is not a local filesystem path. Restrict weird path components.
    raw = str(remote_dir or "/").replace("\\", "/")
    if any(item in {".", ".."} for item in raw.split("/")):
        raise RuntimeError("FTP-путь не может содержать . или ..")
    if raw.startswith("/"):
        client.cwd("/")
    for part in raw.split("/"):
        if not part:
            continue
        try:
            client.cwd(part)
        except error_perm:
            client.mkd(part)
            client.cwd(part)


def ftp_upload(path: Path, ftp_cfg: dict, password: str, log=print) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"Нет файла для FTP: {path}")
    remote_name = path.name
    remote_tmp = f".{remote_name}.uploading"
    with ftp_connection(ftp_cfg, password) as ftp:
        ftp_ensure_dir(ftp, str(ftp_cfg.get("path", "/")))
        log(f"FTP: отправка {path.name} на {ftp_cfg.get('host')}:{ftp_cfg.get('port',21)}")
        try:
            with path.open("rb") as source:
                ftp.storbinary(f"STOR {remote_tmp}", source, blocksize=256 * 1024)
            try:
                remote_size = ftp.size(remote_tmp)
                if remote_size is not None and remote_size != path.stat().st_size:
                    raise RuntimeError(f"FTP: неверный размер: {remote_size} != {path.stat().st_size}")
            except error_perm:
                # Some FTP servers don't support SIZE; upload success still verified by STOR completion.
                pass
            ftp.rename(remote_tmp, remote_name)
            log(f"FTP: готово {remote_name}")
        except Exception:
            try:
                ftp.delete(remote_tmp)
            except Exception:
                pass
            raise


def ftp_check(ftp_cfg: dict, password: str):
    with ftp_connection(ftp_cfg, password) as ftp:
        ftp_ensure_dir(ftp, str(ftp_cfg.get("path", "/")))
        ftp.pwd()


def prune_local(job: dict, output: Path, keep: int, log=print) -> None:
    """Retain newest N *backup sets*, not N individual files.

    A .dt plus a .zip with the same timestamp is one set. Incomplete and
    failed-FTP sets are preserved and never silently discarded.
    """
    keep = max(1, int(keep))
    folder = output.parent
    prefix = safe_component(job.get("archive_prefix") or job.get("ref") or job.get("name") or "backup") + "_"
    endings = {".dt", ".bak", ".zip", ".7z"}
    grouped = {}
    for p in folder.iterdir():
        if not (p.is_file() and p.name.startswith(prefix) and p.suffix.lower() in endings):
            continue
        group_key = p.stem
        grouped.setdefault(group_key, []).append(p)
    groups = sorted(grouped.values(),
                    key=lambda files: max(p.stat().st_mtime for p in files), reverse=True)
    for files in groups[keep:]:
        if any(p.with_name(p.name + ".pending-ftp").exists() for p in files):
            log("Пропуск очистки: есть неотправленная FTP-копия " + files[0].stem)
            continue
        for old in files:
            log(f"Удаление старой локальной копии: {old.name}")
            old.unlink()
            side = Path(str(old) + ".sha256")
            side.unlink(missing_ok=True)
        # A successful v3 1C dump may have an associated designer log.
        raw_log = folder / (files[0].stem + ".log")
        raw_log.unlink(missing_ok=True)




def ftp_targets_for_job(cfg: dict, sec: dict, job: dict):
    profiles = cfg.get("ftp_profiles", [])
    requested = job.get("ftp_targets") or []
    if not isinstance(requested, list):
        requested = []
    if not requested and job.get("ftp_profile"):
        requested = [job["ftp_profile"]]
    if requested:
        found = []
        for name in requested:
            profile = next((p for p in profiles if str(p.get("name", "")).casefold() == str(name).casefold()), None)
            if profile is None:
                raise RuntimeError(f"FTP-профиль не найден: {name}")
            pwd = sec.get("ftp_profile_passwords", {}).get(profile["name"], "")
            found.append((profile, pwd))
        return found
    return [(cfg.get("ftp", {}), sec.get("ftp_password", ""))]


def resolve_ftp_target(cfg: dict, sec: dict, job: dict):
    return ftp_targets_for_job(cfg, sec, job)[0]


def finalize_backup(job: dict, source: Path, cfg: dict, sec: dict, log=print) -> Path:
    fmt = str(job.get("archive_format", "none")).lower()
    packed = pack_single(source, fmt, log) if fmt != "none" else source
    sha = sha256_file(packed)
    (Path(str(packed) + ".sha256")).write_text(f"{sha}  {packed.name}\n", encoding="ascii")
    log(f"SHA256: {sha}")
    if job.get("ftp_enabled", False):
        marker = packed.with_name(packed.name + ".pending-ftp")
        marker.write_text("upload pending", encoding="utf-8")
        for ftp_cfg, password in ftp_targets_for_job(cfg, sec, job):
            ftp_upload(packed, ftp_cfg, password, log)
        marker.unlink(missing_ok=True)
    else:
        log("FTP: отключён для задания")

    # Delete the raw .dt/.bak only after successful archive and FTP transfer.
    if packed != source and job.get("remove_raw_after_pack", False):
        source.unlink(missing_ok=True)
        Path(str(source) + ".sha256").unlink(missing_ok=True)
        log(f"Исходный файл удалён после успешной упаковки/отправки: {source.name}")
    prune_local(job, packed, int(job.get("keep", 7)), log)
    return packed


def backup_files(job: dict, cfg: dict, sec: dict, log=print) -> Path:
    sources = [x.strip() for x in str(job.get("file_paths", "")).splitlines() if x.strip()]
    if not sources:
        raise RuntimeError("Добавьте файлы или папки для резервного копирования")
    folder = make_target_folder(cfg, job)
    fmt = str(job.get("archive_format") or "zip")
    if fmt == "none":
        raise RuntimeError("Для задания «Файлы и папки» выберите ZIP или 7z")
    output = folder / f"{safe_component(job.get('archive_prefix') or job.get('ref') or job.get('name'))}_{stamp()}.{fmt}"
    forbidden = [Path(cfg["backup_root"]), folder]
    output = pack_files(sources, output, fmt, forbidden=forbidden, log=log)
    hash_val = sha256_file(output)
    Path(str(output) + ".sha256").write_text(f"{hash_val}  {output.name}\n", encoding="ascii")
    log(f"SHA256: {hash_val}")
    if job.get("ftp_enabled", False):
        marker = output.with_name(output.name + ".pending-ftp")
        marker.write_text("upload pending", encoding="utf-8")
        for ftp_cfg, password in ftp_targets_for_job(cfg, sec, job):
            ftp_upload(output, ftp_cfg, password, log)
        marker.unlink(missing_ok=True)
    prune_local(job, output, int(job.get("keep", 7)), log)
    return output


@contextmanager
def global_backup_lock(base_dir: Path):
    """Cross-process lock preventing GUI and Windows service from backing up simultaneously.
    The file remains but the OS byte lock is automatically released after crashes.
    """
    lock_path = Path(base_dir) / "backup.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
    except OSError as exc:
        raise RuntimeError(
            f"Корневая папка копий «{base_dir}» недоступна. "
            "Откройте «Сервис → Параметры программы → Файлы архивов», "
            "выберите существующий диск и проверьте права учётной записи службы."
        ) from exc
    with lock_file as lock:
        lock.seek(0)
        if lock.read(1) != b"X":
            lock.seek(0)
            lock.write(b"X")
            lock.flush()
        lock.seek(0)
        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Другая копия программы или служба уже выполняет резервное копирование") from exc
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Другое резервное копирование уже выполняется") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sql_check_connection(job: dict, sec: dict) -> None:
    """Non-destructive auth test; BACKUP/RESTORE tests remain for actual backup."""
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("Не установлен pyodbc / Microsoft ODBC Driver for SQL Server") from exc
    server = str(job.get("sql_server", ".")).strip()
    driver = str(job.get("sql_driver", "ODBC Driver 18 for SQL Server"))
    if "}" in driver or ";" in driver:
        raise RuntimeError("Недопустимое имя драйвера ODBC")
    options = [f"DRIVER={{{driver}}}", f"SERVER={{{server}}}", "DATABASE=master", "Encrypt=yes",
               "TrustServerCertificate=yes" if job.get("sql_trust_cert", False) else "TrustServerCertificate=no"]
    if job.get("sql_windows_auth", True):
        options.append("Trusted_Connection=yes")
    else:
        user = str(job.get("sql_user", ""))
        pwd = sec.get("sql_passwords", {}).get(job_key(job), "")
        if not user or not pwd:
            raise RuntimeError("Укажите логин/пароль SQL Server")
        options.extend(["UID={" + user.replace("}", "}}") + "}",
                        "PWD={" + pwd.replace("}", "}}") + "}"])
    conn = pyodbc.connect(";".join(options) + ";", timeout=20)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        if not row or row[0] != 1:
            raise RuntimeError("SQL Server не ответил на проверочный запрос")
    finally:
        conn.close()
