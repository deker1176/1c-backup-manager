"""Classic split-panel GUI inspired by task managers: tasks above, journal below.

All 1C/SQL/file/FTP implementation remains in the main engine.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import uuid
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ibases_import import (
    Infobase, default_ibases_candidates, read_ibases, item_to_job
)

TYPES = {"1c": "Резервное копирование 1С", "mssql": "Резервное копирование MS SQL", "files": "Резервное копирование файлов"}
SHORT_TYPES = {"1c": "1С:Предприятие", "mssql": "Microsoft SQL Server", "files": "Файлы и папки"}
SOURCE_DISPLAY = {"server": "Клиент-серверная", "file": "Файловая", "web": "Веб-подключение"}
PLATFORM_DISPLAY = {"inherit": "Из общих параметров", "auto": "Автоматически (последняя)", "manual": "Указать 1cv8.exe"}


def add_line(parent, label, variable, row: int, password=False, width=40, col=0):
    ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(3, 12), pady=7)
    ent = ttk.Entry(parent, textvariable=variable, width=width, show="●" if password else "")
    ent.grid(row=row, column=col + 1, sticky="ew", pady=7, padx=3)
    parent.columnconfigure(col + 1, weight=1)
    return ent


def title_label(parent, text, description=""):
    ttk.Label(parent, text=text, style="DialogHeader.TLabel").pack(anchor="w", pady=(0, 5))
    if description:
        ttk.Label(parent, text=description, foreground="#52677b", wraplength=670, justify="left").pack(anchor="w", pady=(0, 15))


def spin_time(frame, hour, minute):
    group = ttk.Frame(frame)
    ttk.Spinbox(group, from_=0, to=23, increment=1, wrap=True,
                textvariable=hour, format="%02.0f", width=4).pack(side="left")
    ttk.Label(group, text=":").pack(side="left", padx=3)
    ttk.Spinbox(group, from_=0, to=59, increment=1, wrap=True,
                textvariable=minute, format="%02.0f", width=4).pack(side="left")
    ttk.Button(group, text="Очистить", command=lambda: (hour.set(""), minute.set(""))).pack(side="left", padx=10)
    return group


class InfobaseImportDialog(tk.Toplevel):
    def __init__(self, owner, on_import=None, on_select=None):
        super().__init__(owner)
        self.title("Выбор баз 1С из стартера")
        self.geometry("940x610")
        self.minsize(790, 500)
        self.transient(owner)
        self.grab_set()
        self.on_import = on_import
        self.on_select = on_select
        self.items = []
        self.path = tk.StringVar()
        self.message = tk.StringVar(value="Выберите файл списка информационных баз 1С")
        for candidate in default_ibases_candidates():
            if candidate.exists():
                self.path.set(str(candidate))
                break
        if not self.path.get():
            self.path.set(str(default_ibases_candidates()[0]))
        self._build()
        if Path(self.path.get()).exists():
            self.load_file(silent=True)

    def _build(self):
        pad = ttk.Frame(self, padding=14)
        pad.pack(fill="both", expand=True)
        title_label(pad, "Импорт информационных баз", "Читается список 1С пользователя Windows. Исходный ibases.v8i не изменяется.")
        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="Файл:").pack(side="left", padx=(0, 6))
        ttk.Entry(row, textvariable=self.path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор...", command=self.browse).pack(side="left", padx=5)
        ttk.Button(row, text="Обновить", command=self.load_file).pack(side="left")

        self.tree = ttk.Treeview(pad, columns=("source", "kind", "locator", "note"), show="headings", selectmode="extended", height=15)
        for name, title, width in [
            ("source", "Название в 1С", 240),
            ("kind", "Тип", 85),
            ("locator", "Подключение", 295),
            ("note", "Поддержка DT", 205),
        ]:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, stretch=name in {"source", "locator"})
        self.tree.tag_configure("web", foreground="#9a5b36")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _: self.confirm())

        ttk.Label(pad, textvariable=self.message, foreground="#52677b").pack(anchor="w", pady=(8, 4))
        ttk.Label(
            pad,
            text="Импортированные задания создаются выключенными: сначала проверьте доступ к серверу/RAS. "
                 "Веб-базы Ws доступны для просмотра, но локальный DumpIB для них не выполняется.",
            wraplength=860, justify="left", foreground="#52677b"
        ).pack(anchor="w")
        btns = ttk.Frame(pad)
        btns.pack(fill="x", pady=(16, 0))
        ttk.Button(btns, text="Выбрать все", command=self.select_all).pack(side="left")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Импортировать выбранные" if self.on_import else "Выбрать базу",
                   command=self.confirm).pack(side="right", padx=8)

    def browse(self):
        path = filedialog.askopenfilename(parent=self, title="Список баз 1С", filetypes=[("Списки 1С", "*.v8i"), ("Все файлы", "*.*")])
        if path:
            self.path.set(path)
            self.load_file()

    def load_file(self, silent=False):
        try:
            self.items = read_ibases(self.path.get())
            for iid in self.tree.get_children():
                self.tree.delete(iid)
            for i, x in enumerate(self.items):
                if x.kind == "server":
                    kind, location, note = "Сервер", f"{x.server} / {x.ref}", "Нужен доступ к RAS"
                elif x.kind == "file":
                    kind, location, note = "Файловая", x.path, "Конфигуратор /DumpIB"
                else:
                    kind, location, note = "Веб", x.url, "Нет прямого DumpIB"
                self.tree.insert("", "end", iid=str(i), values=(x.name, kind, location, note), tags=("web",) if x.kind == "web" else ())
            self.message.set(f"Найдено записей 1С: {len(self.items)}. Выберите нужные строки.")
        except Exception as exc:
            self.message.set(f"Ошибка чтения списка: {exc}")
            if not silent:
                messagebox.showerror("Список баз", str(exc), parent=self)

    def select_all(self):
        self.tree.selection_set([str(i) for i in range(len(self.items)) if self.items[i].kind != 'web'])

    def confirm(self):
        ids = self.tree.selection()
        if not ids:
            messagebox.showwarning("Выбор баз", "Выделите хотя бы одну базу.", parent=self)
            return
        items = [self.items[int(i)] for i in ids]
        if self.on_select:
            if len(items) != 1:
                messagebox.showwarning("Выбор базы", "Выберите одну информационную базу.", parent=self)
                return
            self.on_select(items[0])
        elif self.on_import:
            self.on_import(items)
        self.destroy()


class ScrollablePage(ttk.Frame):
    """Scrollable editor page so 1C credentials do not clip on small screens."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, background="#f4f4f4")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.content = ttk.Frame(self.canvas, padding=12)
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_size)
        self.canvas.bind("<Configure>", self._update_width)

    def _update_size(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _update_width(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)


class TaskEditor(tk.Toplevel):
    """Settings are a separate editor, like a traditional backup task manager."""
    def __init__(self, owner, job: dict, on_save):
        super().__init__(owner)
        self.owner = owner
        self.backend = owner.backend
        self.job = deepcopy(job)
        self.on_save = on_save
        self.title(f"Настройка задания — {self.job.get('name') or 'Новое задание'}")
        self.geometry("1050x710")
        self.minsize(875, 590)
        self.transient(owner)
        self.grab_set()
        self.v = {}
        self._build_variables()
        self._build()

    def _build_variables(self):
        def sv(key, fallback=""):
            self.v[key] = tk.StringVar(value=str(self.job.get(key, fallback)))
        def bv(key, fallback=False):
            self.v[key] = tk.BooleanVar(value=bool(self.job.get(key, fallback)))
        for field in ["name", "ref", "ib_ref", "ib_user", "one_c_server", "one_c_file", "one_c_url", "one_c_ras",
                      "one_c_exe", "sql_server", "sql_database", "sql_backup_dir", "sql_driver",
                      "sql_user", "folder", "keep", "archive_format", "platform_mode"]:
            sv(field)
        self.legacy_server_default = self.job.get("type", "1c") == "1c" and not self.job.get("one_c_server")
        self.legacy_ib_ref_default = self.job.get("type", "1c") == "1c" and not self.job.get("ib_ref")
        if self.legacy_server_default:
            self.v["one_c_server"].set(self.owner.cfg.get("server", ""))
        if self.legacy_ib_ref_default:
            self.v["ib_ref"].set(self.job.get("ref", ""))
        for field in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth", "sql_trust_cert", "sql_remove_stage"]:
            bv(field)
        self.v["connection_kind"] = tk.StringVar(value=self.job.get("connection_kind", "server"))
        self.kind_display = tk.StringVar(value=SOURCE_DISPLAY.get(self.v["connection_kind"].get(), "Клиент-серверная"))
        self.platform_display = tk.StringVar(value=PLATFORM_DISPLAY.get(self.v["platform_mode"].get(), "Из общих параметров"))
        self.v["ib_password"] = tk.StringVar(value=self.owner.sec.get("db_passwords", {}).get(
            self.backend.job_key(self.job), self.owner.sec.get("db_passwords", {}).get(self.job.get("ref", ""), "")))
        self.v["sql_password"] = tk.StringVar(value=self.owner.sec.get("sql_passwords", {}).get(self.backend.job_key(self.job), ""))
        tm = str(self.job.get("schedule_time", ""))
        h, m = (tm.split(":") + [""])[:2] if ":" in tm else ("", "")
        self.hour = tk.StringVar(value=h)
        self.minute = tk.StringVar(value=m)

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        head = ttk.Frame(root)
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text="Настройка задания", style="DialogHeader.TLabel").pack(side="left")
        ttk.Label(head, text=SHORT_TYPES.get(self.job.get("type", "1c"), ""), foreground="#52677b").pack(side="right")
        ttk.Separator(root).pack(fill="x", pady=(0, 8))

        central = ttk.Frame(root)
        central.pack(fill="both", expand=True)
        sidebar = ttk.Frame(central, padding=(0, 0, 14, 0), width=210)
        sidebar.pack(side="left", fill="y")
        ttk.Label(sidebar, text="РАЗДЕЛЫ", font=("Segoe UI", 9, "bold"), foreground="#607185").pack(anchor="w", pady=(5, 7))
        self.nav = tk.Listbox(sidebar, width=27, height=14, borderwidth=0, highlightthickness=0,
                              font=("Segoe UI", 10), activestyle="none", selectbackground="#d9e9fb",
                              selectforeground="#163a5b")
        self.nav.pack(fill="y", expand=True)
        pages = ["Общие параметры", "Подключение к источнику", "Архивирование", "FTP / FTPS", "Расписание"]
        for item in pages:
            self.nav.insert("end", "   " + item)
        self.nav.bind("<<ListboxSelect>>", self._select_page)

        right = ttk.Frame(central)
        right.pack(side="left", fill="both", expand=True)
        self.pages = []
        for _ in pages:
            page = ScrollablePage(right)
            page.place(x=0, y=0, relwidth=1, relheight=1)
            self.pages.append(page)
        self.build_common(self.pages[0].content)
        self.build_source(self.pages[1].content)
        self.build_archive(self.pages[2].content)
        self.build_ftp(self.pages[3].content)
        self.build_schedule(self.pages[4].content)
        self.nav.selection_set(0)
        self.pages[0].lift()

        ttk.Separator(root).pack(fill="x", pady=(12, 12))
        actions = ttk.Frame(root)
        actions.pack(fill="x")
        ttk.Button(actions, text="Проверить настройки", command=self.check).pack(side="left", padx=3)
        ttk.Button(actions, text="Отмена", command=self.destroy).pack(side="right", padx=3)
        ttk.Button(actions, text="Сохранить", command=self.save, style="Accent.TButton").pack(side="right", padx=8)

    def _select_page(self, event=None):
        choice = self.nav.curselection()
        if choice:
            self.pages[choice[0]].lift()

    def build_common(self, page):
        title_label(page, "Основные параметры", "Название, идентификатор и включение задания в расписание.")
        form = ttk.Frame(page)
        form.pack(fill="x")
        add_line(form, "Название задания", self.v["name"], 0)
        add_line(form, "Идентификатор", self.v["ref"], 1)
        ttk.Checkbutton(form, text="Задание активно", variable=self.v["enabled"]).grid(row=2, column=1, sticky="w", pady=12)
        ttk.Separator(page).pack(fill="x", pady=15)
        ttk.Label(page, text=f"Тип: {SHORT_TYPES.get(self.job.get('type', '1c'))}", foreground="#52677b").pack(anchor="w")
        ttk.Label(page, text="Двойной щелчок по заданию в главном окне открывает эту форму.", foreground="#52677b").pack(anchor="w", pady=8)

    def build_source(self, page):
        kind = self.job.get("type", "1c")
        if kind == "1c":
            title_label(page, "Подключение к базе 1С", "Выберите информационную базу из стартера или задайте подключение вручную.")
            ttk.Button(page, text="Выбрать информационную базу из списка 1С...", command=self.choose_launcher_base).pack(anchor="w", pady=(0, 12))
            row = ttk.Frame(page)
            row.pack(fill="x", pady=(0, 8))
            ttk.Label(row, text="Вариант базы:").pack(side="left", padx=(0, 12))
            tkind = ttk.Combobox(row, textvariable=self.kind_display, state="readonly", width=25,
                                 values=list(SOURCE_DISPLAY.values()))
            tkind.pack(side="left")
            tkind.bind("<<ComboboxSelected>>", lambda _: self.change_source_kind())
            self.variant = ttk.Frame(page)
            self.variant.pack(fill="x")
            self.variant_groups = {}
            c = ttk.LabelFrame(self.variant, text="Клиент-серверная база", padding=8)
            self.variant_groups["server"] = c
            add_line(c, "Сервер 1С", self.v["one_c_server"], 0)
            add_line(c, "Имя базы на сервере (Ref)", self.v["ib_ref"], 1)
            add_line(c, "RAS адрес (если не стандартный)", self.v["one_c_ras"], 2)
            ttk.Label(c, text="Для других серверов должен быть доступен RAS. Сам импорт его не запускает.",
                      foreground="#52677b", wraplength=560).grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
            c = ttk.LabelFrame(self.variant, text="Файловая база", padding=8)
            self.variant_groups["file"] = c
            add_line(c, "Каталог базы (с 1Cv8.1CD)", self.v["one_c_file"], 0)
            ttk.Button(c, text="Выбрать каталог...", command=lambda: self.browse_dir("one_c_file")).grid(row=1, column=1, sticky="w", pady=6)
            ttk.Label(c, text="Перед DumpIB пользователи должны закрыть файловую базу; rac здесь не используется.",
                      foreground="#52677b", wraplength=560).grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
            c = ttk.LabelFrame(self.variant, text="Веб-подключение", padding=8)
            self.variant_groups["web"] = c
            add_line(c, "URL базы", self.v["one_c_url"], 0)
            ttk.Label(c, text="Для Ws соединения прямой DumpIB в этой программе недоступен. Настройте доступ на стороне сервера 1С.",
                      foreground="#a9542b", wraplength=550).grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
            self.refresh_source_kind()

            exe = ttk.LabelFrame(page, text="Платформа 1С для резервного копирования", padding=8)
            exe.pack(fill="x", pady=(10, 6))
            tkind = ttk.Combobox(exe, textvariable=self.platform_display, state="readonly",
                                 values=list(PLATFORM_DISPLAY.values()), width=28)
            ttk.Label(exe, text="Выбор версии").grid(row=0, column=0, sticky="w", padx=3)
            tkind.grid(row=0, column=1, sticky="w", pady=3)
            add_line(exe, "1cv8.exe (ручной режим)", self.v["one_c_exe"], 1)
            ttk.Button(exe, text="...", width=3, command=self.browse_exe).grid(row=1, column=2)
            ttk.Label(exe, text="auto — последняя установленная 1cv8.exe; inherit — версия из общих параметров;\n"
                                "manual — конкретный 1cv8.exe. 1cestart.exe для ночного DumpIB не требуется.",
                      foreground="#52677b").grid(row=2, column=0, columnspan=3, sticky="w", pady=7)

            credentials = ttk.LabelFrame(page, text="Учётная запись базы", padding=8)
            credentials.pack(fill="x")
            add_line(credentials, "Пользователь", self.v["ib_user"], 0)
            add_line(credentials, "Пароль", self.v["ib_password"], 1, password=True)
        elif kind == "mssql":
            title_label(page, "Microsoft SQL Server", "SQL Server создаёт .bak через BACKUP DATABASE; подключение должно иметь необходимые права.")
            form = ttk.Frame(page)
            form.pack(fill="x")
            for i, (key, label) in enumerate([
                ("sql_server", "Сервер / экземпляр"), ("sql_database", "Имя базы SQL"),
                ("sql_backup_dir", "Каталог для .bak (SQL Server)"), ("sql_driver", "ODBC Driver"),
                ("sql_user", "Пользователь SQL")]):
                add_line(form, label, self.v[key], i)
            add_line(form, "Пароль SQL", self.v["sql_password"], 5, password=True)
            ttk.Checkbutton(form, text="Авторизация Windows", variable=self.v["sql_windows_auth"]).grid(row=6, column=1, sticky="w", pady=5)
            ttk.Checkbutton(form, text="Доверять сертификату SQL-сервера", variable=self.v["sql_trust_cert"]).grid(row=7, column=1, sticky="w", pady=5)
            ttk.Checkbutton(form, text="Удалять исходный .bak после копирования в каталог архива", variable=self.v["sql_remove_stage"]).grid(row=8, column=1, sticky="w", pady=5)
        else:
            title_label(page, "Файлы и папки", "Выберите несколько источников. Создаётся полная копия в ZIP или 7z.")
            self.filebox = tk.Text(page, height=12, font=("Consolas", 10), wrap="none")
            self.filebox.pack(fill="both", expand=True)
            self.filebox.insert("1.0", self.job.get("file_paths", ""))
            buttons = ttk.Frame(page)
            buttons.pack(fill="x", pady=10)
            ttk.Button(buttons, text="+ Файл", command=self.add_file).pack(side="left", padx=3)
            ttk.Button(buttons, text="+ Каталог", command=self.add_folder).pack(side="left", padx=3)
            ttk.Label(page, text="Один путь в каждой строке. Открытые файлы без VSS не гарантируют согласованность.",
                      foreground="#52677b", wraplength=650).pack(anchor="w")

    def change_source_kind(self):
        self.v["connection_kind"].set(next(
            (key for key, label in SOURCE_DISPLAY.items() if label == self.kind_display.get()), "server"
        ))
        self.refresh_source_kind()

    def refresh_source_kind(self):
        for group in self.variant_groups.values():
            group.pack_forget()
        kind = self.v["connection_kind"].get()
        if kind in self.variant_groups:
            self.variant_groups[kind].pack(fill="x")

    def choose_launcher_base(self):
        InfobaseImportDialog(self, on_select=self.set_launcher_base)

    def set_launcher_base(self, item: Infobase):
        self.v["name"].set(item.name)
        self.v["connection_kind"].set(item.kind)
        self.kind_display.set(SOURCE_DISPLAY[item.kind])
        if item.kind == "server":
            self.v["ib_ref"].set(item.ref)
            self.v["ref"].set(item.ref)
            self.v["one_c_server"].set(item.server)
        elif item.kind == "file":
            self.v["one_c_file"].set(item.path)
        else:
            self.v["one_c_url"].set(item.url)
            self.v["enabled"].set(False)
        self.refresh_source_kind()

    def browse_dir(self, key):
        result = filedialog.askdirectory(parent=self, title="Выберите каталог")
        if result:
            self.v[key].set(result)

    def browse_exe(self):
        result = filedialog.askopenfilename(parent=self, title="Укажите 1cv8.exe",
                                            filetypes=[("Исполняемые файлы", "*.exe")])
        if result:
            self.v["one_c_exe"].set(result)
            self.v["platform_mode"].set("manual")

    def add_file(self):
        path = filedialog.askopenfilename(parent=self)
        if path:
            self.filebox.insert("end", ("\n" if self.filebox.get("1.0", "end-1c").strip() else "") + path)

    def add_folder(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.filebox.insert("end", ("\n" if self.filebox.get("1.0", "end-1c").strip() else "") + path)

    def build_archive(self, page):
        title_label(page, "Архивирование и хранение", "Где сохранить готовый бэкап и сколько локальных копий оставить.")
        group = ttk.LabelFrame(page, text="Локальные копии", padding=10)
        group.pack(fill="x")
        add_line(group, "Каталог копий (пусто = автоматически)", self.v["folder"], 0)
        ttk.Button(group, text="Выбрать...", command=lambda: self.browse_dir("folder")).grid(row=1, column=1, sticky="w", pady=5)
        add_line(group, "Количество копий", self.v["keep"], 2)
        ttk.Label(group, text="Формат").grid(row=3, column=0, sticky="w", pady=7)
        ttk.Combobox(group, textvariable=self.v["archive_format"], values=["none", "zip", "7z"],
                     state="readonly", width=18).grid(row=3, column=1, sticky="w", pady=7)
        ttk.Checkbutton(group, text="Удалять исходный .dt / .bak после успешной упаковки",
                        variable=self.v["remove_raw_after_pack"]).grid(row=4, column=0, columnspan=2, sticky="w", pady=9)
        ttk.Label(group, text="none: исходный .dt или .bak; zip: ZIP; 7z: архив 7-Zip.",
                  foreground="#52677b").grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(page, text="Очистка старых локальных копий производится только после успешной упаковки и передачи FTP.",
                  wraplength=650, foreground="#52677b").pack(anchor="w", pady=18)

    def build_ftp(self, page):
        title_label(page, "Загрузка на FTP / FTPS", "Общее удалённое хранилище настраивается один раз в параметрах программы.")
        ttk.Checkbutton(page, text="Отправлять копию этого задания на FTP / FTPS",
                        variable=self.v["ftp_enabled"]).pack(anchor="w", pady=(3, 15))
        ftp = self.owner.cfg.get("ftp", {})
        protocol = "FTPS (TLS)" if ftp.get("tls", True) else "FTP (без защиты)"
        host = ftp.get("host") or "не настроен"
        path = ftp.get("path", "/")
        ttk.Label(page, text=f"Сервер: {host}    Протокол: {protocol}    Каталог: {path}",
                  foreground="#52677b", wraplength=660).pack(anchor="w", pady=8)
        ttk.Button(page, text="Параметры FTP-хранилища...", command=self.owner.open_settings).pack(anchor="w", pady=6)
        ttk.Label(page, text="Если передача не удалась, локальный архив сохраняется и его можно отправить повторно.",
                  foreground="#52677b", wraplength=660).pack(anchor="w", pady=16)

    def build_schedule(self, page):
        title_label(page, "Расписание автозапуска", "Каждая база, SQL и файловая задача имеют собственное ежедневное время запуска.")
        panel = ttk.Frame(page)
        panel.pack(fill="x", pady=12)
        ttk.Label(panel, text="Каждый день в:").pack(side="left", padx=(0, 14))
        spin_time(panel, self.hour, self.minute).pack(side="left")
        ttk.Label(page, text="Время можно выбрать стрелками или ввести вручную.\n"
                              "Очистите оба поля, чтобы отключить запуск по расписанию.",
                  foreground="#52677b", justify="left").pack(anchor="w", pady=12)
        ttk.Label(page, text="Если запланированное время наступило, но предыдущий бэкап ещё выполнялся, "
                              "задача будет запущена позже в тот же день.",
                  foreground="#52677b", wraplength=640).pack(anchor="w", pady=8)

    def check(self):
        try:
            self.extract()
            messagebox.showinfo("Проверка", "Поля заполнены корректно. Подключение к серверу проверяется отдельно из списка заданий.", parent=self)
        except Exception as exc:
            messagebox.showerror("Проверка", str(exc), parent=self)

    def extract(self):
        job = deepcopy(self.job)
        for key in ["name", "ref", "ib_ref", "ib_user", "one_c_server", "one_c_file", "one_c_url", "one_c_ras",
                    "one_c_exe", "sql_server", "sql_database", "sql_backup_dir", "sql_driver", "sql_user",
                    "folder", "archive_format", "platform_mode"]:
            job[key] = self.v[key].get().strip()
        job["keep"] = max(1, int(self.v["keep"].get() or "7"))
        for field in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth", "sql_trust_cert", "sql_remove_stage"]:
            job[field] = bool(self.v[field].get())
        job["connection_kind"] = self.v["connection_kind"].get()
        # Preserve global-server inheritance for tasks migrated from v3/v4.
        if self.legacy_server_default and job["one_c_server"].casefold() == str(self.owner.cfg.get("server", "")).casefold():
            job["one_c_server"] = ""
        if self.legacy_ib_ref_default and job["ib_ref"] == str(self.job.get("ref", "")):
            job["ib_ref"] = ""
        job["platform_mode"] = next((key for key, label in PLATFORM_DISPLAY.items()
                                     if label == self.platform_display.get()), "inherit")
        h, m = self.hour.get().strip(), self.minute.get().strip()
        if bool(h) != bool(m):
            raise ValueError("Заполните часы и минуты либо очистите оба поля.")
        job["schedule_time"] = self.backend.normalize_time_hhmm(f"{h}:{m}") if h else ""
        if not job["name"]:
            raise ValueError("Введите название задания.")
        if not job["ref"]:
            raise ValueError("Введите идентификатор задания.")
        if job.get("type") == "files":
            job["file_paths"] = self.filebox.get("1.0", "end-1c").strip()
            if job["archive_format"] == "none":
                raise ValueError("Для файлов выберите ZIP или 7z.")
        elif job.get("type") == "1c":
            if job["connection_kind"] == "web" and job.get("enabled"):
                raise ValueError("Web (Ws) не поддерживает DumpIB. Отключите задание или задайте серверное подключение.")
            if job["connection_kind"] == "file" and not job["one_c_file"]:
                raise ValueError("Укажите каталог файловой базы 1С.")
            if job["connection_kind"] == "server" and not (job["ib_ref"] or job["ref"]):
                raise ValueError("Укажите имя базы на сервере (Ref).")
            if job["platform_mode"] == "manual":
                if Path(job["one_c_exe"]).name.lower() != "1cv8.exe":
                    raise ValueError("Выберите конкретный файл 1cv8.exe, не 1cestart.exe.")
        return self.backend.normalize_db(job)

    def save(self):
        try:
            job = self.extract()
            self.on_save(job, self.v["ib_password"].get(), self.v["sql_password"].get())
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Настройка задания", str(exc), parent=self)


class SettingsDialog(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.title("Параметры программы и службы")
        self.geometry("940x660")
        self.minsize(780, 540)
        self.transient(owner)
        self.grab_set()
        cfg = owner.cfg
        sec = owner.sec
        self.v = {}
        for key in ("server", "ras_endpoint", "cluster_agent", "cluster_uuid", "cluster_user",
                    "platform_bin", "backup_root", "wait_seconds", "service_poll_seconds"):
            self.v[key] = tk.StringVar(value=str(cfg.get(key, "")))
        self.v["cluster_password"] = tk.StringVar(value=sec.get("cluster_password", ""))
        self.v["force_terminate"] = tk.BooleanVar(value=cfg.get("force_terminate", True))
        ftp = cfg.get("ftp", {})
        for key in ("host", "port", "user", "path"):
            self.v["ftp_" + key] = tk.StringVar(value=str(ftp.get(key, "")))
        self.v["ftp_tls"] = tk.BooleanVar(value=ftp.get("tls", True))
        self.v["ftp_password"] = tk.StringVar(value=sec.get("ftp_password", ""))
        self._build()

    def _build(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Параметры программы", style="DialogHeader.TLabel").pack(anchor="w", pady=(0, 10))
        tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True)
        c1, c2, c3 = [ttk.Frame(tabs, padding=16) for _ in range(3)]
        tabs.add(c1, text="Сервер 1С и платформа")
        tabs.add(c2, text="Локальное хранилище и FTP")
        tabs.add(c3, text="Служба Windows")

        ttk.Label(c1, text="Общие параметры для локального сервера 1С", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=5)
        form = ttk.Frame(c1)
        form.pack(fill="x", pady=10)
        for i, (key, label) in enumerate([
            ("server", "Сервер 1С"), ("ras_endpoint", "RAS"), ("cluster_agent", "Агент кластера"),
            ("cluster_uuid", "UUID кластера (если несколько)"), ("cluster_user", "Администратор кластера")]):
            add_line(form, label, self.v[key], i)
        add_line(form, "Пароль администратора кластера", self.v["cluster_password"], 5, password=True)
        add_line(form, "Папка bin платформы 1С", self.v["platform_bin"], 6)
        ttk.Button(form, text="Выбрать bin...", command=self.choose_bin).grid(row=7, column=1, sticky="w", pady=4)
        ttk.Label(c1, text="Если папка bin пустая, автоматически выбирается последняя установленная версия платформы 1С.",
                  foreground="#52677b").pack(anchor="w", pady=15)

        c2_form = ttk.Frame(c2)
        c2_form.pack(fill="x")
        add_line(c2_form, "Каталог резервных копий", self.v["backup_root"], 0)
        ttk.Button(c2_form, text="Выбрать...", command=self.choose_backup).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Separator(c2).pack(fill="x", pady=12)
        ttk.Label(c2, text="FTP / FTPS — общие реквизиты", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ftp = ttk.Frame(c2)
        ftp.pack(fill="x", pady=8)
        for i, (key, label) in enumerate([
            ("ftp_host", "Сервер FTP"), ("ftp_port", "Порт"), ("ftp_user", "Логин"),
            ("ftp_path", "Каталог на FTP")]):
            add_line(ftp, label, self.v[key], i)
        add_line(ftp, "Пароль FTP", self.v["ftp_password"], 4, password=True)
        ttk.Checkbutton(ftp, text="Использовать FTPS с проверкой TLS-сертификата",
                        variable=self.v["ftp_tls"]).grid(row=5, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Button(c2, text="Проверить соединение FTP", command=self.test_ftp).pack(anchor="w", pady=6)

        c3_form = ttk.Frame(c3)
        c3_form.pack(fill="x")
        add_line(c3_form, "Ждать выхода пользователей (сек.)", self.v["wait_seconds"], 0)
        add_line(c3_form, "Опрос расписания (сек.)", self.v["service_poll_seconds"], 1)
        ttk.Checkbutton(c3_form, text="После ожидания принудительно завершать сеансы 1С",
                        variable=self.v["force_terminate"]).grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(c3, text="После установки службы Windows настройте её запуск от имени того же пользователя,\n"
                           "который сохранил пароли DPAPI. Тип запуска — Автоматически.",
                  foreground="#52677b", justify="left").pack(anchor="w", pady=14)
        svcbuttons = ttk.Frame(c3)
        svcbuttons.pack(anchor="w", pady=7)
        for label, cmd in [("Установить / обновить", "install"), ("Запустить", "start"),
                           ("Остановить", "stop"), ("Удалить", "remove")]:
            ttk.Button(svcbuttons, text=label, command=lambda c=cmd: self.owner.service_command(c)).pack(side="left", padx=3)
        ttk.Button(c3, text="Аварийно разблокировать базы 1С", command=self.owner.unblock_all).pack(anchor="w", pady=8)

        ttk.Separator(root).pack(fill="x", pady=14)
        buttons = ttk.Frame(root)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="right", padx=3)
        ttk.Button(buttons, text="Сохранить", command=self.save).pack(side="right", padx=8)

    def choose_bin(self):
        p = filedialog.askdirectory(parent=self, title="Папка bin 1С")
        if p:
            self.v["platform_bin"].set(p)

    def choose_backup(self):
        p = filedialog.askdirectory(parent=self, title="Каталог архивов")
        if p:
            self.v["backup_root"].set(p)

    def test_ftp(self):
        try:
            self.owner.backend.ftp_check({
                "host": self.v["ftp_host"].get(), "port": int(self.v["ftp_port"].get() or 21),
                "user": self.v["ftp_user"].get(), "path": self.v["ftp_path"].get(),
                "tls": self.v["ftp_tls"].get()
            }, self.v["ftp_password"].get())
            messagebox.showinfo("FTP", "Соединение и каталог доступны.", parent=self)
        except Exception as exc:
            messagebox.showerror("FTP", str(exc), parent=self)

    def extract(self):
        cfg = deepcopy(self.owner.cfg)
        sec = deepcopy(self.owner.sec)
        for key in ("server", "ras_endpoint", "cluster_agent", "cluster_uuid", "cluster_user",
                    "platform_bin", "backup_root"):
            cfg[key] = self.v[key].get().strip()
        cfg["wait_seconds"] = max(0, int(self.v["wait_seconds"].get()))
        cfg["service_poll_seconds"] = max(10, int(self.v["service_poll_seconds"].get()))
        cfg["force_terminate"] = bool(self.v["force_terminate"].get())
        cfg["ftp"] = {
            "host": self.v["ftp_host"].get().strip(),
            "port": int(self.v["ftp_port"].get() or 21),
            "user": self.v["ftp_user"].get().strip(),
            "path": self.v["ftp_path"].get().strip(),
            "tls": bool(self.v["ftp_tls"].get()),
        }
        sec["cluster_password"] = self.v["cluster_password"].get()
        sec["ftp_password"] = self.v["ftp_password"].get()
        return cfg, sec

    def save(self):
        try:
            cfg, sec = self.extract()
            self.owner.persist(cfg, sec)
            messagebox.showinfo("Параметры", "Настройки сохранены. Служба подхватит расписание автоматически.", parent=self)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Параметры", str(exc), parent=self)


def create_app_class(backend):
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.backend = backend
            self.title("1C Backup Manager v4.1 — менеджер заданий")
            self.geometry("1220x820")
            self.minsize(980, 635)
            self.style()
            self.cfg = backend.load_config()
            self.secrets_error = None
            try:
                self.sec = backend.load_secrets()
            except Exception as exc:
                self.secrets_error = str(exc)
                self.sec = {"cluster_password": "", "db_passwords": {}, "sql_passwords": {}, "ftp_password": ""}
                messagebox.showerror("Пароли", "Пароли secrets.dat не расшифрованы.\n"
                                     "Сохранение отключено, чтобы не потерять существующие пароли.")
            self.jobs = [backend.normalize_db(j) for j in self.cfg.get("databases", [])]
            self.worker = None
            self.q = queue.Queue()
            self.summary = tk.StringVar()
            self.service_status = tk.StringVar(value="Проверка...")
            self._build()
            self.refresh_jobs()
            self.refresh_journal()
            self.after(180, self.poll)
            self.after(5000, self.periodic)

        def style(self):
            st = ttk.Style(self)
            if "vista" in st.theme_names():
                st.theme_use("vista")
            elif "clam" in st.theme_names():
                st.theme_use("clam")
            st.configure("Classic.Treeview", rowheight=26, font=("Segoe UI", 10))
            st.configure("Classic.Treeview.Heading", font=("Segoe UI", 9, "bold"))
            st.configure("DialogHeader.TLabel", font=("Segoe UI", 14, "bold"))
            st.configure("Toolbar.TButton", padding=(10, 6))

        def _build(self):
            menus = tk.Menu(self)
            filemenu = tk.Menu(menus, tearoff=0)
            filemenu.add_command(label="Импорт баз 1С...", command=self.import_bases)
            filemenu.add_command(label="Параметры программы...", command=self.open_settings)
            filemenu.add_command(label="Очистить список заданий...", command=self.clear_jobs)
            filemenu.add_separator()
            filemenu.add_command(label="Выход", command=self.destroy)
            menus.add_cascade(label="Файл", menu=filemenu)
            jobs_menu = tk.Menu(menus, tearoff=0)
            for kind in TYPES:
                jobs_menu.add_command(label="Добавить: " + SHORT_TYPES[kind], command=lambda k=kind: self.new_job(k))
            jobs_menu.add_separator()
            jobs_menu.add_command(label="Изменить", command=self.edit_job)
            jobs_menu.add_command(label="Копировать", command=self.clone_job)
            jobs_menu.add_command(label="Удалить", command=self.delete_job)
            jobs_menu.add_command(label="Выполнить сейчас", command=self.run_selected)
            jobs_menu.add_command(label="Проверить подключение", command=self.test_selected)
            jobs_menu.add_command(label="Повторить загрузку на FTP", command=self.retry_ftp)
            menus.add_cascade(label="Задания", menu=jobs_menu)
            service = tk.Menu(menus, tearoff=0)
            for label, cmd in [("Установить / обновить", "install"), ("Запустить", "start"),
                               ("Остановить", "stop"), ("Удалить", "remove")]:
                service.add_command(label=label, command=lambda c=cmd: self.service_command(c))
            menus.add_cascade(label="Служба", menu=service)
            helpmenu = tk.Menu(menus, tearoff=0)
            helpmenu.add_command(label="Telegram @dek76", command=lambda: webbrowser.open("https://t.me/dek76"))
            menus.add_cascade(label="Справка", menu=helpmenu)
            self.config(menu=menus)

            tools = ttk.Frame(self, padding=(10, 8))
            tools.pack(fill="x")
            ttk.Button(tools, text="+ Новое задание", command=self.new_job_menu, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Button(tools, text="Из списка 1С...", command=self.import_bases, style="Toolbar.TButton").pack(side="left", padx=4)
            ttk.Separator(tools, orient="vertical").pack(side="left", fill="y", padx=9)
            ttk.Button(tools, text="Изменить", command=self.edit_job, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Button(tools, text="Копия", command=self.clone_job, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Button(tools, text="Удалить", command=self.delete_job, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Separator(tools, orient="vertical").pack(side="left", fill="y", padx=9)
            ttk.Button(tools, text="▶ Выполнить", command=self.run_selected, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Button(tools, text="▶ Все активные", command=self.run_all, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Button(tools, text="Проверить", command=self.test_selected, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Button(tools, text="FTP повтор", command=self.retry_ftp, style="Toolbar.TButton").pack(side="left", padx=2)
            ttk.Button(tools, text="Параметры", command=self.open_settings, style="Toolbar.TButton").pack(side="right", padx=2)

            ttk.Separator(self).pack(fill="x")
            pane = ttk.Panedwindow(self, orient="vertical")
            pane.pack(fill="both", expand=True, padx=10, pady=(7, 0))
            above = ttk.Frame(pane)
            below = ttk.Frame(pane)
            pane.add(above, weight=6)
            pane.add(below, weight=4)

            notebook = ttk.Notebook(above)
            notebook.pack(fill="both", expand=True)
            tasks_page = ttk.Frame(notebook, padding=5)
            active_page = ttk.Frame(notebook, padding=5)
            notebook.add(tasks_page, text="Настройка заданий")
            notebook.add(active_page, text="Выполняющиеся задания")

            cols = ("type", "schedule", "last", "status")
            self.tree = ttk.Treeview(tasks_page, columns=cols, show="tree headings", selectmode="browse",
                                     style="Classic.Treeview")
            for col, label, width in [
                ("#0", "Наименование", 390), ("type", "Вид задачи", 260),
                ("schedule", "Время", 95), ("last", "Последний бэкап", 180),
                ("status", "Результат", 165),
            ]:
                self.tree.heading(col, text=label)
                self.tree.column(col, width=width, stretch=col in ("#0", "type"), anchor="w" if col in ("#0", "type") else "center")
            self.tree.tag_configure("disabled", foreground="#8892a0")
            self.tree.tag_configure("error", foreground="#a73737")
            self.tree.tag_configure("success", foreground="#267b49")
            scroll = ttk.Scrollbar(tasks_page, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=scroll.set)
            self.tree.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            self.tree.bind("<Double-1>", lambda _: self.edit_job())
            self.tree.bind("<Return>", lambda _: self.edit_job())

            ttk.Label(active_page, text="Текущие и последние операции", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=10, padx=8)
            self.active_text = tk.Text(active_page, height=7, font=("Consolas", 10), state="disabled", wrap="word")
            self.active_text.pack(fill="both", expand=True, padx=8, pady=5)

            logtabs = ttk.Notebook(below)
            logtabs.pack(fill="both", expand=True)
            journal_page = ttk.Frame(logtabs, padding=5)
            raw_page = ttk.Frame(logtabs, padding=5)
            backups_page = ttk.Frame(logtabs, padding=5)
            logtabs.add(journal_page, text="Журнал задач")
            logtabs.add(backups_page, text="Созданные архивы")
            logtabs.add(raw_page, text="Технический журнал")

            self.journal = ttk.Treeview(journal_page, columns=("time", "task", "status", "message"), show="headings", height=10)
            for col, label, width in [("time", "Время", 160), ("task", "Задание", 190),
                                      ("status", "Результат", 110), ("message", "Подробности", 640)]:
                self.journal.heading(col, text=label)
                self.journal.column(col, width=width, stretch=col == "message")
            self.journal.tag_configure("error", foreground="#a73737")
            self.journal.tag_configure("success", foreground="#267b49")
            self.journal.pack(fill="both", expand=True)

            self.archives = ttk.Treeview(backups_page, columns=("name", "size", "modified"), show="headings")
            for col, label, width in [("name", "Файл", 600), ("size", "Размер, МБ", 115), ("modified", "Изменён", 180)]:
                self.archives.heading(col, text=label)
                self.archives.column(col, width=width, stretch=col == "name")
            self.archives.pack(fill="both", expand=True)
            self.rawlog = tk.Text(raw_page, height=9, state="disabled", font=("Consolas", 9), wrap="word")
            self.rawlog.pack(fill="both", expand=True)

            status = ttk.Frame(self, padding=(10, 5))
            status.pack(fill="x")
            ttk.Label(status, textvariable=self.summary).pack(side="left")
            ttk.Label(status, textvariable=self.service_status).pack(side="left", padx=18)
            contact = ttk.Label(status, text="Telegram: @dek76", foreground="#1763a1", cursor="hand2")
            contact.pack(side="right")
            contact.bind("<Button-1>", lambda _: webbrowser.open("https://t.me/dek76"))

        def _index(self):
            selected = self.tree.selection()
            if not selected:
                return None
            iid = selected[0]
            if iid.startswith("job_"):
                try:
                    idx = int(iid[4:])
                    if 0 <= idx < len(self.jobs):
                        return idx
                except Exception:
                    pass
            return None

        def refresh_jobs(self):
            selection = self._index()
            for iid in self.tree.get_children():
                self.tree.delete(iid)
            state = self.backend.load_scheduler_state()
            for kind in TYPES:
                grouped = [(idx, j) for idx, j in enumerate(self.jobs) if self.backend.job_type(j) == kind]
                if not grouped:
                    continue
                node = self.tree.insert("", "end", iid=f"group_{kind}", text=SHORT_TYPES[kind], open=True,
                                        values=("", "", "", ""))
                for idx, job in grouped:
                    entry = self.backend.get_db_state(job, state)
                    result = entry.get("last_result", "")
                    tags = ("disabled",) if not job.get("enabled", True) else (("error",) if result == "error" else ("success",) if result == "ok" else ())
                    self.tree.insert(node, "end", iid=f"job_{idx}", text=job.get("name") or job.get("ref"),
                                     values=(TYPES[kind], job.get("schedule_time") or "вручную",
                                             self.backend.format_ui_time(entry.get("last_success_time", "")),
                                             "Ошибка" if result == "error" else "Успешно" if result == "ok" else "—"),
                                     tags=tags)
            if selection is not None and selection < len(self.jobs):
                self.tree.selection_set(f"job_{selection}")
            count = len(self.jobs)
            enabled = sum(bool(j.get("enabled", True)) for j in self.jobs)
            self.summary.set(f"Заданий: {count}    •    Включено: {enabled}")
            self.refresh_service_status()
            self.refresh_archives()

        def refresh_archives(self):
            try:
                for iid in self.archives.get_children():
                    self.archives.delete(iid)
                root = Path(self.cfg.get("backup_root", ""))
                if not root.exists():
                    return
                files = sorted((x for x in root.glob("**/*") if x.is_file() and x.suffix.lower() in {".dt", ".bak", ".zip", ".7z"}),
                               key=lambda p: p.stat().st_mtime, reverse=True)[:200]
                for p in files:
                    self.archives.insert("", "end", values=(str(p.relative_to(root)),
                                 f"{p.stat().st_size / 1048576:.2f}", datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M")))
            except OSError:
                pass

        def refresh_journal(self):
            state = self.backend.load_scheduler_state()
            for iid in self.journal.get_children():
                self.journal.delete(iid)
            rows = []
            for job in self.jobs:
                entry = self.backend.get_db_state(job, state)
                stamp = entry.get("last_attempt_time", "")
                if not stamp:
                    continue
                status = entry.get("last_result", "")
                msg = entry.get("last_error", "") if status == "error" else "Резервное копирование выполнено"
                rows.append((stamp, job.get("name", ""), "Ошибка" if status == "error" else "Успешно", msg))
            for stamp, name, result, details in sorted(rows, reverse=True):
                self.journal.insert("", "end", values=(stamp, name, result, details), tags=("error",) if result == "Ошибка" else ("success",))

        def refresh_service_status(self):
            if not self.backend.SERVICE_AVAILABLE:
                self.service_status.set("Служба: pywin32 недоступен")
                return
            try:
                v = self.backend.win32serviceutil.QueryServiceStatus("OneCBackupManager")[1]
                self.service_status.set("Служба: " + {
                    self.backend.win32service.SERVICE_RUNNING: "Работает",
                    self.backend.win32service.SERVICE_STOPPED: "Остановлена",
                    self.backend.win32service.SERVICE_START_PENDING: "Запускается",
                    self.backend.win32service.SERVICE_STOP_PENDING: "Останавливается",
                }.get(v, f"{v}"))
            except Exception:
                self.service_status.set("Служба: Не установлена")

        def periodic(self):
            self.refresh_jobs()
            self.refresh_journal()
            self.after(5000, self.periodic)

        def _log(self, line):
            self.q.put(("log", str(line)))

        def poll(self):
            try:
                while True:
                    kind, data = self.q.get_nowait()
                    if kind == "log":
                        self.rawlog.configure(state="normal")
                        self.rawlog.insert("end", data + "\n")
                        self.rawlog.see("end")
                        self.rawlog.configure(state="disabled")
                        self.active_text.configure(state="normal")
                        self.active_text.insert("end", data + "\n")
                        self.active_text.see("end")
                        self.active_text.configure(state="disabled")
                    elif kind in ("done", "error"):
                        self.worker = None
                        self.refresh_jobs()
                        self.refresh_journal()
                        if kind == "done":
                            messagebox.showinfo("Резервное копирование", data)
                        else:
                            messagebox.showerror("Резервное копирование", data)
            except queue.Empty:
                pass
            self.after(180, self.poll)

        def persist(self, cfg=None, sec=None):
            if self.secrets_error:
                raise RuntimeError("Невозможно сохранить: secrets.dat не удалось расшифровать.")
            cfg = deepcopy(cfg if cfg is not None else self.cfg)
            cfg["databases"] = [self.backend.normalize_db(job) for job in self.jobs]
            sec = deepcopy(sec if sec is not None else self.sec)
            self.backend.save_secrets(sec.get("cluster_password", ""), sec.get("db_passwords", {}),
                                      sec.get("sql_passwords", {}), sec.get("ftp_password", ""))
            self.backend.save_config(cfg)
            self.cfg, self.sec = cfg, sec
            self.refresh_jobs()

        def new_job_menu(self):
            menu = tk.Menu(self, tearoff=0)
            for k, label in SHORT_TYPES.items():
                menu.add_command(label=label, command=lambda v=k: self.new_job(v))
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

        def new_job(self, kind):
            i = len(self.jobs) + 1
            job = self.backend.normalize_db({
                "id": uuid.uuid4().hex,
                "type": kind,
                "name": f"Новое задание {i}",
                "ref": f"{kind}_{i}",
                "ib_ref": f"{kind}_{i}" if kind == "1c" else "",
                "one_c_server": self.cfg.get("server", ""),
                "connection_kind": "server",
                "enabled": False,
                "archive_format": "zip" if kind == "files" else "none",
                "sql_server": ".",
                "sql_driver": "ODBC Driver 18 for SQL Server",
                "keep": 7,
            })
            TaskEditor(self, job, lambda updated, passwd, sqlpass: self.apply_job(updated, passwd, sqlpass, None))

        def edit_job(self):
            idx = self._index()
            if idx is None:
                return
            TaskEditor(self, self.jobs[idx], lambda updated, passwd, sqlpass: self.apply_job(updated, passwd, sqlpass, idx))

        def apply_job(self, job, passwd, sqlpass, index):
            key = self.backend.job_key(job)
            if any(str(other.get("ref", "")).casefold() == str(job["ref"]).casefold() and i != index
                   for i, other in enumerate(self.jobs)):
                raise ValueError("Идентификатор Ref уже используется другой задачей.")
            draft = deepcopy(self.jobs)
            if index is None:
                draft.append(job)
            else:
                draft[index] = job
            saved = self.jobs
            saved_sec = deepcopy(self.sec)
            try:
                self.jobs = draft
                if job.get("type") == "1c":
                    self.sec.setdefault("db_passwords", {})[key] = passwd
                elif job.get("type") == "mssql":
                    self.sec.setdefault("sql_passwords", {})[key] = sqlpass
                self.persist()
            except Exception:
                self.jobs = saved
                self.sec = saved_sec
                raise
            self.tree.selection_set(f"job_{index if index is not None else len(self.jobs) - 1}")

        def clear_jobs(self):
            if not self.jobs:
                return
            if not messagebox.askyesno(
                "Очистка списка заданий",
                "Удалить из программы все задания?\n\nФайлы резервных копий на диске НЕ удаляются.\n"
                "Для сохранности желательно сначала скопировать config.json и secrets.dat."
            ):
                return
            saved = self.jobs
            try:
                self.jobs = []
                self.persist()
                self.refresh_journal()
            except Exception as exc:
                self.jobs = saved
                messagebox.showerror("Очистка списка", str(exc))

        def import_bases(self):
            InfobaseImportDialog(self, on_import=self.apply_import)

        def apply_import(self, selected: list[Infobase]):
            draft = deepcopy(self.jobs)
            added = 0
            skipped = 0
            for item in selected:
                if any(j.get("type", "1c") == "1c" and
                       j.get("connection_kind", "server") == item.kind and
                       ((item.kind == "server" and str(j.get("one_c_server") or self.cfg.get("server", "")).casefold() == item.server.casefold() and
                         str(j.get("ib_ref") or j.get("ref", "")).casefold() == item.ref.casefold()) or
                        (item.kind == "file" and str(j.get("one_c_file", "")).casefold() == item.path.casefold()) or
                        (item.kind == "web" and str(j.get("one_c_url", "")).casefold() == item.url.casefold()))
                       for j in draft):
                    skipped += 1
                    continue
                draft.append(self.backend.normalize_db(item_to_job(item, draft)))
                added += 1
            self.jobs = draft
            self.persist()
            messagebox.showinfo("Импорт из 1С", f"Добавлено: {added}\nУже были в списке: {skipped}\n\n"
                                "Новые задания выключены до проверки подключений.")

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
            self.apply_job(self.backend.normalize_db(clone), password, sqlpass, None)

        def delete_job(self):
            idx = self._index()
            if idx is None:
                return
            if not messagebox.askyesno("Удаление задания", f"Удалить задание «{self.jobs[idx].get('name')}»?\n"
                                     "Локальные архивы при этом останутся на диске."):
                return
            self.jobs.pop(idx)
            self.persist()

        def open_settings(self):
            SettingsDialog(self)

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
            try:
                result = self.backend.run_any_backup(self.cfg, self.sec, job, self._log)
                self.backend.record_backup_result(job, True, scheduled=False)
                return result
            except Exception as exc:
                self.backend.record_backup_result(job, False, error=self.backend.friendly_error_message(exc, str(exc)), scheduled=False)
                raise

        def run_selected(self):
            idx = self._index()
            if idx is None:
                messagebox.showwarning("Выбор задания", "Выберите строку задания, а не группу.")
                return
            job = deepcopy(self.jobs[idx])
            # Manual Run is explicit even if task is disabled in schedule.
            job["enabled"] = True
            self.start_worker(lambda: f"Выполнено: {job['name']}\n{self._backup_job(job)}")

        def run_all(self):
            jobs = [deepcopy(x) for x in self.jobs if x.get("enabled")]
            if not jobs:
                messagebox.showinfo("Задания", "Нет включённых заданий.")
                return
            def worker():
                failures = []
                for job in jobs:
                    try:
                        self._backup_job(job)
                    except Exception as exc:
                        failures.append(f"{job.get('name')}: {exc}")
                if failures:
                    raise RuntimeError("Не все задания выполнены:\n" + "\n".join(failures))
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
                if typ == "1c":
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
                            cfg["ras_endpoint"] = job.get("one_c_ras") or f"{server}:1545"
                            cfg["cluster_uuid"] = ""
                        eng = self.backend.BackupEngine(cfg, self.sec, self._log)
                        try:
                            eng.prepare()
                            clu = eng.get_cluster_uuid()
                            iid, _ = eng.find_infobase(clu, job.get("ib_ref") or job.get("ref"))
                            eng.get_infobase_info(clu, iid, job)
                        finally:
                            eng.close()
                elif typ == "mssql":
                    from backup_extensions import sql_check_connection
                    sql_check_connection(job, self.sec)
                else:
                    paths = [x.strip() for x in job.get("file_paths", "").splitlines() if x.strip()]
                    if not paths:
                        raise RuntimeError("Укажите файлы или папки")
                    for p in paths:
                        if not Path(p).exists():
                            raise RuntimeError(f"Не найден источник: {p}")
                return f"Проверка подключения выполнена: {job['name']}"
            self.start_worker(worker)

        def unblock_all(self):
            if not messagebox.askyesno("Аварийная разблокировка", "Снять блокировки сеансов у включённых баз 1С?"):
                return
            def worker():
                for j in self.jobs:
                    if j.get("type") != "1c" or not j.get("enabled") or j.get("connection_kind") != "server":
                        continue
                    cfg = deepcopy(self.cfg)
                    server = j.get("one_c_server", "")
                    if server:
                        cfg["server"] = server
                    if server and server.casefold() != self.cfg.get("server", "").casefold():
                        cfg["ras_endpoint"] = j.get("one_c_ras") or f"{server}:1545"
                        cfg["cluster_uuid"] = ""
                    eng = self.backend.BackupEngine(cfg, self.sec, self._log)
                    try:
                        eng.prepare()
                        eng.emergency_unblock(j)
                    finally:
                        eng.close()
                return "Блокировки сняты"
            self.start_worker(worker)

        def service_command(self, command):
            if self.secrets_error:
                messagebox.showerror("Служба", "Не расшифрованы пароли DPAPI")
                return
            if not self.backend.SERVICE_AVAILABLE:
                messagebox.showerror("Служба", "pywin32 недоступен. Пересоберите EXE на Windows.")
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
                res = subprocess.run([str(exe), actual], capture_output=True, text=False,
                                     creationflags=self.backend.CREATE_NO_WINDOW)
                out = self.backend.decode_service_output(res.stdout or b"") + "\n" + self.backend.decode_service_output(res.stderr or b"")
                if res.returncode:
                    raise RuntimeError(self.backend.friendly_service_error(out, res.returncode))
                if command == "install":
                    sc = subprocess.run(["sc.exe", "config", "OneCBackupManager", "start=", "auto"],
                                        capture_output=True, text=False, creationflags=self.backend.CREATE_NO_WINDOW)
                    if sc.returncode:
                        raise RuntimeError("Служба обновлена, но не удалось настроить автоматический запуск.")
                self.refresh_service_status()
                messagebox.showinfo("Служба", "Операция выполнена. Для чтения DPAPI-паролей проверьте учётную запись службы.")
            except Exception as exc:
                messagebox.showerror("Служба", str(exc))

    return App


# ===== v4.2 redesigned GUI (closer to Effector Saver workflow) =====

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

    def test(self):
        try:
            profile, pwd = self.data()
            self.master.backend.ftp_check(profile, pwd)
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
                groups[grp] = self.tree.insert("", "end", text=grp, open=True)
            self.tree.insert(groups[grp], "end", iid=kind, text=label)
        self.tree.bind("<Double-1>", lambda e: self.confirm())
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Создать", command=self.confirm).pack(side="right")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=8)

    def confirm(self):
        sel = self.tree.selection()
        if not sel:
            return
        kind = sel[0]
        if kind not in {"1c", "mssql", "files"}:
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
        self.geometry("980x700")
        self.minsize(860, 620)
        self.transient(owner)
        self.grab_set()
        self.v = {}
        self._build_variables()
        self._build()

    def _build_variables(self):
        def sv(key, fallback=""):
            self.v[key] = tk.StringVar(value=str(self.job.get(key, fallback)))
        def bv(key, fallback=False):
            self.v[key] = tk.BooleanVar(value=bool(self.job.get(key, fallback)))
        for field in ["name", "ref", "ib_ref", "ib_user", "one_c_server", "one_c_file", "one_c_url", "one_c_ras",
                      "one_c_exe", "sql_server", "sql_database", "sql_backup_dir", "sql_driver", "sql_user",
                      "folder", "keep", "archive_format", "platform_mode", "ftp_profile"]:
            sv(field)
        for field in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth", "sql_trust_cert", "sql_remove_stage",
                      "stop_after_enabled"]:
            bv(field)
        self.v["ib_password"] = tk.StringVar(value=self.owner.sec.get("db_passwords", {}).get(
            self.backend.job_key(self.job), self.owner.sec.get("db_passwords", {}).get(self.job.get("ref", ""), "")))
        self.v["sql_password"] = tk.StringVar(value=self.owner.sec.get("sql_passwords", {}).get(self.backend.job_key(self.job), ""))
        self.v["connection_kind"] = tk.StringVar(value=self.job.get("connection_kind", "server"))
        self.v["command_before"] = tk.StringVar(value=str(self.job.get("command_before", "")))
        self.v["command_after"] = tk.StringVar(value=str(self.job.get("command_after", "")))
        self.v["stop_after_hours"] = tk.StringVar(value=str(self.job.get("stop_after_hours", 24)))
        self.v["stop_after_minutes"] = tk.StringVar(value=str(self.job.get("stop_after_minutes", 0)))
        tm = str(self.job.get("schedule_time", ""))
        h, m = (tm.split(":") + [""])[:2] if ":" in tm else ("", "")
        self.hour = tk.StringVar(value=h)
        self.minute = tk.StringVar(value=m)
        self.kind_display = tk.StringVar(value=SOURCE_DISPLAY.get(self.v["connection_kind"].get(), "Клиент-серверная"))
        self.platform_display = tk.StringVar(value=PLATFORM_DISPLAY.get(self.v["platform_mode"].get() or "inherit", "Из общих параметров"))

    def page_names(self):
        kind = self.job.get("type", "1c")
        if kind == "1c":
            return ["Подключение к базе", "Отключение пользователей", "Хранилище архивов", "Файл архива", "Выполнить команды", "Расписание автозапуска", "Прервать выполнение"]
        if kind == "mssql":
            return ["Основные параметры", "База Microsoft SQL", "Хранилище архивов", "Файл архива", "Выполнить команды", "Расписание автозапуска", "Прервать выполнение"]
        return ["Основные параметры", "Источники файлов", "Хранилище архивов", "Файл архива", "Выполнить команды", "Расписание автозапуска", "Прервать выполнение"]

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        top = ttk.Frame(root)
        top.pack(fill="x")
        ttk.Label(top, text="Вид задачи:").pack(side="left")
        ttk.Label(top, text=("Резервное копирование 1С:Предприятие 8 / BAS" if self.job.get("type") == "1c" else
                             "Резервное копирование файлов и баз данных" if self.job.get("type") == "files" else
                             "Резервное копирование Microsoft SQL Server")).pack(side="left", padx=(6,0))
        ttk.Label(top, text="Наименование:").pack(side="left", padx=(30,6))
        ttk.Entry(top, textvariable=self.v["name"]).pack(side="left", fill="x", expand=True)

        center = ttk.Frame(root)
        center.pack(fill="both", expand=True, pady=(10, 0))
        left = ttk.Frame(center)
        left.pack(side="left", fill="y", padx=(0, 10))
        self.nav = tk.Listbox(left, width=28, exportselection=False)
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
        title_label(page, "Основные параметры")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        add_line(frm, "Идентификатор / Ref", self.v["ref"], 0)
        ttk.Checkbutton(frm, text="Задание активно", variable=self.v["enabled"]).grid(row=1, column=1, sticky="w", pady=8)

    def page_1c_source(self, page):
        title_label(page, "Параметры подключения к информационной базе")
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
        add_line(c, "Кластер серверов", self.v["one_c_server"], 0)
        add_line(c, "Имя базы", self.v["ib_ref"], 1)
        add_line(c, "Пользователь", self.v["ib_user"], 2)
        add_line(c, "Пароль", self.v["ib_password"], 3, password=True)
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
        title_label(page, "Завершение работы пользователей")
        box = ttk.LabelFrame(page, text="Параметры отключения пользователей", padding=10)
        box.pack(fill="x")
        ttk.Label(box, text="Для серверных баз используется проверенный механизм v3/v4:\n"
                            "запрет новых сеансов → ожидание → при необходимости принудительное завершение → DumpIB → разблокировка.",
                  justify="left").pack(anchor="w")
        ttk.Label(box, text="Параметры ожидания, принудительного завершения и RAS задаются в «Сервис → Параметры программы».",
                  foreground="#52677b", justify="left").pack(anchor="w", pady=10)
        ttk.Button(box, text="Открыть параметры программы", command=self.owner.open_settings).pack(anchor="w")

    def page_sql(self, page):
        title_label(page, "Включить в архив backup базы Microsoft SQL")
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
        title_label(page, "Источники файлов и каталогов")
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
        title_label(page, "Укажите, где следует хранить создаваемые архивы")
        local = ttk.LabelFrame(page, text="Локальное хранилище", padding=10)
        local.pack(fill="x")
        add_line(local, "Папка хранения", self.v["folder"], 0)
        ttk.Button(local, text="...", width=3, command=lambda: self.browse_dir("folder")).grid(row=0, column=2, padx=5)
        add_line(local, "Хранить копий", self.v["keep"], 1)
        ftp = ttk.LabelFrame(page, text="FTP / FTPS", padding=10)
        ftp.pack(fill="x", pady=(12,0))
        ttk.Checkbutton(ftp, text="Отправлять архив на FTP / FTPS", variable=self.v["ftp_enabled"]).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(ftp, text="Хранилище:").grid(row=1, column=0, sticky="w", pady=5)
        self.ftp_combo = ttk.Combobox(ftp, textvariable=self.v["ftp_profile"], state="readonly")
        self.ftp_combo.grid(row=1, column=1, sticky="w", pady=5)
        ttk.Button(ftp, text="Управление...", command=self.manage_profiles).grid(row=1, column=2, padx=5)
        ttk.Label(ftp, text="Если профиль не выбран, используется старое общее FTP-хранилище из параметров программы.", foreground="#52677b").grid(row=2, column=0, columnspan=3, sticky="w", pady=6)
        self.refresh_profiles_combo()

    def page_archive(self, page):
        title_label(page, "Параметры файла архива")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        ttk.Label(frm, text="Окончание имени архива:").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Label(frm, text="yyyy-mm-dd_hh-mm-ss", foreground="#52677b").grid(row=0, column=1, sticky="w", pady=5)
        ttk.Label(frm, text="Формат:").grid(row=1, column=0, sticky="w", pady=5)
        values = ["none", "zip", "7z"] if self.job.get("type") != "files" else ["zip", "7z"]
        ttk.Combobox(frm, textvariable=self.v["archive_format"], values=values, state="readonly", width=20).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Checkbutton(frm, text="Удалять исходный .dt / .bak после успешной упаковки", variable=self.v["remove_raw_after_pack"]).grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(frm, text="Для 1С и MS SQL доступно: none (без упаковки), zip и 7z. Для файлов — zip и 7z.", foreground="#52677b").grid(row=3, column=0, columnspan=2, sticky="w", pady=6)

    def page_commands(self, page):
        title_label(page, "Выполнить команды системы")
        ttk.Label(page, text="Выполнить команды перед выполнением задачи").pack(anchor="w")
        self.cmd_before = tk.Text(page, height=8)
        self.cmd_before.pack(fill="x", pady=(4,10))
        self.cmd_before.insert("1.0", self.v["command_before"].get())
        ttk.Label(page, text="Выполнить команды после выполнения задачи").pack(anchor="w")
        self.cmd_after = tk.Text(page, height=8)
        self.cmd_after.pack(fill="x", pady=(4,10))
        self.cmd_after.insert("1.0", self.v["command_after"].get())
        ttk.Label(page, text="Команды сохраняются в задаче. Интеграцию их запуска можно включить следующим шагом, если нужно.", foreground="#52677b").pack(anchor="w")

    def page_schedule(self, page):
        title_label(page, "Расписание")
        panel = ttk.Frame(page)
        panel.pack(fill="x")
        ttk.Checkbutton(panel, text="Запускать по расписанию", variable=self.v["enabled"]).pack(anchor="w", pady=6)
        ttk.Label(panel, text="Периодичность:").pack(anchor="w")
        ttk.Combobox(panel, values=["Ежедневно"], state="readonly", width=20).pack(anchor="w", pady=5)
        ttk.Label(panel, text="Время начала:").pack(anchor="w", pady=(8,0))
        spin_time(panel, self.hour, self.minute).pack(anchor="w", pady=5)

    def page_timeout(self, page):
        title_label(page, "Принудительное завершение работы задачи")
        frm = ttk.Frame(page)
        frm.pack(fill="x")
        ttk.Checkbutton(frm, text="Прервать выполнение задачи через", variable=self.v["stop_after_enabled"]).grid(row=0, column=0, columnspan=4, sticky="w", pady=5)
        ttk.Spinbox(frm, from_=0, to=999, textvariable=self.v["stop_after_hours"], width=6).grid(row=1, column=0, sticky="w")
        ttk.Label(frm, text="час.").grid(row=1, column=1, sticky="w", padx=(4,18))
        ttk.Spinbox(frm, from_=0, to=59, textvariable=self.v["stop_after_minutes"], width=6).grid(row=1, column=2, sticky="w")
        ttk.Label(frm, text="мин.").grid(row=1, column=3, sticky="w", padx=(4,0))
        ttk.Label(frm, text="Параметр сохраняется в конфигурации задачи.", foreground="#52677b").grid(row=2, column=0, columnspan=4, sticky="w", pady=8)

    def manage_profiles(self):
        StorageProfilesDialog(self.owner)
        self.wait_window(self.owner.winfo_children()[-1])
        self.refresh_profiles_combo()

    def refresh_profiles_combo(self):
        vals = [p.get("name", "") for p in self.owner.cfg.get("ftp_profiles", []) if p.get("name")]
        self.ftp_combo.configure(values=vals)
        if self.v["ftp_profile"].get() and self.v["ftp_profile"].get() not in vals:
            self.v["ftp_profile"].set("")

    def change_source_kind(self):
        self.v["connection_kind"].set(next((key for key, label in SOURCE_DISPLAY.items() if label == self.kind_display.get()), "server"))
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
        self.kind_display.set(SOURCE_DISPLAY[item.kind])
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
                    "folder", "archive_format", "platform_mode", "ftp_profile"]:
            job[key] = self.v[key].get().strip()
        for field in ["enabled", "ftp_enabled", "remove_raw_after_pack", "sql_windows_auth", "sql_trust_cert", "sql_remove_stage", "stop_after_enabled"]:
            job[field] = bool(self.v[field].get())
        job["keep"] = max(1, int(self.v["keep"].get() or 7))
        job["connection_kind"] = self.v["connection_kind"].get()
        job["platform_mode"] = next((key for key, label in PLATFORM_DISPLAY.items() if label == self.platform_display.get()), "inherit")
        if self.job.get("type") == "files":
            job["file_paths"] = self.filebox.get("1.0", "end-1c").strip()
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
        if job.get("type") == "1c":
            if job.get("connection_kind") == "server":
                if not (job.get("one_c_server") and job.get("ib_ref")):
                    raise ValueError("Для серверной базы укажите сервер и имя базы.")
            elif job.get("connection_kind") == "file":
                if not job.get("one_c_file"):
                    raise ValueError("Укажите каталог файловой базы.")
            else:
                job["enabled"] = False
        elif job.get("type") == "mssql":
            if not job.get("sql_server") or not job.get("sql_database"):
                raise ValueError("Для MS SQL укажите сервер и базу.")
        else:
            if not job.get("file_paths"):
                raise ValueError("Добавьте хотя бы один файл или каталог.")
        return self.backend.normalize_db(job)

    def save(self):
        try:
            job = self.extract()
            self.on_save(job, self.v["ib_password"].get(), self.v["sql_password"].get())
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Задача", str(exc), parent=self)


def create_app_class(backend):
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.backend = backend
            self.title("1C Backup Manager v4.2")
            self.geometry("1280x860")
            self.minsize(1040, 680)
            self.style()
            self.cfg = backend.load_config()
            self.cfg.setdefault("ftp_profiles", [])
            self.secrets_error = None
            try:
                self.sec = backend.load_secrets()
            except Exception:
                self.secrets_error = traceback.format_exc()
                self.sec = {"cluster_password": "", "db_passwords": {}, "sql_passwords": {}, "ftp_password": "", "ftp_profile_passwords": {}}
            self.jobs = [backend.normalize_db(j) for j in self.cfg.get("databases", [])]
            self.worker = None
            self.q = queue.Queue()
            self.summary = tk.StringVar()
            self.service_status = tk.StringVar(value="Служба: Проверка...")
            self._build()
            self.refresh_jobs()
            self.refresh_journal()
            self.after(200, self.poll)
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
            svc.add_command(label="Очистить список заданий...", command=self.clear_jobs)
            svc.add_separator()
            svc.add_command(label="Выход", command=self.destroy)
            menus.add_cascade(label="Сервис", menu=svc)
            jobs = tk.Menu(menus, tearoff=0)
            jobs.add_command(label="Создать...", command=self.new_job_menu)
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

            top = ttk.Frame(self, padding=10)
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
            self.tree.column("#0", width=350, stretch=True)
            self.tree.column("kind", width=360, stretch=True)
            self.tree.pack(side="left", fill="both", expand=True)
            s = ttk.Scrollbar(upper, orient="vertical", command=self.tree.yview)
            s.pack(side="right", fill="y")
            self.tree.configure(yscrollcommand=s.set)
            self.tree.bind("<Double-1>", lambda e: self.edit_job())
            tools = ttk.Frame(page_tasks)
            tools.pack(fill="x", pady=(6, 0))
            ttk.Button(tools, text="Создать", command=self.new_job_menu).pack(side="left", padx=2)
            ttk.Button(tools, text="Изменить", command=self.edit_job).pack(side="left", padx=2)
            ttk.Button(tools, text="Удалить", command=self.delete_job).pack(side="left", padx=2)
            ttk.Button(tools, text="Выполнить", command=self.run_selected).pack(side="left", padx=12)
            ttk.Button(tools, text="Проверить", command=self.test_selected).pack(side="left", padx=2)
            ttk.Button(tools, text="Хранилища", command=self.open_profiles).pack(side="right", padx=2)

            ttk.Label(page_active, text="Текущие и последние операции", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(6, 6), padx=6)
            self.active_text = tk.Text(page_active, font=("Consolas", 9), state="disabled")
            self.active_text.pack(fill="both", expand=True, padx=6, pady=4)

            lower = ttk.Notebook(top)
            lower.pack(fill="both", expand=True, pady=(6, 0))
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
            self.archives = ttk.Treeview(backups_page, columns=("name", "size", "modified"), show="headings")
            for col, title, width in [("name", "Файл", 600), ("size", "Размер, МБ", 110), ("modified", "Дата", 180)]:
                self.archives.heading(col, text=title)
                self.archives.column(col, width=width, stretch=col == "name")
            self.archives.pack(fill="both", expand=True)
            self.rawlog = tk.Text(raw_page, font=("Consolas", 9), state="disabled")
            self.rawlog.pack(fill="both", expand=True)

            status = ttk.Frame(self, padding=(10, 2))
            status.pack(fill="x")
            ttk.Label(status, textvariable=self.summary).pack(side="left")
            ttk.Label(status, textvariable=self.service_status).pack(side="left", padx=20)
            ttk.Label(status, text="Агент запущен", foreground="#52677b").pack(side="left", padx=20)

        def _index(self):
            sel = self.tree.selection()
            if not sel or not sel[0].startswith("job_"):
                return None
            return int(sel[0][4:])

        def refresh_jobs(self):
            current = self._index()
            self.tree.delete(*self.tree.get_children())
            root = self.tree.insert("", "end", iid="root", text="\\", open=True, values=("",))
            for idx, job in enumerate(self.jobs):
                if job.get("type") == "1c":
                    kind = "Резервное копирование 1С:Предприятие 8 / BAS"
                elif job.get("type") == "mssql":
                    kind = "Резервное копирование файлов и баз данных"
                else:
                    kind = "Резервное копирование файлов и баз данных"
                self.tree.insert(root, "end", iid=f"job_{idx}", text=job.get("name") or job.get("ref"), values=(kind,))
            if current is not None and current < len(self.jobs):
                self.tree.selection_set(f"job_{current}")
            enabled = sum(1 for x in self.jobs if x.get("enabled"))
            self.summary.set(f"Всего заданий: {len(self.jobs)}   •   Включено: {enabled}")
            self.refresh_archives()
            self.refresh_service_status()

        def refresh_archives(self):
            self.archives.delete(*self.archives.get_children())
            root = Path(self.cfg.get("backup_root", ""))
            if not root.exists():
                return
            files = sorted((x for x in root.glob("**/*") if x.is_file() and x.suffix.lower() in {".dt", ".bak", ".zip", ".7z"}), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
            for p in files:
                self.archives.insert("", "end", values=(str(p.relative_to(root)), f"{p.stat().st_size/1048576:.2f}", datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S")))

        def refresh_journal(self):
            self.journal.delete(*self.journal.get_children())
            state = self.backend.load_scheduler_state()
            rows = []
            for job in self.jobs:
                entry = self.backend.get_db_state(job, state)
                start = entry.get("last_attempt_time", "")
                if not start:
                    continue
                end = entry.get("last_success_time", start)
                result = entry.get("last_error", "") if entry.get("last_result") == "error" else f"Успешное выполнение задачи: {job.get('name')}"
                rows.append((start, end, result))
            for row in sorted(rows, reverse=True):
                self.journal.insert("", "end", values=row)

        def refresh_service_status(self):
            if not self.backend.SERVICE_AVAILABLE:
                self.service_status.set("Служба: pywin32 недоступен")
                return
            try:
                v = self.backend.win32serviceutil.QueryServiceStatus("OneCBackupManager")[1]
                self.service_status.set("Служба: " + {self.backend.win32service.SERVICE_RUNNING: "Работает", self.backend.win32service.SERVICE_STOPPED: "Остановлена"}.get(v, str(v)))
            except Exception:
                self.service_status.set("Служба: Не установлена")

        def periodic(self):
            self.refresh_jobs(); self.refresh_journal(); self.after(5000, self.periodic)

        def _log(self, line):
            self.q.put(("log", str(line)))

        def poll(self):
            try:
                while True:
                    kind, data = self.q.get_nowait()
                    if kind == "log":
                        for widget in (self.rawlog, self.active_text):
                            widget.configure(state="normal")
                            widget.insert("end", data + "\n")
                            widget.see("end")
                            widget.configure(state="disabled")
                    elif kind in ("done", "error"):
                        self.worker = None
                        self.refresh_jobs(); self.refresh_journal()
                        if kind == "done":
                            messagebox.showinfo("Резервное копирование", data)
                        else:
                            messagebox.showerror("Резервное копирование", data)
            except queue.Empty:
                pass
            self.after(200, self.poll)

        def persist(self, cfg=None, sec=None):
            if self.secrets_error:
                raise RuntimeError("Невозможно сохранить: secrets.dat не расшифрован.")
            cfg = deepcopy(cfg if cfg is not None else self.cfg)
            cfg.setdefault("ftp_profiles", [])
            cfg["databases"] = [self.backend.normalize_db(job) for job in self.jobs]
            sec = deepcopy(sec if sec is not None else self.sec)
            self.backend.save_secrets(sec.get("cluster_password", ""), sec.get("db_passwords", {}), sec.get("sql_passwords", {}), sec.get("ftp_password", ""), sec.get("ftp_profile_passwords", {}))
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
                "ib_ref": f"job_{i}", "one_c_server": self.cfg.get("server", ""), "connection_kind": "server",
                "enabled": False, "archive_format": "zip" if kind == "files" else "none", "sql_server": ".",
                "sql_driver": "ODBC Driver 18 for SQL Server", "keep": 7,
            })
            EffectorTaskEditor(self, job, lambda updated, passwd, sqlpass: self.apply_job(updated, passwd, sqlpass, None))

        def edit_job(self):
            idx = self._index()
            if idx is None:
                return
            EffectorTaskEditor(self, self.jobs[idx], lambda updated, passwd, sqlpass: self.apply_job(updated, passwd, sqlpass, idx))

        def apply_job(self, job, passwd, sqlpass, index):
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
                if job.get("type") == "1c":
                    self.sec.setdefault("db_passwords", {})[key] = passwd
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
            self.apply_job(self.backend.normalize_db(clone), password, sqlpass, None)

        def delete_job(self):
            idx = self._index()
            if idx is None:
                return
            if not messagebox.askyesno("Удаление", f"Удалить задание «{self.jobs[idx].get('name')}»?"):
                return
            self.jobs.pop(idx)
            self.persist()

        def open_settings(self):
            SettingsDialog(self)

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
            try:
                result = self.backend.run_any_backup(self.cfg, self.sec, job, self._log)
                self.backend.record_backup_result(job, True, scheduled=False)
                return result
            except Exception as exc:
                self.backend.record_backup_result(job, False, error=self.backend.friendly_error_message(exc, str(exc)), scheduled=False)
                raise

        def run_selected(self):
            idx = self._index()
            if idx is None:
                return
            job = deepcopy(self.jobs[idx]); job["enabled"] = True
            self.start_worker(lambda: f"Выполнено: {job['name']}\n{self._backup_job(job)}")

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
                if typ == "1c":
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
                            cfg["ras_endpoint"] = job.get("one_c_ras") or f"{server}:1545"
                            cfg["cluster_uuid"] = ""
                        eng = self.backend.BackupEngine(cfg, self.sec, self._log)
                        try:
                            eng.prepare(); clu = eng.get_cluster_uuid(); iid, _ = eng.find_infobase(clu, job.get("ib_ref") or job.get("ref")); eng.get_infobase_info(clu, iid, job)
                        finally:
                            eng.close()
                elif typ == "mssql":
                    from backup_extensions import sql_check_connection
                    sql_check_connection(job, self.sec)
                else:
                    paths = [x.strip() for x in job.get("file_paths", "").splitlines() if x.strip()]
                    if not paths:
                        raise RuntimeError("Укажите файлы или папки")
                    for p in paths:
                        if not Path(p).exists():
                            raise RuntimeError(f"Не найден источник: {p}")
                return f"Проверка подключения выполнена: {job['name']}"
            self.start_worker(worker)

    return App
