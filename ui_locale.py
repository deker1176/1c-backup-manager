"""Lightweight RU/EN localization for the Tk user interface.

No database names, filesystem paths, passwords or job keys are translated.
Russian texts remain canonical for existing configuration compatibility.
"""
from __future__ import annotations
import re
import tkinter as tk
from tkinter import ttk

RU_EN = {
    "Отчеты — почта, Telegram и MAX": "Reports — email, Telegram and MAX",
    "Настройка SMTP, Telegram и MAX выполняется в меню Сервис → Параметры программы → Отчеты.":
        "Configure SMTP, Telegram and MAX under Service → Program settings → Reports.",
    "Отправлять отчет в MAX": "Send report to MAX",
    "Тип ID:": "ID type:",
    "ID получателя MAX": "MAX recipient ID",
    "Bot Token MAX": "MAX Bot Token",
    "Тест MAX": "Test MAX",
    "chat_id — чат/канал, user_id — личное сообщение. ID — числовой. Бот должен иметь доступ к получателю.":
        "chat_id — group/channel, user_id — direct message. ID must be numeric and the bot must have access.",

    "Русский": "Russian", "Английский": "English", "Язык программы": "Program language",
    "Новый язык применяется после повторного открытия программы.": "Restart the application to apply the new language.",
    "Агент запущен": "Agent running", "Агент остановлен": "Agent stopped",
    "Агент не установлен": "Agent not installed", "Агент: проверка...": "Agent: checking...",
    "Агент: нет доступа": "Agent: access denied", "Агент: ошибка проверки": "Agent: check error",
    "Служба: Не установлена": "Service: not installed", "Служба: Недоступна": "Service: unavailable",
    "Служба: не установлена": "Service: not installed",
    "Параметры программы...": "Program settings...", "Управление хранилищами...": "Manage storages...",
    "Импорт баз 1С...": "Import 1C infobases...", "Очистить список заданий...": "Clear task list...",
    "Создать...": "New...", "Создать папку...": "New folder...",
    "Переименовать папку...": "Rename folder...", "Удалить папку...": "Delete folder...",
    "Переместить в папку...": "Move to folder...", "Выход": "Exit", "Все активные": "Run all enabled",
    "Повторить отправку на FTP": "Retry FTP upload", "Обновить журнал": "Refresh log",
    "Свернуть в трей": "Minimize to tray", "Текущие и последние операции": "Current and recent operations",
    "Название": "Name", "Профиль": "Profile", "Имя файла": "File name",
    "Программа будет перезапущена": "The application will restart",
    "Автозапуск": "Autostart", "Параметры агента": "Agent settings",
    "Параметры менеджера": "Manager settings", "Файлы архивов": "Backup files",
    "Журнал": "Log", "Служебные": "Maintenance", "Дополнительно": "Advanced",
    "Отчеты": "Reports", "Отчёты": "Reports", "Параметры программы": "Program settings",
    "Параметры системы": "System settings", "Дополнительные параметры": "Additional settings",
    "Корневая папка копий": "Backup root directory", "Корневая папка копий:": "Backup root directory:",
    "Формат ZIP/7z и количество копий настраиваются отдельно у каждой задачи.":
       "ZIP/7z format and retention are configured for each task.",
    "Выберите язык интерфейса:": "Interface language:",
    "Параметры подключения к информационной базе": "Infobase connection settings",
    "Выбрать информационную базу из списка и заполнить основные параметры": "Choose an infobase from the 1C launcher",
    "Идентификатор / Ref": "Identifier / Ref", "Исполняемый файл:": "Executable:",
    "1cv8.exe (ручной режим)": "1cv8.exe (manual)", "Вариант базы:": "Database mode:",
    "Сервер 1С": "1C server", "Имя базы": "Infobase name",
    "Пользователь": "Username", "Пароль": "Password", "Клиент-серверная": "Client/server",
    "Файловая": "File-based", "Веб-подключение": "Web connection",
    "Клиент-серверная база": "Client/server infobase", "Каталог базы": "Infobase directory",
    "Файловая база": "File-based infobase", "URL": "URL",
    "Кластер серверов": "1C server", "Подключение к базе": "Connect to database",
    "Отключение пользователей": "Disconnect users", "Хранилище архивов": "Backup storage",
    "Файл архива": "Archive file", "Выполнить команды": "Run commands",
    "Расписание автозапуска": "Schedule", "Прервать выполнение": "Stop execution",
    "Отправка отчетов": "Send reports", "Тестирование": "Database check",
    "Выполнение процедуры": "Run procedure", "Основные параметры": "General",
    "База Microsoft SQL": "Microsoft SQL database", "Источники файлов": "Files and folders",
    "Произвольные команды": "Custom commands", "Параметры отключения пользователей": "User session controls",
    "Завершение работы пользователей": "End user sessions",
    "Ждать выхода пользователей, сек.": "Wait for users, seconds",
    "Принудительно завершать сеансы": "Force logoff remaining sessions",
    "Администратор кластера (не ИБ)": "Cluster admin (not infobase user)",
    "Пароль администратора кластера": "Cluster admin password",
    "Пустые значения используют общие параметры. Пароль кластера шифруется Windows DPAPI.":
      "Empty values use existing defaults. Passwords are protected by Windows DPAPI.",
    "Из общих параметров": "From saved settings", "Автоматически (последняя)": "Auto (latest version)",
    "Указать 1cv8.exe": "Select 1cv8.exe", "По умолчанию": "Default",
    "Да": "Yes", "Нет": "No", "Ежедневно": "Daily",
    "Выполнение обмена": "Exchange", "Внешняя обработка": "External processing",
    "Вид задачи:": "Task type:", "Наименование:": "Name:", "Задача": "Task",
    "Новое задание": "New task", "Новое задание 1": "New task 1",
    "Создать": "Create", "Удалить": "Delete", "Изменить": "Edit", "Копировать": "Duplicate",
    "Выполнить": "Run", "Проверить": "Check", "Сохранить": "Save", "Отмена": "Cancel",
    "Настройка задач": "Task setup", "Активные задачи": "Active tasks",
    "Журнал задач": "Task log", "Бэкапы": "Backups", "Тех. лог": "Technical log",
    "Наименование": "Name", "Вид задачи": "Task type", "Вкл": "On", "Периодичность": "Frequency",
    "Начало": "Start", "Конец": "Finish", "Результат": "Result",
    "Дата": "Date", "Размер": "Size", "Файл": "File", "Файл:": "File:",
    "Сервис": "Service", "Задачи": "Tasks", "Помощь": "Help",
    "Хранилища": "Storages", "Папка": "Folder", "Хранить копий": "Keep copies",
    "Хранилище:": "Storage:", "Управление...": "Manage...", "Обзор...": "Browse...",
    "Папка хранения": "Storage directory", "Задание активно": "Task enabled",
    "Запускать по расписанию": "Run on schedule", "Периодичность:": "Recurrence:",
    "Время начала:": "Start time:", "Очистить": "Clear",
    "Параметры тестирования и исправления 1С 8": "1C 8 check and repair settings",
    "По умолчанию выполняется только проверка без исправлений.": "Default: check only, no repairs.",
    "Реиндексация таблиц": "Reindex tables", "Проверка логической целостности": "Check logical integrity",
    "Проверка ссылочной целостности": "Check referential integrity", "Пересчёт итогов": "Recalculate totals",
    "Сжатие таблиц": "Compact tables", "Реструктуризация таблиц": "Restructure tables",
    "Режим выполнения": "Execution mode", "Только тестирование (без изменений)": "Check only (no changes)",
    "Тестирование и исправление": "Check and repair", "Только тестирование": "Check only",
    "Вызов процедуры 1С:Предприятие 8 / BAS": "Run 1C 8 / BAS procedure",
    "Тестирование и исправление 1С:Предприятие 8 / BAS": "1C 8 / BAS check and repair",
    "Резервное копирование 1С:Предприятие 8 / BAS": "1C 8 / BAS backup",
    "Резервное копирование Microsoft SQL Server": "Microsoft SQL Server backup",
    "Резервное копирование файлов и баз данных": "Files and database backup",
    "Дополнительное копирование": "Additional copy", "Произвольные команды": "Custom commands",
    "Выберите тип новой задачи": "Choose new task type", "Резервное копирование": "Backup",
    "Дополнительно 1С:Предприятие": "Additional 1C tasks", "Другие задачи": "Other tasks",
    "Отчеты — почта и Telegram": "Reports — email and Telegram",
    "Электронная почта (SMTP)": "Email (SMTP)", "SMTP сервер": "SMTP server",
    "Порт": "Port", "Логин SMTP": "SMTP username", "Отправитель": "From",
    "Получатель (через запятую)": "Recipients (comma-separated)",
    "Пароль SMTP": "SMTP password", "Защита соединения": "Connection security",
    "Тест письма": "Test email", "Тест Telegram": "Test Telegram",
    "Опрос расписания, сек.": "Schedule polling, seconds",
    "Ожидание выхода пользователей 1С, сек.": "Wait for 1C users, seconds",
    "Принудительно завершать оставшиеся сеансы 1С": "Disconnect remaining 1C sessions",
    "Действие при закрытии окна:": "Action when closing:",
    "Спрашивать при закрытии": "Ask on close", "Сворачивать в трей": "Minimize to tray",
    "Закрывать интерфейс": "Exit interface",
    "Сворачивать в трей при нажатии кнопки «Свернуть»": "Minimize to tray when minimizing window",
    "Открыть папку логов в проводнике": "Open log folder",
    "Аварийно снять блокировки 1С": "Emergency unblock 1C",
    "Автоматически удалять записи журнала старше, дней": "Delete log records older than (days)",
    "Запускать как службу Windows (сервер) — рекомендуемый вариант для ночных заданий.":
       "Run as a Windows service (recommended for nightly backups).",
    "Установить / обновить службу": "Install / update service",
    "Запустить службу": "Start service", "Остановить службу": "Stop service",
    "Остановить": "Stop", "Установить / обновить": "Install / update",
    "Удалить службу": "Remove service",
    "Служба": "Windows service", "Агент запущен": "Agent running",
    "Агент остановлен": "Agent stopped", "Агент не установлен": "Agent not installed",
    "Программа": "Program", "Прервать выполнение задачи через": "Stop task after",
    "час.": "hours", "мин.": "min",
    "FTP / папка для архивов": "FTP backup folder", "Пассивный режим": "Passive mode",
    "FTP/FTPS": "FTP/FTPS", "Адрес сервера": "Server address",
    "Проверка": "Check", "Отправка отчётов": "Report delivery",
    "Параметры": "Settings",
    "Подключение к серверу": "Connect to server",
    "Не найден": "Not found", "Готово": "Done",
}

RU_EN.update({'+ Задача': ' + Task',
 '+ Папка': '+ Folder',
 '+ Создать': '+ Create',
 '+ Файл': '+ File',
 'FTP из прежних настроек': 'FTP from legacy settings',
 'SQL - авторизация': 'SQL authentication',
 'База': 'Database',
 'Версия 1С выбирается автоматически; для отдельного задания можно вручную указать 1cv8.exe.': '1C version '
                                                                                               'is detected '
                                                                                               'automatically; '
                                                                                               'each task '
                                                                                               'can override '
                                                                                               '1cv8.exe '
                                                                                               'manually.',
 'Включено: ': 'Enabled: ',
 'Включить в архив backup базы Microsoft SQL': 'Include Microsoft SQL database backup',
 'Внешние обработки 1С': '1C external processing',
 'Все файлы': 'All files',
 'Всего заданий: ': 'Tasks: ',
 'Выберите каталог': 'Choose a directory',
 'Выбрать': 'Select',
 'Выполнение процедуры 1С 8 через внешнюю обработку': 'Run 1C 8 procedure via external processing',
 'Выполнить команды перед выполнением задачи': 'Run commands before task',
 'Выполнить команды после выполнения задачи': 'Run commands after task',
 'Выполнить команды системы': 'Run system commands',
 'Для безопасности все задания выполняются последовательно, без параллельного DumpIB.': 'For safety, tasks '
                                                                                        'run sequentially; '
                                                                                        'DumpIB is never '
                                                                                        'parallelized.',
 'Для файловой базы: подтверждаю, что все пользователи вышли': 'For file infobase: I confirm that all users '
                                                               'have logged out',
 'Завершить сеансы пользователей перед выполнением (серверная база)': 'Disconnect users before running '
                                                                      '(server infobase)',
 'Закрытие окна не останавливает Windows-службу.': 'Closing the window does not stop the Windows service.',
 'Заполняйте только если администратор кластера действительно задан. Пароль защищён Windows DPAPI.': 'Fill '
                                                                                                     'only '
                                                                                                     'if a '
                                                                                                     'cluster '
                                                                                                     'admin '
                                                                                                     'is '
                                                                                                     'configured. '
                                                                                                     'Passwords '
                                                                                                     'are '
                                                                                                     'protected '
                                                                                                     'by '
                                                                                                     'Windows '
                                                                                                     'DPAPI.',
 'Запретить новые сеансы → дождаться выхода → принудительное завершение при необходимости → DumpIB → разблокировать.': 'Deny '
                                                                                                                       'new '
                                                                                                                       'sessions '
                                                                                                                       '→ '
                                                                                                                       'wait '
                                                                                                                       'for '
                                                                                                                       'users '
                                                                                                                       '→ '
                                                                                                                       'force '
                                                                                                                       'disconnect '
                                                                                                                       'if '
                                                                                                                       'needed '
                                                                                                                       '→ '
                                                                                                                       'DumpIB '
                                                                                                                       '→ '
                                                                                                                       'unblock.',
 'Имя файла архива (пусто — Ref)': 'Archive filename (blank uses Ref)',
 'Источники файлов и каталогов': 'Files and folders to back up',
 'Каждая непустая строка — отдельная команда. Не храните пароли в командной строке.': 'Each non-empty line '
                                                                                      'is a command. Never '
                                                                                      'store passwords in '
                                                                                      'command text.',
 'Каналы отправки': 'Notification channels',
 'Каталог временного .bak': 'Temporary .bak directory',
 'Кластер 1С требует отдельного администратора (дополнительно)': 'Cluster requires a separate administrator '
                                                                 '(advanced)',
 'Когда отправлять': 'When to send',
 'Количество локальных копий': 'Local copies to keep',
 'Локальная папка': 'Local folder',
 'Локальное хранилище требуется для резервной копии.': 'Local storage is required for backups.',
 'Настройка SMTP и Telegram выполняется в меню Сервис → Параметры программы → Отчеты.': 'Configure SMTP and '
                                                                                        'Telegram in Service '
                                                                                        '→ Program settings '
                                                                                        '→ Reports.',
 'Не заданы команды': 'No commands specified',
 'Неверный путь FTP': 'Invalid FTP path',
 'Нет включённых заданий.': 'No enabled tasks.',
 'Новая папка': 'New folder',
 'Ограничение (час / мин):': 'Time limit (hours / min):',
 'Окончание имени архива:': 'Archive filename suffix:',
 'Отправлять отчет в Telegram': 'Send report to Telegram',
 'Отправлять отчет по электронной почте (SMTP)': 'Send report via email (SMTP)',
 'Папка назначения:': 'Destination folder:',
 'Параметр запуска /C': 'Launch parameter /C',
 'Параметр сохраняется в конфигурации задачи.': 'This value is saved in task configuration.',
 'Параметры файла архива': 'Archive file options',
 'Поля задания заполнены корректно.': 'Task fields are valid.',
 'После успешного выполнения задачи': 'When task succeeds',
 'Прервать выполнение задачи через': 'Stop task after',
 'При ошибке выполнения задачи': 'When task fails',
 'Принудительное завершение работы задачи': 'Stop task on timeout',
 'Протокол': 'Protocol',
 'Процедура:': 'Procedure:',
 'Размер, МБ': 'Size, MB',
 'Разрешить выполнение команд перед/после задания': 'Allow pre/post commands',
 'Расписание': 'Schedule',
 'Редактировать': 'Edit',
 'Сервер': 'Server',
 'Сервер указывается в выбранной базе из списка 1С. При ручном создании заполните его самостоятельно.': 'The '
                                                                                                        '1C '
                                                                                                        'server '
                                                                                                        'is '
                                                                                                        'taken '
                                                                                                        'from '
                                                                                                        'the '
                                                                                                        'imported '
                                                                                                        'infobase. '
                                                                                                        'Enter '
                                                                                                        'it '
                                                                                                        'manually '
                                                                                                        'for '
                                                                                                        'a '
                                                                                                        'new '
                                                                                                        'task.',
 'Служба: Проверка...': 'Service: checking...',
 'Соединение и каталог доступны.': 'Connection and directory are accessible.',
 'Создать папку': 'Create folder',
 'Таймаут выполнения должен быть не меньше 1 часа': 'The timeout must be at least one hour',
 'Таймаут, часов': 'Timeout, hours',
 'Тест': 'Test',
 'Тестовое сообщение отправлено.': 'Test message sent.',
 'Удаление': 'Delete',
 'Удалить задание «': 'Delete task "',
 'Удалить исходный .dt / .bak после успешной упаковки': 'Delete source .dt / .bak after successful archiving',
 'Удалять исходный .dt / .bak после успешной упаковки': 'Delete source .dt / .bak after successful archiving',
 'Укажите, где следует хранить создаваемые архивы': 'Select backup storage locations',
 'Указать 1cv8.exe': 'Choose 1cv8.exe',
 'Управление хранилищами приложения': 'Manage storage profiles',
 'Файл обработки (.epf)': 'Processing file (.epf)',
 'Формат:': 'Format:',
 'Хранилище': 'Storage',
 'Хранилище (галочка — используется)': 'Storage (checked means enabled)',
 'Частичная потеря данных:': 'Partial data loss:',
 'Эта задача отправляет сводку последних результатов всех настроенных заданий по расписанию.': 'This task '
                                                                                               'sends '
                                                                                               'scheduled '
                                                                                               'summary '
                                                                                               'reports for '
                                                                                               'all '
                                                                                               'configured '
                                                                                               'tasks.',
 'Язык интерфейса применяется после повторного открытия программы.': 'Restart to apply interface language.',
 '− Удалить': '− Delete',
 '☑ Локальный архив': '☑ Local backup',
 '✎ Изменить': '✎ Edit'})
EN_RU = {v:k for k,v in RU_EN.items()}


def tr(language: str, text: str) -> str:
    if language != "en" or not isinstance(text, str):
        return text
    if text in RU_EN:
        return RU_EN[text]
    # Labels with a colon: preserve consistent translation without affecting data.
    if text.endswith(":") and text[:-1] in RU_EN:
        return RU_EN[text[:-1]] + ":"
    for start, en in (("Задача: ", "Task: "), ("Всего заданий: ", "Tasks: "),
                      ("Включено: ", "Enabled: "), ("Агент ", "Agent ")):
        if text.startswith(start):
            return en + text[len(start):]
    return text


def source_value(text: str) -> str:
    """Return canonical Russian selector text even if interface shows English."""
    return EN_RU.get(text, text)


def translate_widgets(root, language: str) -> None:
    """Translate widget chrome without modifying editable user data."""
    if language != "en":
        return
    seen = set()
    def walk(widget):
        if str(widget) in seen:
            return
        seen.add(str(widget))
        try:
            txt = widget.cget("text")
            english = tr(language, txt)
            if english != txt:
                widget.configure(text=english)
        except (tk.TclError, AttributeError):
            pass
        if isinstance(widget, ttk.Notebook):
            for tab in widget.tabs():
                txt = widget.tab(tab, "text")
                widget.tab(tab, text=tr(language, txt))
        if isinstance(widget, ttk.Treeview):
            for col in ["#0", *widget.cget("columns")]:
                try:
                    txt = widget.heading(col, "text")
                    widget.heading(col, text=tr(language, txt))
                except tk.TclError:
                    pass
        if isinstance(widget, tk.Listbox):
            try:
                count = widget.size()
                selected = tuple(widget.curselection())
                for index in range(count):
                    ru = widget.get(index)
                    en = tr(language, ru)
                    if en != ru:
                        widget.delete(index)
                        widget.insert(index, en)
                for index in selected:
                    widget.selection_set(index)
            except tk.TclError:
                pass
        if isinstance(widget, ttk.Combobox):
            # Translate option labels, never server paths or free-form user input.
            current = widget.get()
            values = list(widget.cget("values"))
            changed = [tr(language, v) for v in values]
            if changed != values:
                widget.configure(values=changed)
                if current in values:
                    widget.set(tr(language, current))
        if isinstance(widget, tk.Menu):
            for index in range(widget.index("end") + 1 if widget.index("end") is not None else 0):
                try:
                    txt = widget.entrycget(index, "label")
                    new = tr(language, txt)
                    if new != txt:
                        widget.entryconfigure(index, label=new)
                    child = widget.entrycget(index, "menu")
                    if child:
                        walk(widget.nametowidget(child))
                except (tk.TclError, ValueError, KeyError):
                    pass
        for child in widget.winfo_children():
            walk(child)
    walk(root)
    try:
        root.title(tr(language, root.title()))
    except (AttributeError, tk.TclError):
        pass
