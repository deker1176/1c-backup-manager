"""Effector-style task manager UI for 1C Backup Manager."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from run_journal import list_task_runs, read_task_log, next_scheduled_start, prune_task_runs
from pathlib import Path
import queue
import os
import posixpath
import re
import subprocess
import sys
import threading
import traceback
import uuid
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from classic_gui import (ScrollablePage, InfobaseImportDialog, SettingsDialog,
                         add_line, title_label, spin_time, SOURCE_DISPLAY, PLATFORM_DISPLAY, TYPES, SHORT_TYPES)
from ibases_import import Infobase, item_to_job
from tray_support import TrayController, available as tray_available
from service_status import query_service_state, service_labels
from ui_locale import tr, source_value, translate_widgets


def section_bar(parent, text):
    """Classic gray section header matching the reference editor layout."""
    lbl = tk.Label(parent, text=text, anchor="w", bg="#808080", fg="white",
                   font=("Segoe UI", 9, "bold"), padx=7, pady=4)
    lbl.pack(fill="x", pady=(0, 10))
    return lbl

# ===== v4.2 redesigned GUI (closer to Effector Saver workflow) =====

class RemoteFtpBrowser(tk.Toplevel):
    """Browse/create/delete remote FTP directories without exposing credentials."""
    def __init__(self, owner, cfg, password, on_select):
        super().__init__(owner)
        from backup_extensions import ftp_connection
        self.title("Выберите папку на FTP")
        self.geometry("540x510")
        self.transient(owner)
        self.grab_set()
        self.on_select = on_select
        self.conn = ftp_connection(cfg, password)
        self.ftp = None
        pane = ttk.Frame(self, padding=10)
        pane.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(pane, show="tree", selectmode="browse")
        sc = ttk.Scrollbar(pane, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sc.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self.on_open)
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="Создать папку", command=self.create_dir).pack(side="left")
        ttk.Button(btns, text="Удалить папку", command=self.delete_dir).pack(side="left", padx=5)
        ttk.Button(btns, text="Выбрать", command=self.choose).pack(side="right")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        try:
            self.ftp = self.conn.__enter__()
            self.tree.insert("", "end", iid="/", text="/", open=True)
            self.load_children("/")
        except Exception as exc:
            messagebox.showerror("FTP", str(exc), parent=self)
            self.after(10, self.destroy)

    def safe_path(self, path):
        if not path.startswith("/") or any(p in {".", ".."} for p in path.split("/")):
            raise ValueError("Неверный путь FTP")
        return path

    def get_dirs(self, path):
        self.ftp.cwd(self.safe_path(path))
        try:
            return sorted({name for name, facts in self.ftp.mlsd() if facts.get("type") == "dir" and name not in (".", "..")})
        except Exception:
            result = []
            for name in self.ftp.nlst():
                name = name.rstrip("/").split("/")[-1]
                if name in ("", ".", ".."):
                    continue
                try:
                    self.ftp.cwd(posixpath.join(path, name))
                    result.append(name)
                except Exception:
                    continue
                finally:
                    self.ftp.cwd(path)
            return sorted(set(result))

    def load_children(self, path):
        for iid in self.tree.get_children(path):
            self.tree.delete(iid)
        for name in self.get_dirs(path):
            child = posixpath.normpath(posixpath.join(path, name))
            if not child.startswith("/"):
                child = "/" + child
            if child not in self.tree.get_children(path):
                self.tree.insert(path, "end", iid=child, text=name)
                self.tree.insert(child, "end", text="...", tags=("dummy",))

    def on_open(self, _event):
        sel = self.tree.focus()
        if sel and sel.startswith("/"):
            try: self.load_children(sel)
            except Exception as exc: messagebox.showerror("FTP", str(exc), parent=self)

    def selected(self):
        s = self.tree.selection()
        return s[0] if s and s[0].startswith("/") else "/"

    def create_dir(self):
        parent = self.selected()
        name = simpledialog.askstring("FTP", "Имя новой папки:", parent=self)
        if not name:
            return
        if name in {".", ".."} or "/" in name or "\\" in name:
            messagebox.showerror("FTP", "Имя папки не должно содержать /, \\ или ..", parent=self)
            return
        try:
            self.ftp.cwd(parent)
            self.ftp.mkd(name)
            self.load_children(parent)
        except Exception as exc:
            messagebox.showerror("FTP", str(exc), parent=self)

    def delete_dir(self):
        path = self.selected()
        if path == "/":
            return
        if not messagebox.askyesno("FTP", "Удалить пустую FTP-папку?", parent=self):
            return
        try:
            self.ftp.rmd(path)
            self.load_children(posixpath.dirname(path) or "/")
        except Exception as exc:
            messagebox.showerror("FTP", "Удалить можно только пустую папку. " + str(exc), parent=self)

    def choose(self):
        self.on_select(self.selected())
        self.destroy()

    def destroy(self):
        try:
            if self.conn and self.ftp:
                self.conn.__exit__(None, None, None)
                self.ftp = None
        except Exception:
            pass
        parent = self.master
        super().destroy()
        try:
            if parent.winfo_exists():
                parent.grab_set()
        except tk.TclError:
            pass


class StorageProfileEditor(tk.Toplevel):
    def __init__(self, owner, profile=None, password="", on_save=None):
        super().__init__(owner)
        self.title("FTP / папка для архивов")
        self.geometry("520x370")
        self.resizable(False, False)
        self.transient(owner)
        self.grab_set()
        self.on_save = on_save
        profile = profile or {}
        self.v = {
            "name": tk.StringVar(value=str(profile.get("name", ""))),
            "protocol": tk.StringVar(value="FTPS" if profile.get("tls", True) else "FTP"),
            "host": tk.StringVar(value=str(profile.get("host", ""))),
            "port": tk.StringVar(value=str(profile.get("port", 21))),
            "user": tk.StringVar(value=str(profile.get("user", ""))),
            "password": tk.StringVar(value=password),
            "path": tk.StringVar(value=str(profile.get("path", "/backups"))),
            "passive": tk.BooleanVar(value=profile.get("passive", True)),
        }
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        for i, (lab, key) in enumerate([
            ("Название", "name"), ("Протокол", "protocol"), ("Адрес сервера", "host"),
            ("Порт", "port"), ("Пользователь", "user"), ("Пароль", "password"), ("Папка", "path")
        ]):
            ttk.Label(frm, text=lab + ":").grid(row=i, column=0, sticky="w", pady=5)
            if key == "protocol":
                ttk.Combobox(frm, textvariable=self.v[key], values=["FTP", "FTPS"], state="readonly", width=16).grid(row=i, column=1, sticky="w", pady=5)
            else:
                ttk.Entry(frm, textvariable=self.v[key], show="●" if key == "password" else "", width=34).grid(row=i, column=1, sticky="ew", pady=5)
        ttk.Button(frm, text="...", width=3, command=self.browse_ftp).grid(row=6, column=2, padx=5)
        ttk.Checkbutton(frm, text="Пассивный режим", variable=self.v["passive"]).grid(row=7, column=1, sticky="w", pady=8)
        frm.columnconfigure(1, weight=1)
        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="Тест", command=self.test).pack(side="left")
        ttk.Button(btns, text="OK", command=self.save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=8)

    def data(self):
        name = self.v["name"].get().strip()
        if not name:
            raise ValueError("Введите название хранилища.")
        host = self.v["host"].get().strip()
        if not host:
            raise ValueError("Введите адрес сервера FTP/FTPS.")
        port = int(self.v["port"].get() or 21)
        profile = {
            "name": name, "host": host, "port": port,
            "user": self.v["user"].get().strip(),
            "path": self.v["path"].get().strip() or "/",
            "tls": self.v["protocol"].get() == "FTPS",
            "passive": bool(self.v["passive"].get()),
        }
        return profile, self.v["password"].get()

    def browse_ftp(self):
        try:
            profile, pwd = self.data()
            dlg = RemoteFtpBrowser(self, profile, pwd, lambda path: self.v["path"].set(path))
            self.wait_window(dlg)
        except Exception as exc:
            messagebox.showerror("FTP", str(exc), parent=self)

    def test(self):
        try:
            profile, pwd = self.data()
            self.master.owner.backend.ftp_check(profile, pwd)
            messagebox.showinfo("FTP", "Соединение и каталог доступны.", parent=self)
        except Exception as exc:
            messagebox.showerror("FTP", str(exc), parent=self)

    def save(self):
        try:
            profile, pwd = self.data()
            if self.on_save:
                self.on_save(profile, pwd)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Хранилище", str(exc), parent=self)


class StorageProfilesDialog(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.title("Управление хранилищами приложения")
        self.geometry("560x420")
        self.transient(owner)
        self.grab_set()
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        tools = ttk.Frame(frm)
        tools.pack(fill="x")
        ttk.Button(tools, text="+ Создать", command=self.add).pack(side="left", padx=2)
        ttk.Button(tools, text="✎ Изменить", command=self.edit).pack(side="left", padx=2)
        ttk.Button(tools, text="− Удалить", command=self.delete).pack(side="left", padx=2)
        body = ttk.LabelFrame(frm, text="FTP/FTPS", padding=8)
        body.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(body, columns=("host",), show="tree")
        self.tree.pack(fill="both", expand=True)
        ttk.Button(frm, text="Закрыть", command=self.destroy).pack(side="right")
        self.refresh()

    def profiles(self):
        return self.owner.cfg.setdefault("ftp_profiles", [])

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        root = self.tree.insert("", "end", text="FTP/FTPS", open=True)
        for i, prof in enumerate(self.profiles()):
            self.tree.insert(root, "end", iid=f"p_{i}", text=prof.get("name", f"Profile {i+1}"))

    def _index(self):
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("p_"):
            return None
        return int(sel[0].split("_", 1)[1])

    def add(self):
        def saver(profile, pwd):
            if any(str(x.get("name", "")).casefold() == profile["name"].casefold() for x in self.profiles()):
                raise ValueError("Профиль с таким названием уже существует.")
            self.profiles().append(profile)
            self.owner.sec.setdefault("ftp_profile_passwords", {})[profile["name"]] = pwd
            self.owner.persist()
            self.refresh()
        StorageProfileEditor(self, on_save=saver)

    def edit(self):
        idx = self._index()
        if idx is None:
            return
        prof = deepcopy(self.profiles()[idx])
        pwd = self.owner.sec.setdefault("ftp_profile_passwords", {}).get(prof.get("name", ""), "")
        old_name = prof.get("name", "")
        def saver(profile, password):
            for i, x in enumerate(self.profiles()):
                if i != idx and str(x.get("name", "")).casefold() == profile["name"].casefold():
                    raise ValueError("Профиль с таким названием уже существует.")
            self.profiles()[idx] = profile
            if old_name != profile["name"]:
                for job in self.owner.jobs:
                    if job.get("ftp_profile") == old_name:
                        job["ftp_profile"] = profile["name"]
                    job["ftp_targets"] = [profile["name"] if x == old_name else x
                                          for x in job.get("ftp_targets", [])]
            pwmap = self.owner.sec.setdefault("ftp_profile_passwords", {})
            if old_name in pwmap and old_name != profile["name"]:
                pwmap.pop(old_name, None)
            pwmap[profile["name"]] = password
            self.owner.persist()
            self.refresh()
        StorageProfileEditor(self, prof, pwd, saver)

    def delete(self):
        idx = self._index()
        if idx is None:
            return
        prof = self.profiles()[idx]
        if not messagebox.askyesno("Удаление", f"Удалить хранилище «{prof.get('name')}»?", parent=self):
            return
        name = prof.get("name", "")
        self.profiles().pop(idx)
        self.owner.sec.setdefault("ftp_profile_passwords", {}).pop(name, None)
        for job in self.owner.jobs:
            if job.get("ftp_profile") == name:
                job["ftp_profile"] = ""
                job["ftp_enabled"] = False
        self.owner.persist()
        self.refresh()


class TaskTypeDialog(tk.Toplevel):
    CHOICES = [
        ("Резервное копирование", "Резервное копирование файлов и баз данных", "files"),
        ("Резервное копирование", "Резервное копирование 1С:Предприятие 8 / BAS", "1c"),
        ("Резервное копирование", "Резервное копирование Microsoft SQL Server", "mssql"),
        ("Дополнительно 1С:Предприятие", "Тестирование и исправление 1С:Предприятие 8 / BAS", "1c_check"),
        ("Дополнительно 1С:Предприятие", "Вызов процедуры 1С:Предприятие 8 / BAS", "1c_procedure"),
        ("Другие задачи", "Дополнительное копирование", "extra_copy"),
        ("Другие задачи", "Произвольные команды", "commands"),
        ("Отчеты", "Отправка отчетов", "report"),
    ]
    def __init__(self, owner, on_select):
        super().__init__(owner)
        self.title("Выберите тип новой задачи")
        self.geometry("490x390")
        self.transient(owner)
        self.grab_set()
        self.on_select = on_select
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frm, show="tree")
        self.tree.pack(fill="both", expand=True)
        groups = {}
        for grp, label, kind in self.CHOICES:
            if grp not in groups:
                groups[grp] = self.tree.insert("", "end", text=tr(getattr(owner, "cfg", {}).get("ui_language", "ru"), grp), open=True)
            self.tree.insert(groups[grp], "end", iid=kind, text=tr(getattr(owner, "cfg", {}).get("ui_language", "ru"), label))
        self.tree.bind("<Double-1>", lambda e: self.confirm())
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Создать", command=self.confirm).pack(side="right")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=8)
        self.after_idle(lambda: translate_widgets(self, getattr(owner, "cfg", {}).get("ui_language", "ru")))

    def confirm(self):
        sel = self.tree.selection()
        if not sel:
            return
        kind = sel[0]
        if kind not in {"1c", "1c_check", "1c_procedure", "mssql", "files", "extra_copy", "commands", "report"}:
            return
        self.on_select(kind)
        self.destroy()


class EffectorTaskEditor(tk.Toplevel):
    def __init__(self, owner, job: dict, on_save):
        super().__init__(owner)
        self.owner = owner
        self.backend = owner.backend
        self.job = deepcopy(job)
        self.on_save = on_save
        self.title(f"Задача: {self.job.get('name') or 'Новое задание'}")
        self.geometry("920x690")
        self.minsize(790, 590)
        self.transient(owner)
        self.grab_set()
        self.v = {}
        self._build_variables()
        self._build()
        self.after_idle(lambda: translate_widgets(self, self.owner.cfg.get("ui_language", "ru")))

    def _build_variables(self):
        def sv(key, fallback=""):
            self.v[key] = tk.StringVar(value=str(self.job.get(key, fallback)))
        def bv(key, fallback=False):
            self.v[key] = tk.BooleanVar(value=bool(self.job.get(key, fallback)))
        for field in ["name", "ref", "ib_ref", "ib_user", "one_c_server", "one_c_file", "one_c_url", "one_c_ras",
                      "one_c_exe", "sql_server", "sql_database", "sql_backup_dir", "sql_driver", "sql_user",
                      "folder", "keep", "archive_format", "platform_mode", "ftp_profile", "cluster_user", "wait_seconds_override", "archive_prefix",
                      "procedure_name", "procedure_epf", "procedure_parameter", "procedure_timeout_hours", "check_time_hours", "check_time_minutes",
                      "file_wait_seconds", "file_session_hosts", "file_base_aliases"]:
            sv(field)
        for field in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth", "sql_trust_cert", "sql_remove_stage",
                      "stop_after_enabled", "commands_enabled", "check_reindex", "check_logical", "check_refs", "check_totals", "check_compress", "check_rebuild",
                      "repair_confirmed", "file_offline_confirmed", "procedure_exclusive", "file_force_confirmed",
                       "file_web_maintenance_enabled"]:
            bv(field)
        self.v["ib_password"] = tk.StringVar(value=self.owner.sec.get("db_passwords", {}).get(
            self.backend.job_key(self.job), self.owner.sec.get("db_passwords", {}).get(self.job.get("ref", ""), "")))
        self.v["sql_password"] = tk.StringVar(value=self.owner.sec.get("sql_passwords", {}).get(self.backend.job_key(self.job), ""))
        self.v["cluster_job_password"] = tk.StringVar(value=self.owner.sec.get("cluster_job_passwords", {}).get(self.backend.job_key(self.job), ""))
        self.v["check_mode"] = tk.StringVar(value=self.job.get("check_mode", "test"))
        self.v["repair_bad_refs"] = tk.StringVar(value=self.job.get("repair_bad_refs", "none"))
        self.v["repair_bad_data"] = tk.StringVar(value=self.job.get("repair_bad_data", "none"))
        self.v["force_terminate_override"] = tk.StringVar(value=(
            "По умолчанию" if self.job.get("force_terminate_override") is None
            else "Да" if self.job["force_terminate_override"] else "Нет"))
        selected_targets = self.job.get("ftp_targets") or ([self.job["ftp_profile"]] if self.job.get("ftp_profile") else [])
        self.selected_ftp_profiles = set(selected_targets)
        self.legacy_ftp_selected = bool(self.job.get("ftp_enabled", False) and not selected_targets)
        self.v["connection_kind"] = tk.StringVar(value=self.job.get("connection_kind", "server"))
        self.v["file_disconnect_mode"] = tk.StringVar(value=self.job.get("file_disconnect_mode", "wait_only"))
        self.v["file_auto_disconnect"] = tk.BooleanVar(value=bool(
            self.job.get("file_force_confirmed") and self.job.get("file_disconnect_mode", "wait_only") != "wait_only"))
        self.v["command_before"] = tk.StringVar(value=str(self.job.get("command_before", "")))
        self.v["command_after"] = tk.StringVar(value=str(self.job.get("command_after", "")))
        self.v["command_body"] = tk.StringVar(value=str(self.job.get("command_body", "")))
        for field in ("notify_email", "notify_telegram", "notify_max", "notify_on_success", "notify_on_failure"):
            self.v[field] = tk.BooleanVar(value=bool(self.job.get(field, False)))
        self.v["stop_after_hours"] = tk.StringVar(value=str(self.job.get("stop_after_hours", 24)))
        self.v["stop_after_minutes"] = tk.StringVar(value=str(self.job.get("stop_after_minutes", 0)))
        tm = str(self.job.get("schedule_time", ""))
        h, m = (tm.split(":") + [""])[:2] if ":" in tm else ("", "")
        self.hour = tk.StringVar(value=h)
        self.minute = tk.StringVar(value=m)
        self.kind_display = tk.StringVar(value=SOURCE_DISPLAY.get(self.v["connection_kind"].get(), "Клиент-серверная"))
        self.platform_display = tk.StringVar(value=PLATFORM_DISPLAY.get(self.v["platform_mode"].get() or "inherit", "Автоматически (последняя)"))

    def page_names(self):
        kind = self.job.get("type", "1c")
        if kind == "1c":
            return ["Подключение к базе", "Отключение пользователей", "Хранилище архивов", "Файл архива", "Выполнить команды", "Расписание автозапуска", "Прервать выполнение", "Отправка отчетов"]
        if kind in {"1c_check", "1c_procedure"}:
            middle = "Тестирование" if kind == "1c_check" else "Выполнение процедуры"
            return ["Подключение к базе", "Отключение пользователей", middle,
                    "Выполнить команды", "Расписание автозапуска", "Прервать выполнение"]
        if kind == "commands":
            return ["Основные параметры", "Произвольные команды", "Расписание автозапуска", "Прервать выполнение"]
        if kind == "report":
            return ["Основные параметры", "Отправка отчетов", "Расписание автозапуска"]
        if kind == "mssql":
            return ["Основные параметры", "База Microsoft SQL", "Хранилище архивов", "Файл архива", "Выполнить команды", "Расписание автозапуска", "Прервать выполнение", "Отправка отчетов"]
        if kind == "extra_copy":
            return ["Основные параметры", "Источники файлов", "Хранилище архивов", "Файл архива", "Выполнить команды", "Расписание автозапуска", "Прервать выполнение", "Отправка отчетов"]
        return ["Основные параметры", "Источники файлов", "Хранилище архивов", "Файл архива", "Выполнить команды", "Расписание автозапуска", "Прервать выполнение", "Отправка отчетов"]

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        top = ttk.Frame(root)
        top.pack(fill="x")
        ttk.Label(top, text="Вид задачи:").pack(side="left")
        kind_labels = {"1c": "Резервное копирование 1С:Предприятие 8 / BAS",
                       "mssql": "Резервное копирование Microsoft SQL Server",
                       "files": "Резервное копирование файлов и баз данных",
                       "extra_copy": "Дополнительное копирование",
                       "commands": "Произвольные команды", "report": "Отправка отчетов",
                       "1c_check": "Тестирование и исправление 1С:Предприятие 8 / BAS",
                       "1c_procedure": "Вызов процедуры 1С:Предприятие 8 / BAS"}
        ttk.Label(top, text=kind_labels.get(self.job.get("type"), "Задание")).pack(side="left", padx=(6,0))
        ttk.Label(top, text="Наименование:").pack(side="left", padx=(30,6))
        ttk.Entry(top, textvariable=self.v["name"]).pack(side="left", fill="x", expand=True)

        center = ttk.Frame(root)
        center.pack(fill="both", expand=True, pady=(10, 0))
        left = ttk.Frame(center)
        left.pack(side="left", fill="y", padx=(0, 10))
        self.nav = tk.Listbox(left, width=26, exportselection=False, font=("Segoe UI", 10),
                              borderwidth=1, relief="sunken")
        self.nav.pack(fill="y", expand=True)
        for item in self.page_names():
            self.nav.insert("end", item)
        right = ttk.Frame(center)
        right.pack(side="left", fill="both", expand=True)
        self.pages = []
        names = self.page_names()
        for _ in names:
            p = ScrollablePage(right)
            p.place(x=0, y=0, relwidth=1, relheight=1)
            self.pages.append(p)
        kind = self.job.get("type", "1c")
        if kind in {"1c_check", "1c_procedure"}:
            self.page_1c_source(self.pages[0].content)
            self.page_1c_disconnect(self.pages[1].content)
            if kind == "1c_check":
                self.page_1c_check(self.pages[2].content)
            else:
                self.page_1c_procedure(self.pages[2].content)
            self.page_commands(self.pages[3].content)
            self.page_schedule(self.pages[4].content)
            self.page_timeout(self.pages[5].content)
        elif kind in {"commands", "report"}:
            self.page_basic(self.pages[0].content)
            if kind == "commands":
                self.page_command_task(self.pages[1].content)
                self.page_schedule(self.pages[2].content)
                self.page_timeout(self.pages[3].content)
            else:
                self.page_reports(self.pages[1].content, summary_task=True)
                self.page_schedule(self.pages[2].content)
        else:
            if kind == "1c":
                self.page_1c_source(self.pages[0].content)
                self.page_1c_disconnect(self.pages[1].content)
            elif kind == "mssql":
                self.page_basic(self.pages[0].content)
                self.page_sql(self.pages[1].content)
            else:
                self.page_basic(self.pages[0].content)
                self.page_files(self.pages[1].content)
            self.page_storage(self.pages[2].content)
            self.page_archive(self.pages[3].content)
            self.page_commands(self.pages[4].content)
            self.page_schedule(self.pages[5].content)
            self.page_timeout(self.pages[6].content)
            self.page_reports(self.pages[7].content)
        self.nav.bind("<<ListboxSelect>>", self._select_page)
        self.nav.selection_set(0)
        self.pages[0].lift()

        btns = ttk.Frame(root)
        btns.pack(fill="x", pady=(10,0))
        ttk.Button(btns, text="<", width=3, command=self.prev_page).pack(side="left")
        ttk.Button(btns, text=">", width=3, command=self.next_page).pack(side="left", padx=(6,10))
        ttk.Button(btns, text="Проверить", command=self.check).pack(side="left")
        ttk.Button(btns, text="Сохранить", command=self.save).pack(side="right")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=8)

    def _select_page(self, _=None):
        cur = self.nav.curselection()
        if cur:
            self.pages[cur[0]].lift()

    def prev_page(self):
        cur = self.nav.curselection()[0] if self.nav.curselection() else 0
        cur = max(0, cur - 1)
        self.nav.selection_clear(0, "end")
        self.nav.selection_set(cur)
        self.pages[cur].lift()

    def next_page(self):
        cur = self.nav.curselection()[0] if self.nav.curselection() else 0
        cur = min(len(self.pages)-1, cur + 1)
        self.nav.selection_clear(0, "end")
        self.nav.selection_set(cur)
        self.pages[cur].lift()

    def page_basic(self, page):
        section_bar(page, "Основные параметры")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Идентификатор / Ref", self.v["ref"], 0)
        ttk.Checkbutton(frm, text="Задание активно", variable=self.v["enabled"]).grid(row=1, column=1, sticky="w", pady=8)

    def page_1c_source(self, page):
        section_bar(page, "Параметры подключения к информационной базе")
        ttk.Button(page, text="Выбрать информационную базу из списка и заполнить основные параметры",
                   command=self.choose_launcher_base).pack(anchor="w", pady=(0, 10))
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Идентификатор / Ref", self.v["ref"], 0)
        ttk.Label(frm, text="Исполняемый файл:").grid(row=1, column=0, sticky="w", pady=5)
        mode = ttk.Combobox(frm, textvariable=self.platform_display, values=list(PLATFORM_DISPLAY.values()), state="readonly", width=28)
        mode.grid(row=1, column=1, sticky="w", pady=5)
        add_line(frm, "1cv8.exe (ручной режим)", self.v["one_c_exe"], 2)
        ttk.Button(frm, text="...", width=3, command=self.browse_exe).grid(row=2, column=2, padx=5)
        ttk.Label(frm, text="Вариант базы:").grid(row=3, column=0, sticky="w", pady=5)
        kind_box = ttk.Combobox(frm, textvariable=self.kind_display, state="readonly", values=list(SOURCE_DISPLAY.values()), width=28)
        kind_box.grid(row=3, column=1, sticky="w", pady=5)
        kind_box.bind("<<ComboboxSelected>>", lambda e: self.change_source_kind())
        self.variant = ttk.Frame(page)
        self.variant.pack(fill="x", pady=10)
        self.variant_groups = {}
        c = ttk.LabelFrame(self.variant, text="Клиент-серверная", padding=8)
        self.variant_groups["server"] = c
        add_line(c, "Сервер 1С", self.v["one_c_server"], 0)
        add_line(c, "Имя базы", self.v["ib_ref"], 1)
        add_line(c, "Пользователь", self.v["ib_user"], 2)
        add_line(c, "Пароль", self.v["ib_password"], 3, password=True)
        ttk.Label(c, text="Сервер указывается в выбранной базе из списка 1С. При ручном создании заполните его самостоятельно.",
                  foreground="#52677b", wraplength=520).grid(row=4, column=0, columnspan=2, sticky="w", pady=5)
        c = ttk.LabelFrame(self.variant, text="Файловая база", padding=8)
        self.variant_groups["file"] = c
        add_line(c, "Каталог базы", self.v["one_c_file"], 0)
        ttk.Button(c, text="...", width=3, command=lambda: self.browse_dir("one_c_file")).grid(row=0, column=2, padx=5)
        add_line(c, "Пользователь", self.v["ib_user"], 1)
        add_line(c, "Пароль", self.v["ib_password"], 2, password=True)
        c = ttk.LabelFrame(self.variant, text="Веб-подключение", padding=8)
        self.variant_groups["web"] = c
        add_line(c, "URL", self.v["one_c_url"], 0)
        ttk.Label(c, text="Ws-подключение отображается, но прямой DumpIB не выполняется.", foreground="#a9542b").grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        self.refresh_source_kind()

    def page_1c_disconnect(self, page):
        section_bar(page, "Завершение работы пользователей")
        mode = self.v["connection_kind"].get()
        if mode == "file":
            ttk.Label(page,
                      text="Способ подключения определяется автоматически: напрямую, "
                           "SMB, IIS или Apache. Настройки отдельной конфигурации 1С "
                           "и названия пулов не требуются.",
                      wraplength=560, justify="left").pack(anchor="w", pady=(10, 18))
            ttk.Checkbutton(
                page,
                text="Автоматически завершать пользователей перед созданием копии",
                variable=self.v["file_auto_disconnect"]
            ).pack(anchor="w", pady=8)
            ttk.Checkbutton(
                page,
                text="Разрешаю временно останавливать IIS и Apache при необходимости",
                variable=self.v["file_web_maintenance_enabled"]
            ).pack(anchor="w", pady=8)
            ttk.Label(
                page,
                text="Программа найдёт нужную публикацию по файлу default.vrd, "
                     "определит её пул IIS или службу Apache, создаст временную "
                     "копию базы и восстановит веб-сервер ДО формирования .dt. "
                     "Если веб-публикация не обнаружена, используются доступные "
                     "механизмы отключения клиентских процессов 1С.",
                wraplength=560, justify="left"
            ).pack(anchor="w", pady=(14, 8))
            ttk.Label(
                page,
                text="ВНИМАНИЕ: общий пул IIS или служба Apache может обслуживать "
                     "несколько сайтов и баз 1С. Их работа будет прервана, "
                     "а несохранённые изменения пользователей могут быть потеряны. "
                     "Остановка выполняется только при подтверждённой публикации "
                     "именно выбранной базы; аварийное восстановление регистрируется "
                     "в Планировщике Windows.",
                wraplength=560, justify="left", foreground="#9a482b"
            ).pack(anchor="w", pady=12)
        else:
            ttk.Label(page, text="Клиент-серверная база: остановка сеансов через RAS.",
                      wraplength=540, justify="left").pack(anchor="w", pady=7)
            frm = ttk.Frame(page)
            frm.pack(fill="x", pady=12)
            add_line(frm, "Ждать выхода пользователей, сек.", self.v["wait_seconds_override"], 0)
            ttk.Label(frm, text="Принудительно завершать сеансы").grid(row=1, column=0, sticky="w", pady=6)
            ttk.Combobox(frm, textvariable=self.v["force_terminate_override"],
                         values=["По умолчанию", "Да", "Нет"], state="readonly").grid(row=1, column=1, sticky="w", pady=6)
            self.cluster_auth_expanded = tk.BooleanVar(value=bool(
                self.v["cluster_user"].get() or self.v["cluster_job_password"].get()))
            auth_box = ttk.Frame(page)
            def toggle_cluster_auth():
                if self.cluster_auth_expanded.get():
                    auth_box.pack(fill="x", pady=4)
                else:
                    auth_box.pack_forget()
            ttk.Checkbutton(page, text="Кластер 1С требует отдельного администратора (дополнительно)",
                            variable=self.cluster_auth_expanded,
                            command=toggle_cluster_auth).pack(anchor="w", pady=(8, 0))
            auth_fields = ttk.Frame(auth_box)
            auth_fields.pack(fill="x")
            add_line(auth_fields, "Администратор кластера (не ИБ)", self.v["cluster_user"], 0)
            add_line(auth_fields, "Пароль администратора кластера", self.v["cluster_job_password"], 1, password=True)
            toggle_cluster_auth()

    def page_1c_check(self, page):
        section_bar(page, "Параметры тестирования и исправления 1С 8")
        ttk.Label(page, text="По умолчанию выполняется только проверка без исправлений.",
                  foreground="#52677b").pack(anchor="w", pady=(0, 7))
        options = ttk.Frame(page)
        options.pack(fill="x")
        for i, (key, label) in enumerate([
            ("check_reindex", "Реиндексация таблиц"),
            ("check_logical", "Проверка логической целостности"),
            ("check_refs", "Проверка ссылочной целостности"),
            ("check_totals", "Пересчёт итогов"),
            ("check_compress", "Сжатие таблиц"),
            ("check_rebuild", "Реструктуризация таблиц")]):
            ttk.Checkbutton(options, text=label, variable=self.v[key]).grid(row=i, column=0, sticky="w", pady=3)
        mode = ttk.LabelFrame(page, text="Режим выполнения", padding=8)
        mode.pack(fill="x", pady=(12, 7))
        ttk.Radiobutton(mode, text="Только тестирование (без изменений)", value="test",
                        variable=self.v["check_mode"]).pack(anchor="w")
        ttk.Radiobutton(mode, text="Тестирование и исправление", value="repair",
                        variable=self.v["check_mode"]).pack(anchor="w")
        ttk.Label(mode, text="Исправление создаёт свежую локальную .dt-копию до начала изменений.",
                  foreground="#915126").pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(mode, text="Подтверждаю выполнение исправлений и создание предварительной .dt-копии",
                        variable=self.v["repair_confirmed"]).pack(anchor="w", pady=3)
        ttk.Checkbutton(mode, text="Для файловой базы: подтверждаю, что все пользователи вышли",
                        variable=self.v["file_offline_confirmed"]).pack(anchor="w")
        prefs = ttk.Frame(page)
        prefs.pack(fill="x", pady=(5, 0))
        ttk.Label(prefs, text="Ошибки ссылок:").grid(row=0, column=0, sticky="w", padx=(0,8), pady=3)
        ttk.Combobox(prefs, textvariable=self.v["repair_bad_refs"], state="readonly",
                     values=["none", "create", "clear"], width=16).grid(row=0, column=1, sticky="w")
        ttk.Label(prefs, text="Частичная потеря данных:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(prefs, textvariable=self.v["repair_bad_data"], state="readonly",
                     values=["none", "create", "delete"], width=16).grid(row=1, column=1, sticky="w")
        ttk.Label(prefs, text="Ограничение (час / мин):").grid(row=2, column=0, sticky="w", pady=3)
        timebox = ttk.Frame(prefs)
        timebox.grid(row=2, column=1, sticky="w")
        ttk.Spinbox(timebox, from_=0, to=999, textvariable=self.v["check_time_hours"], width=5).pack(side="left")
        ttk.Label(timebox, text=" : ").pack(side="left")
        ttk.Spinbox(timebox, from_=0, to=59, textvariable=self.v["check_time_minutes"], width=4).pack(side="left")

    def page_1c_procedure(self, page):
        section_bar(page, "Выполнение процедуры 1С 8 через внешнюю обработку")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        ttk.Label(frm, text="Процедура:").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Combobox(frm, textvariable=self.v["procedure_name"],
                     values=["Выполнение обмена", "Внешняя обработка"], width=30).grid(row=0, column=1, sticky="w")
        add_line(frm, "Файл обработки (.epf)", self.v["procedure_epf"], 1)
        ttk.Button(frm, text="...", width=3, command=self.browse_epf).grid(row=1, column=2, padx=5)
        add_line(frm, "Параметр запуска /C", self.v["procedure_parameter"], 2)
        add_line(frm, "Таймаут, часов", self.v["procedure_timeout_hours"], 3)
        ttk.Checkbutton(frm, text="Завершить сеансы пользователей перед выполнением (серверная база)",
                        variable=self.v["procedure_exclusive"]).grid(row=4, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(page, text=("Для запуска процедуры нужна подготовленная внешняя обработка .epf, "
                               "которая сама выполняет нужный код и закрывает 1С после окончания. "
                               "Прямой универсальный вызов произвольного модуля конфигурации недоступен."),
                  wraplength=510, foreground="#915126", justify="left").pack(anchor="w", pady=12)

    def browse_epf(self):
        filename = filedialog.askopenfilename(parent=self, title="Выберите обработку 1С",
                                              filetypes=[("Внешние обработки 1С", "*.epf"), ("Все файлы", "*.*")])
        if filename:
            self.v["procedure_epf"].set(filename)

    def page_sql(self, page):
        section_bar(page, "Включить в архив backup базы Microsoft SQL")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Сервер", self.v["sql_server"], 0)
        ttk.Checkbutton(frm, text="SQL - авторизация", variable=tk.BooleanVar(value=not self.v["sql_windows_auth"].get()), command=lambda: self.v["sql_windows_auth"].set(not self.v["sql_windows_auth"].get())).grid(row=1, column=1, sticky="w", pady=5)
        add_line(frm, "Пользователь", self.v["sql_user"], 2)
        add_line(frm, "Пароль", self.v["sql_password"], 3, password=True)
        add_line(frm, "База", self.v["sql_database"], 4)
        add_line(frm, "Каталог временного .bak", self.v["sql_backup_dir"], 5)
        add_line(frm, "ODBC Driver", self.v["sql_driver"], 6)

    def page_files(self, page):
        section_bar(page, "Источники файлов и каталогов")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Идентификатор / Ref", self.v["ref"], 0)
        ttk.Checkbutton(frm, text="Задание активно", variable=self.v["enabled"]).grid(row=1, column=1, sticky="w", pady=8)
        self.filebox = tk.Text(page, height=18, font=("Consolas", 10))
        self.filebox.pack(fill="both", expand=True, pady=(8,0))
        self.filebox.insert("1.0", self.job.get("file_paths", ""))
        b = ttk.Frame(page)
        b.pack(fill="x", pady=8)
        ttk.Button(b, text="+ Файл", command=self.add_file).pack(side="left")
        ttk.Button(b, text="+ Папка", command=self.add_folder).pack(side="left", padx=6)

    def page_storage(self, page):
        section_bar(page, "Укажите, где следует хранить создаваемые архивы")
        tools = ttk.Frame(page)
        tools.pack(fill="x", pady=(0, 7))
        ttk.Button(tools, text="+", width=3, command=self.manage_profiles).pack(side="left")
        ttk.Button(tools, text="✎", width=3, command=self.edit_selected_storage).pack(side="left", padx=4)
        ttk.Button(tools, text="−", width=3, command=self.remove_selected_storage).pack(side="left")
        self.storage_tree = ttk.Treeview(page, columns=("copies",), show="tree headings", height=10)
        self.storage_tree.heading("#0", text="Хранилище (галочка — используется)")
        self.storage_tree.heading("copies", text="Копий")
        self.storage_tree.column("#0", width=355, stretch=True)
        self.storage_tree.column("copies", width=80, anchor="center", stretch=False)
        self.storage_tree.pack(fill="both", expand=True)
        self.storage_tree.bind("<Double-1>", self.toggle_storage)
        frm = ttk.Frame(page)
        frm.pack(fill="x", pady=(10, 0))
        add_line(frm, "Локальная папка", self.v["folder"], 0)
        ttk.Button(frm, text="...", width=3, command=lambda: self.browse_dir("folder")).grid(row=0, column=2)
        add_line(frm, "Количество локальных копий", self.v["keep"], 1)
        self.v["keep"].trace_add("write", lambda *_: self.refresh_storage_tree())
        self.refresh_storage_tree()
        ttk.Label(page, text="Локальная копия обязательна. Двойной щелчок по FTP включает/отключает отправку.\n"
                             "Профили FTP можно создавать, изменять и тестировать отдельно.",
                  foreground="#52677b", justify="left").pack(anchor="w", pady=(10, 0))

    def refresh_storage_tree(self):
        if not hasattr(self, "storage_tree"):
            return
        self.storage_tree.delete(*self.storage_tree.get_children())
        self.storage_tree.insert("", "end", iid="local", text="☑ Локальный архив", values=(self.v["keep"].get() or "7",))
        if self.owner.cfg.get("ftp", {}).get("host"):
            mark = "☑" if self.legacy_ftp_selected else "☐"
            self.storage_tree.insert("", "end", iid="legacy", text=f"{mark} FTP из прежних настроек", values=("—",))
        for idx, prof in enumerate(self.owner.cfg.get("ftp_profiles", [])):
            name = str(prof.get("name", ""))
            checked = "☑" if name in self.selected_ftp_profiles else "☐"
            proto = "FTPS" if prof.get("tls", True) else "FTP"
            self.storage_tree.insert("", "end", iid=f"ftp_{idx}",
                                     text=f"{checked} {name} [{proto}]", values=("—",))

    def toggle_storage(self, _event=None):
        selection = self.storage_tree.selection()
        if not selection:
            return
        iid = selection[0]
        if iid == "legacy":
            self.legacy_ftp_selected = not self.legacy_ftp_selected
            if self.legacy_ftp_selected:
                self.selected_ftp_profiles.clear()
        elif iid.startswith("ftp_"):
            idx = int(iid[4:])
            name = self.owner.cfg["ftp_profiles"][idx]["name"]
            if name in self.selected_ftp_profiles:
                self.selected_ftp_profiles.discard(name)
            else:
                self.selected_ftp_profiles.add(name)
                self.legacy_ftp_selected = False
        self.refresh_storage_tree()

    def edit_selected_storage(self):
        selection = self.storage_tree.selection()
        if selection and selection[0] == "local":
            self.browse_dir("folder")
        else:
            self.manage_profiles()

    def remove_selected_storage(self):
        selection = self.storage_tree.selection()
        if not selection:
            return
        if selection[0] == "local":
            messagebox.showinfo("Хранилища", "Локальное хранилище требуется для резервной копии.", parent=self)
            return
        if selection[0] == "legacy":
            self.legacy_ftp_selected = False
        elif selection[0].startswith("ftp_"):
            idx = int(selection[0][4:])
            self.selected_ftp_profiles.discard(self.owner.cfg["ftp_profiles"][idx]["name"])
        self.refresh_storage_tree()


    def page_archive(self, page):
        section_bar(page, "Параметры файла архива")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Имя файла архива (пусто — Ref)", self.v["archive_prefix"], 0)
        ttk.Label(frm, text="Окончание имени архива:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Label(frm, text="yyyy-mm-dd_hh-mm-ss", foreground="#52677b").grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(frm, text="Формат:").grid(row=2, column=0, sticky="w", pady=5)
        values = ["none", "zip", "7z"] if self.job.get("type") not in {"files", "extra_copy"} else ["zip", "7z"]
        ttk.Combobox(frm, textvariable=self.v["archive_format"], values=values, state="readonly", width=20).grid(row=2, column=1, sticky="w", pady=5)
        ttk.Checkbutton(frm, text="Удалять исходный .dt / .bak после успешной упаковки", variable=self.v["remove_raw_after_pack"]).grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(frm, text="Для 1С и MS SQL доступно: none (без упаковки), zip и 7z. Для файлов — zip и 7z.", foreground="#52677b").grid(row=4, column=0, columnspan=2, sticky="w", pady=6)

    def page_commands(self, page):
        section_bar(page, "Выполнить команды системы")
        ttk.Checkbutton(page, text="Разрешить выполнение команд перед/после задания",
                        variable=self.v["commands_enabled"]).pack(anchor="w", pady=(0, 10))
        ttk.Label(page, text="Выполнить команды перед выполнением задачи").pack(anchor="w")
        self.cmd_before = tk.Text(page, height=8)
        self.cmd_before.pack(fill="x", pady=(4,10))
        self.cmd_before.insert("1.0", self.v["command_before"].get())
        ttk.Label(page, text="Выполнить команды после выполнения задачи").pack(anchor="w")
        self.cmd_after = tk.Text(page, height=8)
        self.cmd_after.pack(fill="x", pady=(4,10))
        self.cmd_after.insert("1.0", self.v["command_after"].get())
        ttk.Label(page, text="Команды запускаются от имени пользователя Windows-службы, по одной строке.\n"
                             "После задания команды выполняются даже при ошибке бэкапа. Логины/пароли в командах не скрываются внутри Windows.",
                  foreground="#52677b", wraplength=560, justify="left").pack(anchor="w")

    def page_command_task(self, page):
        section_bar(page, "Произвольные команды")
        ttk.Label(page, text="Команды выполняются последовательно от имени пользователя службы Windows.",
                  foreground="#52677b", wraplength=570).pack(anchor="w", pady=8)
        self.commandbox = tk.Text(page, height=17, font=("Consolas", 10))
        self.commandbox.pack(fill="both", expand=True)
        self.commandbox.insert("1.0", self.v["command_body"].get())
        ttk.Label(page, text="Каждая непустая строка — отдельная команда. Не храните пароли в командной строке.",
                  foreground="#52677b", wraplength=570).pack(anchor="w", pady=8)

    def page_reports(self, page, summary_task=False):
        section_bar(page, "Отправка отчетов")
        ttk.Label(page, text="Настройка SMTP, Telegram и MAX выполняется в меню Сервис → Параметры программы → Отчеты.",
                  foreground="#52677b", wraplength=570).pack(anchor="w", pady=(0, 12))
        group = ttk.LabelFrame(page, text="Каналы отправки", padding=12)
        group.pack(fill="x")
        ttk.Checkbutton(group, text="Отправлять отчет по электронной почте (SMTP)",
                        variable=self.v["notify_email"]).pack(anchor="w", pady=5)
        ttk.Checkbutton(group, text="Отправлять отчет в Telegram", variable=self.v["notify_telegram"]).pack(anchor="w", pady=5)
        ttk.Checkbutton(group, text="Отправлять отчет в MAX", variable=self.v["notify_max"]).pack(anchor="w", pady=5)
        if not summary_task:
            triggers = ttk.LabelFrame(page, text="Когда отправлять", padding=12)
            triggers.pack(fill="x", pady=12)
            ttk.Checkbutton(triggers, text="После успешного выполнения задачи", variable=self.v["notify_on_success"]).pack(anchor="w", pady=5)
            ttk.Checkbutton(triggers, text="При ошибке выполнения задачи", variable=self.v["notify_on_failure"]).pack(anchor="w", pady=5)
        else:
            ttk.Label(page, text="Эта задача отправляет сводку последних результатов всех настроенных заданий по расписанию.",
                      foreground="#52677b", wraplength=570).pack(anchor="w", pady=12)

    def page_schedule(self, page):
        section_bar(page, "Расписание")
        panel = ttk.Frame(page)
        panel.pack(fill="x")
        ttk.Checkbutton(panel, text="Запускать по расписанию", variable=self.v["enabled"]).pack(anchor="w", pady=6)
        ttk.Label(panel, text="Периодичность:").pack(anchor="w")
        ttk.Combobox(panel, values=["Ежедневно"], state="readonly", width=20).pack(anchor="w", pady=5)
        ttk.Label(panel, text="Время начала:").pack(anchor="w", pady=(8,0))
        spin_time(panel, self.hour, self.minute).pack(anchor="w", pady=5)

    def page_timeout(self, page):
        section_bar(page, "Принудительное завершение работы задачи")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        ttk.Checkbutton(frm, text="Прервать выполнение задачи через", variable=self.v["stop_after_enabled"]).grid(row=0, column=0, columnspan=4, sticky="w", pady=5)
        ttk.Spinbox(frm, from_=0, to=999, textvariable=self.v["stop_after_hours"], width=6).grid(row=1, column=0, sticky="w")
        ttk.Label(frm, text="час.").grid(row=1, column=1, sticky="w", padx=(4,18))
        ttk.Spinbox(frm, from_=0, to=59, textvariable=self.v["stop_after_minutes"], width=6).grid(row=1, column=2, sticky="w")
        ttk.Label(frm, text="мин.").grid(row=1, column=3, sticky="w", padx=(4,0))
        ttk.Label(frm, text="Параметр сохраняется в конфигурации задачи.", foreground="#52677b").grid(row=2, column=0, columnspan=4, sticky="w", pady=8)

    def manage_profiles(self):
        dlg = StorageProfilesDialog(self.owner)
        self.wait_window(dlg)
        valid = {p.get("name") for p in self.owner.cfg.get("ftp_profiles", [])}
        self.selected_ftp_profiles.intersection_update(valid)
        self.refresh_storage_tree()

    def change_source_kind(self):
        self.v["connection_kind"].set(next((key for key, label in SOURCE_DISPLAY.items() if label == source_value(self.kind_display.get())), "server"))
        self.refresh_source_kind()

    def refresh_source_kind(self):
        for group in getattr(self, 'variant_groups', {}).values():
            group.pack_forget()
        kind = self.v["connection_kind"].get()
        if kind in getattr(self, 'variant_groups', {}):
            self.variant_groups[kind].pack(fill="x")

    def choose_launcher_base(self):
        InfobaseImportDialog(self, on_select=self.set_launcher_base)

    def set_launcher_base(self, item):
        self.v["name"].set(item.name)
        self.v["connection_kind"].set(item.kind)
        self.kind_display.set(tr(self.owner.cfg.get("ui_language", "ru"), SOURCE_DISPLAY[item.kind]))
        if item.kind == "server":
            self.v["ib_ref"].set(item.ref)
            self.v["ref"].set(item.ref)
            self.v["one_c_server"].set(item.server)
        elif item.kind == "file":
            self.v["one_c_file"].set(item.path)
            self.v["ref"].set(item.name)
        else:
            self.v["one_c_url"].set(item.url)
            self.v["ref"].set(item.name)
            self.v["enabled"].set(False)
        self.refresh_source_kind()

    def browse_dir(self, key):
        result = filedialog.askdirectory(parent=self, title="Выберите каталог")
        if result:
            self.v[key].set(result)

    def browse_exe(self):
        result = filedialog.askopenfilename(parent=self, title="Укажите 1cv8.exe", filetypes=[("Исполняемые файлы", "*.exe")])
        if result:
            self.v["one_c_exe"].set(result)
            self.v["platform_mode"].set("manual")
            self.platform_display.set(tr(self.owner.cfg.get("ui_language", "ru"), PLATFORM_DISPLAY["manual"]))

    def add_file(self):
        path = filedialog.askopenfilename(parent=self)
        if path:
            self.filebox.insert("end", ("\n" if self.filebox.get("1.0", "end-1c").strip() else "") + path)

    def add_folder(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.filebox.insert("end", ("\n" if self.filebox.get("1.0", "end-1c").strip() else "") + path)

    def check(self):
        try:
            self.extract()
            messagebox.showinfo("Проверить", "Поля задания заполнены корректно.", parent=self)
        except Exception as exc:
            messagebox.showerror("Проверить", str(exc), parent=self)

    def extract(self):
        job = deepcopy(self.job)
        for key in ["name", "ref", "ib_ref", "ib_user", "one_c_server", "one_c_file", "one_c_url", "one_c_ras",
                    "one_c_exe", "sql_server", "sql_database", "sql_backup_dir", "sql_driver", "sql_user",
                    "folder", "archive_format", "platform_mode", "ftp_profile", "cluster_user", "wait_seconds_override", "archive_prefix",
                    "procedure_name", "procedure_epf", "procedure_parameter", "procedure_timeout_hours", "check_time_hours", "check_time_minutes",
                    "file_wait_seconds", "file_session_hosts", "file_base_aliases"]:
            job[key] = self.v[key].get().strip()
        for field in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth", "sql_trust_cert", "sql_remove_stage", "stop_after_enabled", "commands_enabled", "notify_email", "notify_telegram", "notify_max", "notify_on_success", "notify_on_failure", "check_reindex", "check_logical", "check_refs", "check_totals", "check_compress", "check_rebuild",
                      "repair_confirmed", "file_offline_confirmed", "procedure_exclusive", "file_force_confirmed",
                       "file_web_maintenance_enabled"]:
            job[field] = bool(self.v[field].get())
        job["keep"] = max(1, int(self.v["keep"].get() or 7))
        job["ftp_targets"] = sorted(self.selected_ftp_profiles)
        job["ftp_enabled"] = bool(self.selected_ftp_profiles or self.legacy_ftp_selected)
        job["ftp_profile"] = (job["ftp_targets"][0] if job["ftp_targets"] else "")
        job["force_terminate_override"] = {"Да": True, "Нет": False}.get(source_value(self.v["force_terminate_override"].get()))
        if job["wait_seconds_override"]:
            job["wait_seconds_override"] = str(max(0, int(job["wait_seconds_override"])))
        job["check_mode"] = self.v["check_mode"].get()
        job["repair_bad_refs"] = self.v["repair_bad_refs"].get()
        job["repair_bad_data"] = self.v["repair_bad_data"].get()
        job["connection_kind"] = self.v["connection_kind"].get()
        if job.get("type") == "1c" and job["connection_kind"] == "file":
            automatically = bool(self.v["file_auto_disconnect"].get())
            job["file_force_confirmed"] = automatically
            job["file_disconnect_mode"] = (
                "force_hosts" if str(job.get("file_session_hosts") or "").strip()
                else "force_local"
            ) if automatically else "wait_only"
            from file_session_control import validate_file_disconnect_settings
            validate_file_disconnect_settings(job)
            if job.get("file_web_maintenance_enabled") and not automatically:
                raise ValueError("Для остановки IIS/Apache включите автоматическое "
                                 "завершение пользователей")
            # The pre-5.4 IIS-only permission must never trigger a legacy
            # path after the user saves the new unified web consent.
            job["file_iis_maintenance_enabled"] = False
        job["platform_mode"] = next((key for key, label in PLATFORM_DISPLAY.items() if label == source_value(self.platform_display.get())), "inherit")
        if self.job.get("type") in {"files", "extra_copy"}:
            job["file_paths"] = self.filebox.get("1.0", "end-1c").strip()
        if self.job.get("type") == "commands":
            job["command_body"] = self.commandbox.get("1.0", "end-1c").strip()
        elif self.job.get("type") not in {"report"}:
            job["command_before"] = self.cmd_before.get("1.0", "end-1c").strip()
            job["command_after"] = self.cmd_after.get("1.0", "end-1c").strip()
        job["stop_after_hours"] = int(self.v["stop_after_hours"].get() or 0)
        job["stop_after_minutes"] = int(self.v["stop_after_minutes"].get() or 0)
        h, m = self.hour.get().strip(), self.minute.get().strip()
        if bool(h) != bool(m):
            raise ValueError("Заполните часы и минуты либо очистите оба поля.")
        job["schedule_time"] = self.backend.normalize_time_hhmm(f"{h}:{m}") if h else ""
        if not job.get("name"):
            raise ValueError("Введите наименование задачи.")
        if not job.get("ref"):
            raise ValueError("Введите идентификатор / Ref.")
        if job.get("type") in {"1c", "1c_check", "1c_procedure"}:
            if job.get("connection_kind") == "server":
                if not (job.get("one_c_server") and job.get("ib_ref")):
                    raise ValueError("Для серверной базы укажите сервер и имя базы.")
            elif job.get("connection_kind") == "file":
                if not job.get("one_c_file"):
                    raise ValueError("Укажите каталог файловой базы.")
            else:
                job["enabled"] = False
            if job.get("type") == "1c_check":
                from onec_tasks_8 import build_check_options
                build_check_options(job)
            elif job.get("type") == "1c_procedure":
                if not job.get("procedure_epf") or not job["procedure_epf"].lower().endswith(".epf"):
                    raise ValueError("Выберите обработку .epf для вызова процедуры 1С")
                if int(job.get("procedure_timeout_hours") or 0) < 1:
                    raise ValueError("Таймаут выполнения должен быть не меньше 1 часа")
        elif job.get("type") == "mssql":
            if not job.get("sql_server") or not job.get("sql_database"):
                raise ValueError("Для MS SQL укажите сервер и базу.")
        elif job.get("type") == "commands":
            if not job.get("command_body"):
                raise ValueError("Добавьте команды для выполнения.")
        elif job.get("type") == "report":
            if not (job.get("notify_email") or job.get("notify_telegram") or job.get("notify_max")):
                raise ValueError("Для отчёта выберите SMTP, Telegram и/или MAX.")
        else:
            if not job.get("file_paths"):
                raise ValueError("Добавьте хотя бы один файл или каталог.")
        return self.backend.normalize_db(job)

    def save(self):
        try:
            job = self.extract()
            self.on_save(job, self.v["ib_password"].get(), self.v["sql_password"].get(),
                         self.v["cluster_job_password"].get())
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Задача", str(exc), parent=self)


class SystemSettingsDialog(tk.Toplevel):
    """Compact Effector-style system options with a real SMTP/Telegram section."""
    PAGES = ["Автозапуск", "Параметры агента", "Параметры менеджера",
             "Файлы архивов", "Журнал", "Служебные", "Отчеты", "Язык программы"]
    CLOSE_ACTIONS = {"ask": "Спрашивать при закрытии", "tray": "Сворачивать в трей",
                     "exit": "Закрывать интерфейс"}

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.title("Параметры программы")
        self.geometry("1000x690")
        self.minsize(850, 570)
        self.transient(owner)
        self.grab_set()
        cfg, sec = owner.cfg, owner.sec
        self.v = {}
        for key in ("wait_seconds", "service_poll_seconds", "backup_root"):
            self.v[key] = tk.StringVar(value=str(cfg.get(key, "")))
        self.language_var = tk.StringVar(value="English" if cfg.get("ui_language") == "en" else "Русский")
        self.force = tk.BooleanVar(value=bool(cfg.get("force_terminate", True)))
        self.retention_days = tk.StringVar(value=str(cfg.get("journal_retention_days", 60)))
        self.close_display = tk.StringVar(value=self.CLOSE_ACTIONS.get(cfg.get("ui_close_action", "ask"), self.CLOSE_ACTIONS["ask"]))
        self.minimize_to_tray = tk.BooleanVar(value=bool(cfg.get("ui_minimize_to_tray", True)))
        notices = cfg.get("notifications") or {}
        smtp = notices.get("smtp") or {}
        telegram = notices.get("telegram") or {}
        max_settings = notices.get("max") or {}
        self.smtp = {k: tk.StringVar(value=str(smtp.get(k, defaults))) for k, defaults in
                     (("host", ""), ("port", 587), ("security", "STARTTLS"), ("user", ""), ("from", ""), ("to", ""))}
        self.smtp_password = tk.StringVar(value=sec.get("smtp_password", ""))
        self.chat_id = tk.StringVar(value=str(telegram.get("chat_id", "")))
        self.telegram_token = tk.StringVar(value=sec.get("telegram_bot_token", ""))
        self.max_token = tk.StringVar(value=sec.get("max_bot_token", ""))
        self.max_recipient_id = tk.StringVar(value=str(max_settings.get("recipient_id", "")))
        self.max_recipient_type = tk.StringVar(value=str(max_settings.get("recipient_type", "chat_id")))
        self._build()
        self.after_idle(lambda: translate_widgets(self, self.owner.cfg.get("ui_language", "ru")))

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        self.nav = tk.Listbox(body, width=27, exportselection=False, font=("Segoe UI", 10))
        self.nav.pack(side="left", fill="y", padx=(0, 12))
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        self.pages = []
        for name in self.PAGES:
            self.nav.insert("end", name)
            page = ScrollablePage(right)
            page.place(x=0, y=0, relwidth=1, relheight=1)
            self.pages.append(page)
        contents = [p.content for p in self.pages]
        self._autostart(contents[0])
        self._agent(contents[1])
        self._manager(contents[2])
        self._archive_files(contents[3])
        self._journal(contents[4])
        self._service(contents[5])
        self._reports(contents[6])
        self._language(contents[7])
        self.nav.selection_set(0)
        self.pages[0].lift()
        self.nav.bind("<<ListboxSelect>>", self._switch)
        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="Сохранить", command=self.save).pack(side="right")
        ttk.Button(footer, text="Отмена", command=self.destroy).pack(side="right", padx=8)

    def _switch(self, _event=None):
        selection = self.nav.curselection()
        if selection:
            self.pages[selection[0]].lift()

    def _autostart(self, page):
        section_bar(page, "Автозапуск")
        ttk.Label(page, text="Запускать как службу Windows (сервер) — рекомендуемый вариант для ночных заданий.",
                  wraplength=580).pack(anchor="w", pady=7)
        for title, cmd in (("Установить / обновить службу", "install"), ("Запустить службу", "start"),
                           ("Остановить службу", "stop")):
            ttk.Button(page, text=title, command=lambda c=cmd: self.owner.service_command(c)).pack(anchor="w", pady=4)
        ttk.Label(page, textvariable=self.owner.agent_status).pack(anchor="w", pady=(18,0))
        ttk.Label(page, text="Закрытие окна не останавливает Windows-службу.", foreground="#52677b").pack(anchor="w", pady=8)

    def _agent(self, page):
        section_bar(page, "Параметры агента")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Опрос расписания, сек.", self.v["service_poll_seconds"], 0)
        add_line(frm, "Ожидание выхода пользователей 1С, сек.", self.v["wait_seconds"], 1)
        ttk.Checkbutton(frm, text="Принудительно завершать оставшиеся сеансы 1С",
                        variable=self.force).grid(row=2, column=0, columnspan=2, sticky="w", pady=9)
        ttk.Label(page, text="Для безопасности все задания выполняются последовательно, без параллельного DumpIB.",
                  wraplength=570, foreground="#52677b").pack(anchor="w", pady=8)

    def _manager(self, page):
        section_bar(page, "Параметры менеджера")
        ttk.Label(page, text="Действие при закрытии окна:").pack(anchor="w", pady=6)
        ttk.Combobox(page, textvariable=self.close_display, state="readonly",
                     values=list(self.CLOSE_ACTIONS.values()), width=38).pack(anchor="w")
        ttk.Checkbutton(page, text="Сворачивать в трей при нажатии кнопки «Свернуть»",
                        variable=self.minimize_to_tray).pack(anchor="w", pady=(10, 0))
        ttk.Label(page, text="Для сворачивания в трей требуются pystray и Pillow (сборщик EXE устанавливает их автоматически).",
                  wraplength=570, foreground="#52677b").pack(anchor="w", pady=14)

    def _archive_files(self, page):
        section_bar(page, "Файлы архивов")
        frm = ttk.Frame(page)
        frm.pack(fill="x", pady=5)
        ttk.Label(frm, text="Корневая папка копий:").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Entry(frm, textvariable=self.v["backup_root"], width=48).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="...", width=4, command=self._browse_backup_root).grid(row=0, column=2, padx=(6, 0), pady=4)
        frm.columnconfigure(1, weight=1)
        ttk.Label(page,
                  text="Используется для служебной блокировки, журналов и заданий без собственной папки. "
                       "Если у базы указана отдельная папка (например C:\\BCP), её архивы сохраняются туда.",
                  wraplength=600, foreground="#52677b", justify="left").pack(anchor="w", pady=(10, 5))
        ttk.Label(page,
                  text="Выбранный диск должен существовать и быть доступен учётной записи службы Windows.",
                  wraplength=600, foreground="#a94b34", justify="left").pack(anchor="w", pady=5)

    def _browse_backup_root(self):
        start = self.v["backup_root"].get().strip()
        initial = start if start and Path(start).is_dir() else None
        selected = filedialog.askdirectory(parent=self, title="Корневая папка копий", initialdir=initial)
        if selected:
            self.v["backup_root"].set(os.path.normpath(selected))

    @staticmethod
    def validate_backup_root(value):
        """Validate the common storage root before saving settings."""
        value = str(value or "").strip()
        if not value:
            raise ValueError("Укажите корневую папку копий в разделе «Файлы архивов».")
        target = Path(value)
        if os.name == "nt":
            if not target.is_absolute():
                raise ValueError("Укажите полный путь к папке копий, например C:\\BCP.")
            drive = target.drive
            if drive and len(drive) == 2 and drive[1] == ":" and not Path(drive + "\\").exists():
                raise ValueError(f"Диск {drive} недоступен. Выберите существующий диск в разделе «Файлы архивов».")
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"Не удаётся создать или открыть корневую папку копий: {target}. "
                                 "Проверьте диск и права пользователя Windows-службы.") from exc
            if not target.is_dir():
                raise ValueError(f"Путь не является каталогом: {target}")
        return value

    def _journal(self, page):
        section_bar(page, "Журнал")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Автоматически удалять записи журнала старше, дней", self.retention_days, 0)
        ttk.Label(page, text="0 — не удалять. Очистка касается только системных журналов в папке logs, не архивов .dt/.bak/.zip/.7z.",
                  foreground="#a94b34", wraplength=580).pack(anchor="w", pady=12)

    def _service(self, page):
        section_bar(page, "Служебные")
        ttk.Button(page, text="Открыть папку логов в проводнике", command=self._open_logs).pack(anchor="w", pady=8)
        ttk.Button(page, text="Аварийно снять блокировки 1С", command=self.owner.unblock_all).pack(anchor="w", pady=8)

    def _reports(self, page):
        section_bar(page, "Отчеты — почта, Telegram и MAX")
        mail = ttk.LabelFrame(page, text="Электронная почта (SMTP)", padding=9)
        mail.pack(fill="x")
        frm = ttk.Frame(mail)
        frm.pack(fill="x")
        for i, (key, title) in enumerate((("host", "SMTP сервер"), ("port", "Порт"),
                                          ("user", "Логин SMTP"), ("from", "Отправитель"),
                                          ("to", "Получатель (через запятую)"))):
            add_line(frm, title, self.smtp[key], i)
        add_line(frm, "Пароль SMTP", self.smtp_password, 5, password=True)
        ttk.Label(frm, text="Защита соединения").grid(row=6, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.smtp["security"], values=["STARTTLS", "SSL/TLS"],
                     state="readonly", width=16).grid(row=6, column=1, sticky="w", pady=5)
        tg = ttk.LabelFrame(page, text="Telegram Bot API", padding=9)
        tg.pack(fill="x", pady=(12,0))
        tgf = ttk.Frame(tg)
        tgf.pack(fill="x")
        add_line(tgf, "Bot Token", self.telegram_token, 0, password=True)
        add_line(tgf, "Chat ID", self.chat_id, 1)
        ttk.Label(tg, text="Контакт @dek76 — не Chat ID. Для отправки нужен бот, созданный через BotFather, и ID чата.",
                  foreground="#52677b", wraplength=580).pack(anchor="w", pady=5)
        mx = ttk.LabelFrame(page, text="MAX Bot API", padding=9)
        mx.pack(fill="x", pady=(12, 0))
        mxf = ttk.Frame(mx)
        mxf.pack(fill="x")
        add_line(mxf, "Bot Token MAX", self.max_token, 0, password=True)
        ttk.Label(mxf, text="Тип ID:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(mxf, textvariable=self.max_recipient_type,
                     values=["chat_id", "user_id"], state="readonly", width=18).grid(row=1, column=1, sticky="w", pady=5)
        add_line(mxf, "ID получателя MAX", self.max_recipient_id, 2)
        ttk.Label(mx, text="chat_id — чат/канал, user_id — личное сообщение. ID — числовой. Бот должен иметь доступ к получателю.",
                  foreground="#52677b", wraplength=550).pack(anchor="w", pady=5)
        tests = ttk.Frame(page)
        tests.pack(fill="x", pady=10)
        ttk.Button(tests, text="Тест письма", command=lambda: self._test_delivery(True, False)).pack(side="left")
        ttk.Button(tests, text="Тест Telegram", command=lambda: self._test_delivery(False, True)).pack(side="left", padx=8)
        ttk.Button(tests, text="Тест MAX", command=lambda: self._test_delivery(False, False, True)).pack(side="left", padx=8)

    def _language(self, page):
        section_bar(page, "Язык программы")
        ttk.Label(page, text="Выберите язык интерфейса:").pack(anchor="w", pady=8)
        ttk.Combobox(page, textvariable=self.language_var, values=["Русский", "English"],
                     state="readonly", width=24).pack(anchor="w", pady=5)
        ttk.Label(page, text="Новый язык применяется после повторного открытия программы.",
                  foreground="#52677b", wraplength=570).pack(anchor="w", pady=7)

    def _choose_dir(self, key):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.v[key].set(path)

    def _open_logs(self):
        logdir = Path(self.owner.cfg.get("backup_root", "")) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(logdir))
        else:
            messagebox.showinfo("Журнал", str(logdir), parent=self)

    def _settings_snapshot(self):
        cfg = deepcopy(self.owner.cfg)
        sec = deepcopy(self.owner.sec)
        for key, var in self.v.items():
            if key != "cluster_password":
                cfg[key] = var.get().strip()
        cfg["wait_seconds"] = max(0, int(self.v["wait_seconds"].get() or 0))
        cfg["service_poll_seconds"] = max(10, int(self.v["service_poll_seconds"].get() or 30))
        cfg["force_terminate"] = bool(self.force.get())
        cfg["journal_retention_days"] = max(0, int(self.retention_days.get() or 0))
        cfg["ui_close_action"] = next((key for key, value in self.CLOSE_ACTIONS.items()
                                       if value == source_value(self.close_display.get())), "ask")
        cfg["ui_minimize_to_tray"] = bool(self.minimize_to_tray.get())
        cfg["notifications"] = {"smtp": {k: v.get().strip() for k, v in self.smtp.items()},
                                "telegram": {"chat_id": self.chat_id.get().strip()},
                                "max": {"recipient_type": self.max_recipient_type.get().strip(),
                                        "recipient_id": self.max_recipient_id.get().strip()}}
        cfg["notifications"]["smtp"]["port"] = int(self.smtp["port"].get() or 587)
        sec["smtp_password"] = self.smtp_password.get()
        sec["telegram_bot_token"] = self.telegram_token.get()
        sec["max_bot_token"] = self.max_token.get()
        cfg["ui_language"] = "en" if self.language_var.get() in {"English", "Английский"} else "ru"
        return cfg, sec

    def _test_delivery(self, email, telegram, max_enabled=False):
        from report_delivery import send_channels
        try:
            cfg, sec = self._settings_snapshot()
            send_channels(cfg, sec, '1C Backup Manager — тест', 'Проверка отправки отчётов.',
                          email=email, telegram=telegram, max_enabled=max_enabled, log=lambda s: None, strict=True)
            messagebox.showinfo("Отчеты", "Тестовое сообщение отправлено.", parent=self)
        except Exception as exc:
            messagebox.showerror("Отчеты", str(exc), parent=self)

    def save(self):
        try:
            cfg, sec = self._settings_snapshot()
            cfg["backup_root"] = self.validate_backup_root(cfg.get("backup_root"))
            changed_language = cfg.get("ui_language", "ru") != self.owner.cfg.get("ui_language", "ru")
            self.owner.persist(cfg, sec)
            self.destroy()
            if changed_language:
                messagebox.showinfo("Language / Язык", "Язык интерфейса применится при следующем запуске программы.\nThe language will change after restarting the application.", parent=self.owner)
        except Exception as exc:
            messagebox.showerror("Параметры", str(exc), parent=self)

def create_app_class(backend):
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.backend = backend
            self.title(f"1C Backup Manager v{backend.APP_VERSION} — резервное копирование")
            self.geometry("920x740")
            self.minsize(750, 590)
            self.style()
            self.cfg = backend.load_config()
            self.cfg.setdefault("ftp_profiles", [])
            self.cfg.setdefault("task_folders", [])
            self.secrets_error = None
            try:
                self.sec = backend.load_secrets()
            except Exception:
                self.secrets_error = traceback.format_exc()
                self.sec = {"cluster_password": "", "db_passwords": {}, "sql_passwords": {}, "ftp_password": "", "ftp_profile_passwords": {}, "cluster_job_passwords": {}}
            self.jobs = [backend.normalize_db(j) for j in self.cfg.get("databases", [])]
            self.worker = None
            self.q = queue.Queue()
            self.summary = tk.StringVar()
            self.service_status = tk.StringVar(value="Служба: Проверка...")
            self.agent_status = tk.StringVar(value="Агент: проверка...")
            self.tray_controller = None
            self.tray_events = queue.Queue()
            self._build()
            translate_widgets(self, self.cfg.get("ui_language", "ru"))
            self.refresh_jobs()
            self.refresh_journal()
            try:
                prune_task_runs(self.cfg.get("journal_retention_days", 60))
            except OSError:
                pass
            self.protocol("WM_DELETE_WINDOW", self.on_close_request)
            self.bind("<Unmap>", self._on_unmap)
            self.after(200, self.poll)
            self.after(250, self.poll_tray)
            self.after(5000, self.periodic)

        def style(self):
            st = ttk.Style(self)
            if "vista" in st.theme_names():
                st.theme_use("vista")
            st.configure("Classic.Treeview", rowheight=22, font=("Segoe UI", 9))
            st.configure("Classic.Treeview.Heading", font=("Segoe UI", 9, "bold"))
            st.configure("DialogHeader.TLabel", font=("Segoe UI", 13, "bold"))

        def _build(self):
            menus = tk.Menu(self)
            svc = tk.Menu(menus, tearoff=0)
            svc.add_command(label="Параметры программы...", command=self.open_settings)
            svc.add_command(label="Управление хранилищами...", command=self.open_profiles)
            svc.add_command(label="Импорт баз 1С...", command=self.import_bases)
            svc.add_separator()
            svc.add_command(label="Установить/обновить службу", command=lambda: self.service_command("install"))
            svc.add_command(label="Запустить службу", command=lambda: self.service_command("start"))
            svc.add_command(label="Остановить службу", command=lambda: self.service_command("stop"))
            svc.add_separator()
            svc.add_command(label="Очистить список заданий...", command=self.clear_jobs)
            svc.add_separator()
            svc.add_command(label="Свернуть в трей", command=self.hide_to_tray)
            svc.add_command(label="Выход", command=self.on_close_request)
            menus.add_cascade(label="Сервис", menu=svc)
            jobs = tk.Menu(menus, tearoff=0)
            jobs.add_command(label="Создать...", command=self.new_job_menu)
            jobs.add_command(label="Создать папку...", command=self.create_folder)
            jobs.add_command(label="Переименовать папку...", command=self.rename_folder)
            jobs.add_command(label="Удалить папку...", command=self.delete_folder)
            jobs.add_command(label="Переместить в папку...", command=self.move_to_folder)
            jobs.add_command(label="Изменить", command=self.edit_job)
            jobs.add_command(label="Копировать", command=self.clone_job)
            jobs.add_command(label="Удалить", command=self.delete_job)
            jobs.add_separator()
            jobs.add_command(label="Выполнить", command=self.run_selected)
            jobs.add_command(label="Все активные", command=self.run_all)
            jobs.add_command(label="Проверить", command=self.test_selected)
            jobs.add_command(label="Повторить отправку на FTP", command=self.retry_ftp)
            menus.add_cascade(label="Задачи", menu=jobs)
            logm = tk.Menu(menus, tearoff=0)
            logm.add_command(label="Обновить журнал", command=self.refresh_journal)
            menus.add_cascade(label="Журнал задач", menu=logm)
            helpm = tk.Menu(menus, tearoff=0)
            helpm.add_command(label="Telegram @dek76", command=lambda: webbrowser.open("https://t.me/dek76"))
            menus.add_cascade(label="Помощь", menu=helpm)
            self.config(menu=menus)

            top = ttk.Frame(self, padding=(7, 5))
            top.pack(fill="both", expand=True)
            main_tabs = ttk.Notebook(top)
            main_tabs.pack(fill="both", expand=True)
            page_tasks = ttk.Frame(main_tabs, padding=4)
            page_active = ttk.Frame(main_tabs, padding=4)
            main_tabs.add(page_tasks, text="Настройка задач")
            main_tabs.add(page_active, text="Активные задачи")

            upper = ttk.Frame(page_tasks)
            upper.pack(fill="both", expand=True)
            self.tree = ttk.Treeview(upper, columns=("kind",), show="tree headings", style="Classic.Treeview")
            self.tree.heading("#0", text="Наименование")
            self.tree.heading("kind", text="Вид задачи")
            self.tree.column("#0", width=285, stretch=True)
            self.tree.column("kind", width=430, stretch=True)
            self.tree.pack(side="left", fill="both", expand=True)
            s = ttk.Scrollbar(upper, orient="vertical", command=self.tree.yview)
            s.pack(side="right", fill="y")
            self.tree.configure(yscrollcommand=s.set)
            self.tree.bind("<Double-1>", lambda e: self.edit_job())
            self.tree.bind("<Button-3>", self.show_tree_menu)
            tools = ttk.Frame(page_tasks)
            tools.pack(fill="x", pady=(6, 0))
            ttk.Button(tools, text="+ Задача", command=self.new_job_menu).pack(side="left", padx=2)
            ttk.Button(tools, text="+ Папка", command=self.create_folder).pack(side="left", padx=2)
            ttk.Button(tools, text="Изменить", command=self.edit_job).pack(side="left", padx=2)
            ttk.Button(tools, text="Удалить", command=self.delete_job).pack(side="left", padx=2)
            ttk.Button(tools, text="В папку...", command=self.move_to_folder).pack(side="left", padx=2)
            ttk.Button(tools, text="Выполнить", command=self.run_selected).pack(side="left", padx=12)
            ttk.Button(tools, text="Проверить", command=self.test_selected).pack(side="left", padx=2)
            ttk.Button(tools, text="Хранилища", command=self.open_profiles).pack(side="right", padx=2)

            self.active_tree = ttk.Treeview(
                page_active, columns=("job", "next", "last", "status"),
                show="headings", style="Classic.Treeview")
            for col, title, width in (
                ("job", "Имя задачи", 270),
                ("next", "Время следующего запуска", 205),
                ("last", "Время прошлого запуска", 205),
                ("status", "Состояние", 190)):
                self.active_tree.heading(col, text=title)
                self.active_tree.column(col, width=width, stretch=(col == "job"))
            self.active_tree.pack(side="left", fill="both", expand=True)
            active_scroll = ttk.Scrollbar(page_active, orient="vertical", command=self.active_tree.yview)
            active_scroll.pack(side="right", fill="y")
            self.active_tree.configure(yscrollcommand=active_scroll.set)

            lower = ttk.Notebook(top)
            lower.pack(fill="both", expand=True, pady=(5, 0))
            journal_page = ttk.Frame(lower, padding=4)
            backups_page = ttk.Frame(lower, padding=4)
            raw_page = ttk.Frame(lower, padding=4)
            lower.add(journal_page, text="Журнал задач")
            lower.add(backups_page, text="Бэкапы")
            lower.add(raw_page, text="Тех. лог")

            self.journal = ttk.Treeview(journal_page, columns=("start", "end", "result"), show="headings", height=8)
            for col, title, width in [("start", "Начало", 160), ("end", "Конец", 160), ("result", "Результат", 780)]:
                self.journal.heading(col, text=title)
                self.journal.column(col, width=width, stretch=col == "result")
            self.journal.pack(fill="both", expand=True)
            self.journal.bind("<Double-1>", self.open_journal_details)
            self.journal_records = {}
            self.archives = ttk.Treeview(backups_page, columns=("name", "size", "modified"), show="headings")
            for col, title, width in [("name", "Файл", 600), ("size", "Размер, МБ", 110), ("modified", "Дата", 180)]:
                self.archives.heading(col, text=title)
                self.archives.column(col, width=width, stretch=col == "name")
            self.archives.pack(fill="both", expand=True)
            self.rawlog = tk.Text(raw_page, font=("Consolas", 9), state="disabled")
            self.rawlog.pack(fill="both", expand=True)

            # Reserve the bottom status bar *before* the expanding top pane.
            # Otherwise Tk may give all available height to the notebooks and
            # the bar is pushed off-screen at typical Windows display scaling.
            status = ttk.Frame(self, padding=(10, 5), relief="groove")
            status.pack(side="bottom", fill="x", before=top)
            ttk.Label(status, textvariable=self.summary).pack(side="left")
            self.agent_status_label = ttk.Label(status, textvariable=self.agent_status, foreground="#52677b")
            self.agent_status_label.pack(side="left", padx=20)
            ttk.Label(status, textvariable=self.service_status).pack(side="left", padx=12)
            ttk.Label(status, text="Telegram: @dek76", foreground="#2367a0", cursor="hand2").pack(side="right")

        def _index(self):
            sel = self.tree.selection()
            if not sel or not sel[0].startswith("job_"):
                return None
            return int(sel[0][4:])

        def refresh_jobs(self):
            current = self.tree.selection()[0] if self.tree.selection() else None
            self.tree.delete(*self.tree.get_children())
            root = self.tree.insert("", "end", iid="root", text="\\", open=True, values=("",))
            folders = set(self.cfg.get("task_folders", []))
            folders.update(str(j.get("task_group", "")).strip("/") for j in self.jobs if j.get("task_group"))
            allfolders = set()
            for folder in folders:
                parts = [v for v in str(folder).split("/") if v]
                for idx in range(1, len(parts)+1):
                    allfolders.add("/".join(parts[:idx]))
            for folder in sorted(allfolders, key=lambda x: (x.count("/"), x.casefold())):
                parent_path = folder.rsplit("/", 1)[0] if "/" in folder else ""
                parent_id = f"folder::{parent_path}" if parent_path else "root"
                self.tree.insert(parent_id, "end", iid=f"folder::{folder}", text="📁 " + folder.split("/")[-1], open=True, values=("",))
            for idx, job in enumerate(self.jobs):
                kind = {"1c": "Резервное копирование 1С:Предприятие 8 / BAS",
                        "mssql": "Резервное копирование Microsoft SQL Server",
                        "files": "Резервное копирование файлов и баз данных",
                        "extra_copy": "Дополнительное копирование",
                        "commands": "Произвольные команды",
                        "report": "Отправка отчетов",
                        "1c_check": "Тестирование и исправление 1С:Предприятие 8 / BAS",
                        "1c_procedure": "Вызов процедуры 1С:Предприятие 8 / BAS"}.get(job.get("type"), "Задание")
                group = str(job.get("task_group", "")).strip("/")
                parent = f"folder::{group}" if group in allfolders else "root"
                icon = "◉" if job.get("enabled") else "○"
                self.tree.insert(parent, "end", iid=f"job_{idx}",
                                 text=f"{icon} {job.get('name') or job.get('ref')}",
                                 values=(tr(self.cfg.get("ui_language", "ru"), kind),))
            if current and self.tree.exists(current):
                self.tree.selection_set(current)
            enabled = sum(1 for x in self.jobs if x.get("enabled"))
            self.summary.set((f"Tasks: {len(self.jobs)}  •  Enabled: {enabled}" if self.cfg.get("ui_language") == "en"
                              else f"Всего заданий: {len(self.jobs)}  •  Включено: {enabled}"))
            self.refresh_archives()
            self.refresh_active_tasks()
            self.refresh_service_status()

        def selected_folder(self):
            sel = self.tree.selection()
            iid = sel[0] if sel else ""
            if iid.startswith("folder::"):
                return iid[8:]
            if iid.startswith("job_"):
                return str(self.jobs[int(iid[4:])].get("task_group", "")).strip("/")
            return ""

        def create_folder(self):
            base = self.selected_folder()
            name = simpledialog.askstring("Папка задач", "Название новой папки:", parent=self)
            if not name:
                return
            name = name.strip()
            if name in {".", ".."} or any(x in name for x in "/\\:"):
                messagebox.showerror("Папка задач", "Недопустимое имя папки", parent=self)
                return
            new_path = base + "/" + name if base else name
            folders = self.cfg.setdefault("task_folders", [])
            if new_path in folders:
                messagebox.showwarning("Папка задач", "Такая папка уже существует", parent=self)
                return
            folders.append(new_path)
            self.persist()
            self.tree.selection_set("folder::" + new_path)

        def rename_folder(self):
            sel = self.tree.selection()
            if not sel or not sel[0].startswith("folder::"):
                messagebox.showinfo("Папка задач", "Выберите папку в дереве", parent=self)
                return
            old = sel[0][8:]
            new_name = simpledialog.askstring("Переименовать папку", "Новое название:",
                                               initialvalue=old.split("/")[-1], parent=self)
            if not new_name:
                return
            new_name = new_name.strip()
            if new_name in {".", ".."} or any(x in new_name for x in "/\\:"):
                messagebox.showerror("Папка задач", "Недопустимое имя папки", parent=self)
                return
            parent = old.rsplit("/", 1)[0] if "/" in old else ""
            new_path = (parent + "/" if parent else "") + new_name
            if new_path != old and new_path in self.cfg.setdefault("task_folders", []):
                raise ValueError("Такая папка уже существует")
            self.cfg["task_folders"] = [new_path + x[len(old):] if x == old or x.startswith(old + "/") else x
                                        for x in self.cfg["task_folders"]]
            for job in self.jobs:
                g = str(job.get("task_group", ""))
                if g == old or g.startswith(old + "/"):
                    job["task_group"] = new_path + g[len(old):]
            self.persist()

        def delete_folder(self):
            sel = self.tree.selection()
            if not sel or not sel[0].startswith("folder::"):
                return
            folder = sel[0][8:]
            if not messagebox.askyesno("Папка задач", "Удалить папку из списка? Задания будут перемещены в корень.\nАрхивы не удаляются.", parent=self):
                return
            self.cfg["task_folders"] = [x for x in self.cfg.get("task_folders", []) if x != folder and not x.startswith(folder + "/")]
            for job in self.jobs:
                if str(job.get("task_group", "")) == folder or str(job.get("task_group", "")).startswith(folder + "/"):
                    job["task_group"] = ""
            self.persist()

        def move_to_folder(self):
            idx = self._index()
            if idx is None:
                return
            folders = sorted(set(self.cfg.get("task_folders", [])))
            win = tk.Toplevel(self)
            win.title("Переместить задание в папку")
            win.geometry("410x155")
            win.transient(self)
            win.grab_set()
            frame = ttk.Frame(win, padding=12)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="Папка назначения:").pack(anchor="w")
            target = tk.StringVar(value=self.jobs[idx].get("task_group") or "\\")
            ttk.Combobox(frame, textvariable=target, state="readonly", values=["\\"] + folders).pack(fill="x", pady=8)
            def apply():
                self.jobs[idx]["task_group"] = "" if target.get() == "\\" else target.get()
                self.persist()
                win.destroy()
            ttk.Button(frame, text="Переместить", command=apply).pack(side="right")
            ttk.Button(frame, text="Отмена", command=win.destroy).pack(side="right", padx=8)

        def show_tree_menu(self, event):
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
            menu = tk.Menu(self, tearoff=0)
            if iid and iid.startswith("folder::"):
                menu.add_command(label="Новая папка", command=self.create_folder)
                menu.add_command(label="Переименовать папку", command=self.rename_folder)
                menu.add_command(label="Удалить папку", command=self.delete_folder)
            else:
                menu.add_command(label="Новое задание...", command=self.new_job_menu)
                menu.add_command(label="Редактировать", command=self.edit_job)
                menu.add_command(label="Переместить в папку...", command=self.move_to_folder)
                menu.add_separator()
                menu.add_command(label="Выполнить", command=self.run_selected)
            menu.tk_popup(event.x_root, event.y_root)

        def refresh_archives(self):
            self.archives.delete(*self.archives.get_children())
            root = Path(self.cfg.get("backup_root", ""))
            if not root.exists():
                return
            files = sorted((x for x in root.glob("**/*") if x.is_file() and x.suffix.lower() in {".dt", ".bak", ".zip", ".7z"}), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
            for p in files:
                self.archives.insert("", "end", values=(str(p.relative_to(root)), f"{p.stat().st_size/1048576:.2f}", datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S")))

        def refresh_journal(self):
            current = self.journal.selection()[0] if self.journal.selection() else None
            self.journal.delete(*self.journal.get_children())
            history = list_task_runs(limit=500)
            self.journal_records = {r["run_id"]: r for r in history}
            for record in history:
                status = record.get("status")
                display = ("Успешное выполнение" if status == "ok" else
                           "Ошибка выполнения" if status == "error" else
                           "Выполняется" if status == "running" else "Прервано")
                message = record.get("message") or ""
                result = f"{display} задачи: {record.get('job_name', 'Задание')}"
                if status == "error" and message:
                    result += f" — {message[:170]}"
                self.journal.insert("", "end", iid=record["run_id"],
                                    values=(record.get("start", ""), record.get("end", ""), result))
            # Existing installations retain their previous last-run summary until
            # a new 5.1 attempt creates a full per-run journal.
            existing = {r.get("job_key") for r in history}
            state = self.backend.load_scheduler_state()
            for i, job in enumerate(self.jobs):
                if self.backend.job_key(job) in existing:
                    continue
                item = self.backend.get_db_state(job, state)
                if not item.get("last_attempt_time"):
                    continue
                rid = f"legacy_{i}"
                self.journal_records[rid] = {
                    "run_id": rid, "job_name": job.get("name"),
                    "start": item.get("last_attempt_time"), "end": "",
                    "status": item.get("last_result"),
                    "message": item.get("last_error") or "",
                }
                status_text = "Ошибка" if item.get("last_result") == "error" else "Успех"
                self.journal.insert("", "end", iid=rid,
                                    values=(item.get("last_attempt_time"), "",
                                            f"{status_text}: {job.get('name')} (ранний журнал)"))
            if current and self.journal.exists(current):
                self.journal.selection_set(current)

        def refresh_active_tasks(self):
            now = datetime.now()
            current = self.active_tree.selection()[0] if self.active_tree.selection() else None
            self.active_tree.delete(*self.active_tree.get_children())
            state = self.backend.load_scheduler_state()
            latest = {}
            for record in list_task_runs(limit=500):
                key = record.get("job_key")
                if key and key not in latest:
                    latest[key] = record
            for idx, job in enumerate(self.jobs):
                item = self.backend.get_db_state(job, state)
                if not job.get("enabled", True):
                    continue
                next_time = next_scheduled_start(
                    job.get("schedule_time"), item.get("last_scheduled_date", ""), now)
                next_label = next_time.strftime("%d.%m.%Y %H:%M:%S") if next_time else "Без расписания"
                history = latest.get(self.backend.job_key(job), {})
                previous = history.get("start") or item.get("last_attempt_time") or "—"
                status = history.get("status") or item.get("last_result") or ""
                status_label = {
                    "running": "Выполняется", "ok": "Успешно", "error": "Ошибка",
                    "interrupted": "Прервано",
                }.get(status, "Ожидание")
                self.active_tree.insert("", "end", iid=f"active_{idx}",
                                        values=(job.get("name") or job.get("ref"),
                                                next_label, previous, status_label))
            if current and self.active_tree.exists(current):
                self.active_tree.selection_set(current)

        def open_journal_details(self, event=None):
            selected = self.journal.selection()
            if not selected:
                return
            record = self.journal_records.get(selected[0])
            if not record:
                return
            win = tk.Toplevel(self)
            win.title(f"Журнал задачи: {record.get('job_name', 'Задание')}")
            win.geometry("940x640")
            win.minsize(640, 400)
            win.transient(self)
            head = ttk.Frame(win, padding=12)
            head.pack(fill="x")
            ttk.Label(head, text=f"Задача: {record.get('job_name', '')}",
                      font=("Segoe UI", 11, "bold")).pack(anchor="w")
            ttk.Label(head, text=f"Начало: {record.get('start') or '—'}    "
                                 f"Конец: {record.get('end') or '—'}    "
                                 f"Результат: {record.get('status') or '—'}").pack(anchor="w", pady=5)
            body = ttk.Frame(win, padding=(12, 0, 12, 8))
            body.pack(fill="both", expand=True)
            viewer = tk.Text(body, font=("Consolas", 9), wrap="none")
            scroll_y = ttk.Scrollbar(body, orient="vertical", command=viewer.yview)
            scroll_x = ttk.Scrollbar(body, orient="horizontal", command=viewer.xview)
            viewer.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
            viewer.grid(row=0, column=0, sticky="nsew")
            scroll_y.grid(row=0, column=1, sticky="ns")
            scroll_x.grid(row=1, column=0, sticky="ew")
            body.rowconfigure(0, weight=1)
            body.columnconfigure(0, weight=1)
            log_text = read_task_log(record)
            if record.get("run_id", "").startswith("legacy_"):
                log_text = ("Подробный журнал предыдущих версий не сохранялся по запускам.\n\n"
                            + str(record.get("message") or ""))
            viewer.insert("1.0", log_text)
            viewer.configure(state="disabled")
            buttons = ttk.Frame(win, padding=(12, 3, 12, 10))
            buttons.pack(fill="x")
            def copy_log():
                win.clipboard_clear()
                win.clipboard_append(log_text)
            ttk.Button(buttons, text="Копировать лог", command=copy_log).pack(side="left")
            ttk.Button(buttons, text="Закрыть", command=win.destroy).pack(side="right")

        def refresh_service_status(self):
            # Windows SCM is the source of truth. Query via ctypes instead of
            # requiring pywin32 in GUI (pywin32 is still needed by the service).
            try:
                agent_label, service_label = service_labels(query_service_state("OneCBackupManager"))
            except PermissionError:
                agent_label, service_label = "Агент: нет доступа", "Служба: недостаточно прав для проверки"
            except OSError as exc:
                agent_label, service_label = "Агент: ошибка проверки", f"Служба: {exc}"
            self.agent_status.set(tr(self.cfg.get("ui_language", "ru"), agent_label))
            self.service_status.set(tr(self.cfg.get("ui_language", "ru"), service_label))
            self.agent_status_label.configure(
                foreground=("#176c2b" if agent_label == "Агент запущен" else "#a13b24")
            )

        def periodic(self):
            self.refresh_jobs(); self.refresh_journal(); self.after(5000, self.periodic)

        def _log(self, line):
            self.q.put(("log", str(line)))

        def poll(self):
            try:
                while True:
                    kind, data = self.q.get_nowait()
                    if kind == "log":
                        for widget in (self.rawlog,):
                            widget.configure(state="normal")
                            widget.insert("end", data + "\n")
                            widget.see("end")
                            widget.configure(state="disabled")
                    elif kind in ("done", "error"):
                        # Task outcomes are already stored in the persistent per-run
                        # journal by execute_job(). Do not interrupt unattended GUI
                        # sessions with modal dialogs (including success dialogs).
                        # Active tasks and the Journal tab show the outcome.
                        self.worker = None
                        self.refresh_jobs()
                        self.refresh_journal()
            except queue.Empty:
                pass
            self.after(200, self.poll)

        def persist(self, cfg=None, sec=None):
            if self.secrets_error:
                raise RuntimeError("Невозможно сохранить: secrets.dat не расшифрован.")
            cfg = deepcopy(cfg if cfg is not None else self.cfg)
            cfg.setdefault("ftp_profiles", [])
            cfg.setdefault("task_folders", [])
            cfg["databases"] = [self.backend.normalize_db(job) for job in self.jobs]
            sec = deepcopy(sec if sec is not None else self.sec)
            self.backend.save_secrets(sec.get("cluster_password", ""), sec.get("db_passwords", {}),
                                      sec.get("sql_passwords", {}), sec.get("ftp_password", ""),
                                      sec.get("ftp_profile_passwords", {}), sec.get("cluster_job_passwords", {}),
                                      sec.get("smtp_password", ""), sec.get("telegram_bot_token", ""),
                                      sec.get("max_bot_token", ""))
            self.backend.save_config(cfg)
            self.cfg, self.sec = cfg, sec
            self.refresh_jobs()

        def open_profiles(self):
            StorageProfilesDialog(self)

        def new_job_menu(self):
            TaskTypeDialog(self, self.new_job)

        def new_job(self, kind):
            i = len(self.jobs) + 1
            job = self.backend.normalize_db({
                "id": uuid.uuid4().hex, "type": kind, "name": f"Новое задание {i}", "ref": f"job_{i}",
                "ib_ref": f"job_{i}", "one_c_server": "", "connection_kind": "server",
                "platform_mode": "auto", "enabled": False, "archive_format": "zip" if kind in {"files", "extra_copy"} else "none", "sql_server": ".",
                "sql_driver": "ODBC Driver 18 for SQL Server", "keep": 7,
            })
            job["task_group"] = self.selected_folder()
            EffectorTaskEditor(self, job, lambda updated, passwd, sqlpass, clusterpass:
                               self.apply_job(updated, passwd, sqlpass, clusterpass, None))

        def edit_job(self):
            idx = self._index()
            if idx is None:
                return
            EffectorTaskEditor(self, self.jobs[idx], lambda updated, passwd, sqlpass, clusterpass:
                               self.apply_job(updated, passwd, sqlpass, clusterpass, idx))

        def apply_job(self, job, passwd, sqlpass, clusterpass, index):
            key = self.backend.job_key(job)
            if any(str(other.get("ref", "")).casefold() == str(job["ref"]).casefold() and i != index for i, other in enumerate(self.jobs)):
                raise ValueError("Идентификатор Ref уже используется другой задачей.")
            draft = deepcopy(self.jobs)
            if index is None:
                draft.append(job)
            else:
                draft[index] = job
            saved_jobs, saved_sec = self.jobs, deepcopy(self.sec)
            try:
                self.jobs = draft
                if job.get("type") in {"1c", "1c_check", "1c_procedure"}:
                    self.sec.setdefault("db_passwords", {})[key] = passwd
                    if job.get("cluster_user"):
                        self.sec.setdefault("cluster_job_passwords", {})[key] = clusterpass
                elif job.get("type") == "mssql":
                    self.sec.setdefault("sql_passwords", {})[key] = sqlpass
                self.persist()
            except Exception:
                self.jobs, self.sec = saved_jobs, saved_sec
                raise
            self.tree.selection_set(f"job_{index if index is not None else len(self.jobs)-1}")

        def clear_jobs(self):
            if not self.jobs:
                return
            if not messagebox.askyesno("Очистка списка заданий", "Удалить из программы все задания? Архивы на диске НЕ удаляются."):
                return
            saved = self.jobs
            try:
                self.jobs = []
                self.persist()
                self.refresh_journal()
            except Exception:
                self.jobs = saved
                raise

        def import_bases(self):
            InfobaseImportDialog(self, on_import=self.apply_import)

        def apply_import(self, selected):
            draft = deepcopy(self.jobs)
            added = 0
            skipped = 0
            for item in selected:
                if any(j.get("type", "1c") == "1c" and j.get("connection_kind", "server") == item.kind and ((item.kind == "server" and str(j.get("one_c_server") or self.cfg.get("server", "")).casefold() == item.server.casefold() and str(j.get("ib_ref") or j.get("ref", "")).casefold() == item.ref.casefold()) or (item.kind == "file" and str(j.get("one_c_file", "")).casefold() == item.path.casefold()) or (item.kind == "web" and str(j.get("one_c_url", "")).casefold() == item.url.casefold())) for j in draft):
                    skipped += 1
                    continue
                draft.append(self.backend.normalize_db(item_to_job(item, draft)))
                added += 1
            self.jobs = draft
            self.persist()
            messagebox.showinfo("Импорт из 1С", f"Добавлено: {added}\nУже были: {skipped}\nНовые задания выключены до проверки.")

        def clone_job(self):
            idx = self._index()
            if idx is None:
                return
            orig = self.jobs[idx]
            clone = deepcopy(orig)
            clone["id"] = uuid.uuid4().hex
            clone["ref"] = str(clone["ref"]) + "_copy"
            clone["name"] = str(clone["name"]) + " (копия)"
            clone["enabled"] = False
            clone["schedule_time"] = ""
            password = self.sec.get("db_passwords", {}).get(self.backend.job_key(orig), "")
            sqlpass = self.sec.get("sql_passwords", {}).get(self.backend.job_key(orig), "")
            clusterpass = self.sec.get("cluster_job_passwords", {}).get(self.backend.job_key(orig), "")
            self.apply_job(self.backend.normalize_db(clone), password, sqlpass, clusterpass, None)

        def delete_job(self):
            idx = self._index()
            if idx is None:
                return
            if not messagebox.askyesno("Удаление", f"Удалить задание «{self.jobs[idx].get('name')}»?"):
                return
            self.jobs.pop(idx)
            self.persist()

        def open_settings(self):
            SystemSettingsDialog(self)

        def start_worker(self, work):
            if self.worker and self.worker.is_alive():
                messagebox.showwarning("Задания", "Уже выполняется операция резервного копирования.")
                return
            def runner():
                try:
                    msg = work()
                    self.q.put(("done", msg or "Операция завершена"))
                except Exception as exc:
                    self._log(traceback.format_exc())
                    self.q.put(("error", self.backend.friendly_error_message(exc, str(exc))))
            self.worker = threading.Thread(target=runner, daemon=True)
            self.worker.start()

        def _backup_job(self, job):
            self._log(f"Выполнение задания: {job['name']}")
            return self.backend.execute_job(self.cfg, self.sec, job, self._log, scheduled=False)

        def run_selected(self):
            idx = self._index()
            if idx is None:
                return
            job = deepcopy(self.jobs[idx]); job["enabled"] = True
            def work():
                result = self._backup_job(job)
                return f"Выполнено: {job['name']}" + (f"\n{result}" if result else "")
            self.start_worker(work)

        def run_all(self):
            jobs = [deepcopy(x) for x in self.jobs if x.get("enabled")]
            if not jobs:
                messagebox.showinfo("Задания", "Нет включённых заданий.")
                return
            def worker():
                fails = []
                for job in jobs:
                    try: self._backup_job(job)
                    except Exception as exc: fails.append(f"{job.get('name')}: {exc}")
                if fails:
                    raise RuntimeError("Не все задания выполнены:\n" + "\n".join(fails))
                return f"Задания выполнены: {len(jobs)}"
            self.start_worker(worker)

        def retry_ftp(self):
            idx = self._index()
            if idx is None:
                return
            job = deepcopy(self.jobs[idx])
            self.start_worker(lambda: f"FTP: {self.backend.retry_ftp_last(self.cfg, self.sec, job, self._log)}")

        def test_selected(self):
            idx = self._index()
            if idx is None:
                return
            job = deepcopy(self.jobs[idx])
            def worker():
                typ = self.backend.job_type(job)
                if typ in {"1c", "1c_check", "1c_procedure"}:
                    if typ == "1c_procedure":
                        f = Path(str(job.get("procedure_epf", "")))
                        if f.suffix.lower() != ".epf" or not f.is_file():
                            raise RuntimeError("Не найдена внешняя обработка .epf")
                    if typ == "1c_check":
                        from onec_tasks_8 import build_check_options
                        build_check_options(job)
                    if job.get("connection_kind") == "web":
                        raise RuntimeError("Ws-подключение не поддерживает DumpIB")
                    if job.get("connection_kind") == "file":
                        p = Path(job.get("one_c_file", "")) / "1Cv8.1CD"
                        if not p.is_file():
                            raise RuntimeError(f"Файловая 1С не найдена: {p}")
                    else:
                        cfg = deepcopy(self.cfg)
                        server = job.get("one_c_server", "")
                        if server:
                            cfg["server"] = server
                        if server and server.casefold() != self.cfg.get("server", "").casefold():
                            old_server = str(self.cfg.get("server", "")).strip()
                            cfg["ras_endpoint"] = job.get("one_c_ras") or (f"{server}:1545" if old_server else "127.0.0.1:1545")
                            cfg["cluster_agent"] = f"{server}:1540"
                            cfg["cluster_uuid"] = ""
                        eng = self.backend.BackupEngine(cfg, self.sec, self._log)
                        try:
                            eng.prepare(); clu = eng.get_cluster_uuid(); iid, _ = eng.find_infobase(clu, job.get("ib_ref") or job.get("ref")); eng.get_infobase_info(clu, iid, job)
                        finally:
                            eng.close()
                elif typ == "mssql":
                    from backup_extensions import sql_check_connection
                    sql_check_connection(job, self.sec)
                elif typ == "report":
                    if not (job.get("notify_email") or job.get("notify_telegram") or job.get("notify_max")):
                        raise RuntimeError("Выберите SMTP, Telegram и/или MAX")
                elif typ == "commands":
                    if not job.get("command_body", "").strip():
                        raise RuntimeError("Не заданы команды")
                else:
                    paths = [x.strip() for x in job.get("file_paths", "").splitlines() if x.strip()]
                    if not paths:
                        raise RuntimeError("Укажите файлы или папки")
                    for p in paths:
                        if not Path(p).exists():
                            raise RuntimeError(f"Не найден источник: {p}")
                return f"Проверка подключения выполнена: {job['name']}"
            self.start_worker(worker)

        def service_command(self, command):
            if self.secrets_error:
                messagebox.showerror("Служба", "Сохранённые пароли DPAPI не расшифрованы", parent=self)
                return
            if not self.backend.SERVICE_AVAILABLE:
                messagebox.showwarning("Служба", "Поддержка pywin32 недоступна на этом компьютере", parent=self)
                return
            exe = Path(sys.executable if getattr(sys, "frozen", False) else Path(__file__).resolve())
            actual = command
            if command == "install":
                try:
                    self.backend.win32serviceutil.QueryServiceStatus("OneCBackupManager")
                    actual = "update"
                except Exception:
                    actual = "install"
            try:
                result = subprocess.run([str(exe), actual], capture_output=True,
                                        creationflags=self.backend.CREATE_NO_WINDOW)
                output = self.backend.decode_service_output(result.stdout or b"") + "\n" + self.backend.decode_service_output(result.stderr or b"")
                if result.returncode:
                    raise RuntimeError(self.backend.friendly_service_error(output, result.returncode))
                if command == "install":
                    subprocess.run(["sc.exe", "config", "OneCBackupManager", "start=", "auto"],
                                   check=True, capture_output=True, creationflags=self.backend.CREATE_NO_WINDOW)
                self.refresh_service_status()
                messagebox.showinfo("Служба", "Команда выполнена. Проверьте Windows-пользователя службы для DPAPI.", parent=self)
            except Exception as exc:
                messagebox.showerror("Служба", str(exc), parent=self)

        def unblock_all(self):
            if not messagebox.askyesno("Аварийная разблокировка",
                    "Разрешить новые сеансы и регламентные задания в активных серверных базах 1С?", parent=self):
                return
            def work():
                for job in self.jobs:
                    if job.get("type") != "1c" or not job.get("enabled") or job.get("connection_kind", "server") != "server":
                        continue
                    cfg = deepcopy(self.cfg)
                    srv = job.get("one_c_server", "")
                    if srv:
                        cfg["server"] = srv
                    eng = self.backend.BackupEngine(cfg, self.sec, self._log)
                    try:
                        eng.prepare()
                        eng.emergency_unblock(job)
                    finally:
                        eng.close()
                return "Сеансы серверных баз 1С разблокированы"
            self.start_worker(work)

        def poll_tray(self):
            try:
                while True:
                    action = self.tray_events.get_nowait()
                    if action == "show":
                        self.restore_from_tray()
                    elif action == "run_all":
                        self.run_all()
                    elif action == "exit":
                        self.exit_app()
            except queue.Empty:
                pass
            if self.winfo_exists():
                self.after(250, self.poll_tray)

        def restore_from_tray(self):
            # Called on Tk thread from the tray OR another interactive launch.
            # Works for withdrawn and iconic windows alike.
            if self.state() in ("withdrawn", "iconic"):
                self.deiconify()
            self.lift()
            try:
                self.attributes("-topmost", True)
                self.after(200, lambda: self.attributes("-topmost", False))
            except tk.TclError:
                pass
            self.focus_force()

        def hide_to_tray(self):
            if not tray_available():
                messagebox.showwarning("Системный трей",
                    "Для трея установите pystray и Pillow либо соберите EXE через build_windows.bat.", parent=self)
                return
            try:
                if self.tray_controller is None:
                    self.tray_controller = TrayController(self.tray_events.put)
                self.tray_controller.start()
                if not self.tray_controller.icon or not self.tray_controller.icon.visible:
                    raise RuntimeError("Значок трея не отображается; окно останется открытым.")
                self.withdraw()
            except Exception as exc:
                self.deiconify()
                messagebox.showerror("Системный трей", str(exc), parent=self)

        def _on_unmap(self, event):
            if event.widget is not self or not self.cfg.get("ui_minimize_to_tray", True):
                return
            # Wait for the window manager to finish minimization before checking.
            self.after(120, self._minimize_if_iconic)

        def _minimize_if_iconic(self):
            if self.winfo_exists() and self.state() == "iconic" and tray_available():
                self.hide_to_tray()

        def on_close_request(self):
            action = str(self.cfg.get("ui_close_action", "ask"))
            if action == "ask":
                choice = messagebox.askyesnocancel(
                    "Закрытие программы",
                    "Свернуть 1C Backup Manager в системный трей?\n\n"
                    "Да — свернуть в трей\nНет — закрыть интерфейс\nОтмена — оставить окно открытым\n\n"
                    "Windows-служба продолжит выполнять задания отдельно.", parent=self)
                if choice is None:
                    return
                action = "tray" if choice else "exit"
            if action == "tray":
                self.hide_to_tray()
            else:
                self.exit_app()

        def exit_app(self):
            if self.worker and self.worker.is_alive():
                messagebox.showwarning("Выполняется резервное копирование",
                    "Сейчас выполняется задача. Дождитесь завершения или сверните окно в трей.\n"
                    "Закрывать GUI во время DumpIB небезопасно.", parent=self)
                return
            if self.tray_controller:
                try:
                    self.tray_controller.stop()
                except Exception:
                    pass
                self.tray_controller = None
            self.destroy()

    return App
