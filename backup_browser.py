"""Find backup artifacts in job-specific folders, including UNC shares.

No directory is ever created during browsing. Missing/offline network shares
are skipped instead of breaking GUI refresh. A .dt can be restored only into
a NEW, empty file infobase, never into any configured production database.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Iterable


ARCHIVE_EXTENSIONS = {".dt", ".bak", ".zip", ".7z"}


def backup_locations(cfg, jobs, run_artifacts=()):
    root = Path(str(cfg.get("backup_root") or "")).expanduser()
    result = []
    # Scan explicit job output folders first, so an unrelated tree under
    # backup_root cannot exhaust the bounded traversal before UNC archives.
    for job in jobs:
        path = str(job.get("folder") or "").strip()
        if path:
            result.append((Path(path).expanduser(), 1))
        elif str(cfg.get("backup_root") or "").strip():
            label = str(job.get("archive_prefix") or job.get("ref") or job.get("name") or "backup")
            from backup_extensions import safe_component
            result.append((root / safe_component(label), 1))
    for item in run_artifacts:
        path = Path(str(item or "")).expanduser()
        if str(item or "").strip():
            result.append((path.parent, 1))
    if str(cfg.get("backup_root") or "").strip():
        result.append((root, 2))
    dedup = {}
    for folder, depth in result:
        key = os.path.normcase(os.path.normpath(str(folder)))
        dedup[key] = (folder, max(depth, dedup.get(key, (None, 0))[1]))
    return list(dedup.values())


def find_backup_artifacts(cfg, jobs, run_artifacts=(), max_files=500,
                          max_dirs=100, max_entries=15000):
    """Bounded traversal: scan configured output folders plus backup_root."""
    found = {}
    folders_seen, visited, entries = set(), 0, 0
    for root, maxdepth in backup_locations(cfg, jobs, run_artifacts):
        pending = [(root, 0)]
        while pending and visited < max_dirs and entries < max_entries:
            folder, depth = pending.pop()
            key = os.path.normcase(os.path.normpath(str(folder)))
            if key in folders_seen:
                continue
            folders_seen.add(key)
            visited += 1
            try:
                iterator = os.scandir(folder)
            except OSError:
                continue
            try:
                with iterator:
                    for item in iterator:
                        entries += 1
                        if entries > max_entries:
                            break
                        try:
                            if item.is_file(follow_symlinks=False):
                                path = Path(item.path)
                                if path.suffix.casefold() not in ARCHIVE_EXTENSIONS:
                                    continue
                                stat = item.stat(follow_symlinks=False)
                                if stat.st_size <= 0:
                                    continue
                                found[os.path.normcase(os.path.normpath(item.path))] = {
                                    "path": str(path),
                                    "size": stat.st_size,
                                    "modified": stat.st_mtime,
                                    "format": path.suffix.lower().lstrip("."),
                                }
                            elif depth < maxdepth and item.is_dir(follow_symlinks=False):
                                pending.append((Path(item.path), depth + 1))
                        except OSError:
                            continue
            except OSError:
                continue
    return sorted(found.values(), key=lambda a: a["modified"], reverse=True)[:max_files]


def verify_dt_checksum(path):
    archive = Path(path)
    if not archive.is_file() or archive.suffix.casefold() != ".dt":
        raise RuntimeError("Выберите существующий архив .dt")
    sidecar = Path(str(archive) + ".sha256")
    if not sidecar.is_file():
        raise RuntimeError("Для этого .dt нет файла .sha256; проверка недоступна")
    content = sidecar.read_text(encoding="ascii").strip().split()
    if not content or len(content[0]) != 64:
        raise RuntimeError("Неверное содержимое контрольной суммы")
    actual = hashlib.sha256()
    with archive.open("rb") as stream:
        for part in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            actual.update(part)
    if actual.hexdigest().casefold() != content[0].casefold():
        raise RuntimeError("SHA256 НЕ СОВПАДАЕТ. Восстановление отменено")
    return actual.hexdigest()


def validate_new_restore_target(archive, target, jobs):
    archive, target = Path(archive).resolve(), Path(target).resolve()
    if archive.suffix.casefold() != ".dt" or not archive.is_file():
        raise RuntimeError("Автоматическое восстановление поддерживает только .dt")
    if archive.stat().st_size <= 0:
        raise RuntimeError("Архив .dt пуст")
    if target == archive.parent or target == archive:
        raise RuntimeError("Тестовую базу нельзя создавать в папке исходного архива")
    for job in jobs:
        if str(job.get("connection_kind") or "") == "file":
            source = str(job.get("one_c_file") or "").strip()
            if source:
                production = Path(source).resolve()
                if target == production or production in target.parents or target in production.parents:
                    raise RuntimeError("Целевой каталог пересекается с рабочей базой 1С")
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise RuntimeError("Каталог восстановления должен быть новым или ПУСТЫМ")
    return archive, target


def restore_dt_as_test_infobase(archive, target, jobs, platform_bin, log=print, timeout=14400):
    """Create a new 1C infobase from a .dt template, without replacing an old one.

    1cv8 CREATEINFOBASE File="..."; /UseTemplate source.dt is documented by 1C.
    Does not add the test database to the launcher's infobase list automatically.
    """
    archive, target = validate_new_restore_target(archive, target, jobs)
    if Path(str(archive) + ".sha256").is_file():
        log(f"SHA256: подтверждён {verify_dt_checksum(archive)}")
    exe = Path(platform_bin)
    if exe.is_dir():
        exe = exe / "1cv8.exe"
    if not exe.is_file():
        raise RuntimeError("Не найден 1cv8.exe. Проверьте путь к платформе 1С")
    target.mkdir(parents=True, exist_ok=True)
    out = target.parent / (target.name + ".restore.log")
    args = [str(exe), "CREATEINFOBASE", f'File="{target}";',
            "/UseTemplate", str(archive), "/Out", str(out)]
    log(f"ТЕСТОВОЕ восстановление: {archive} -> {target}")
    cp = subprocess.run(args, capture_output=True, timeout=timeout,
                        creationflags=(0x08000000 if os.name == "nt" else 0))
    if cp.returncode != 0 or not (target / "1Cv8.1CD").is_file():
        raise RuntimeError(
            f"Восстановление не завершилось успешно (код {cp.returncode}). "
            f"Проверьте журнал {out}; тестовая папка сохранена для диагностики")
    log(f"Тестовая база создана: {target}. Рабочие базы не изменялись.")
    return target
