# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
import threading
from pathlib import Path
import pytest

import service_status
import tray_support
import single_instance


def test_agent_status_mapping_truthful():
    assert service_status.service_labels(4) == ("Агент запущен", "Служба: работает")
    assert service_status.service_labels(1) == ("Агент остановлен", "Служба: остановлена")
    assert service_status.service_labels(None) == ("Агент не установлен", "Служба: не установлена")
    assert "запускается" in service_status.service_labels(2)[0]


def test_non_windows_single_instance_is_noop():
    if sys.platform == 'win32':
        pytest.skip('Windows named mutex is tested via live Win32 APIs')
    broker = single_instance.GuiSingleInstance()
    assert broker.is_primary is True
    assert not broker.activation_requested()
    assert not broker.activate_existing()
    broker.close()


def test_tray_controller_requests_visible_icon(monkeypatch):
    done = threading.Event()
    installed = []
    callback_calls = []

    class FakeIcon:
        def __init__(self, name, image, title, menu):
            self.visible = False
            self.menu = menu
            self.ready = threading.Event()
            installed.append(self)
        def run(self, setup):
            setup(self)
            self.ready.set()
            done.wait(3)
        def stop(self):
            done.set()

    class FakePystray:
        Icon = FakeIcon
        class Menu(tuple):
            def __new__(cls, *items): return super().__new__(cls, items)
        class MenuItem:
            def __init__(self, name, callback, **kwargs):
                self.name = name
                self.callback = callback

    monkeypatch.setattr(tray_support, 'pystray', FakePystray)
    monkeypatch.setattr(tray_support, 'Image', object())
    monkeypatch.setattr(tray_support, 'make_image', lambda: object())
    controller = tray_support.TrayController(callback_calls.append)
    controller.start()
    assert len(installed) == 1
    assert installed[0].ready.wait(1)
    assert installed[0].visible is True, 'This is the bug from v4.4: pystray icon ran but was never visible.'
    assert [x.name for x in installed[0].menu] == ['Открыть', 'Выполнить все активные', 'Закрыть интерфейс']
    controller.stop()
    assert controller.thread is None


def test_tray_icon_visibility_failure_is_not_silent(monkeypatch):
    class FakeIcon:
        def __init__(self, name, image, title, menu):
            self.visible = False
        def run(self, setup):
            # force a visibility error to make sure the GUI never withdraws
            raise RuntimeError('Explorer unavailable')
        def stop(self): pass
    class FakePystray:
        Icon = FakeIcon
        class Menu(tuple):
            def __new__(cls, *items): return super().__new__(cls, items)
        class MenuItem:
            def __init__(self, name, cb, **kwargs): pass
    monkeypatch.setattr(tray_support, 'pystray', FakePystray)
    monkeypatch.setattr(tray_support, 'Image', object())
    monkeypatch.setattr(tray_support, 'make_image', lambda: object())
    with pytest.raises(RuntimeError, match='Explorer unavailable'):
        tray_support.TrayController(lambda x: None).start()


def test_main_uses_local_mutex_and_activation_event():
    source = (Path(__file__).parent/'single_instance.py').read_text(encoding='utf-8')
    assert 'Local\\\\' in source
    assert 'CreateMutexW' in source and 'CreateEventW' in source
    assert 'SetEvent' in source and 'WaitForSingleObject' in source


def test_status_is_packed_at_bottom_before_expanding_pane():
    source = (Path(__file__).parent/'effector_gui.py').read_text(encoding='utf-8')
    assert 'status.pack(side="bottom", fill="x", before=top)' in source


def test_gui_has_restore_handler_for_second_launch():
    source = (Path(__file__).parent/'onec_backup_manager.py').read_text(encoding='utf-8')
    assert 'gui_instance.activate_existing()' in source
    assert 'app.restore_from_tray()' in source


def test_windows_named_mutex_and_activation_event_simulated(monkeypatch):
    """Simulate two launches in the same interactive Windows session."""
    import types
    from threading import Event
    class FakeFunction:
        def __init__(self, fn): self.fn = fn
        def __call__(self, *args): return self.fn(*args)
    class FakeKernel:
        def __init__(self):
            self.mutexes = set()
            self.events = {}
            self.last_error = 0
            self.CreateMutexW = FakeFunction(self.create_mutex)
            self.CreateEventW = FakeFunction(self.create_event)
            self.SetEvent = FakeFunction(self.set_event)
            self.WaitForSingleObject = FakeFunction(self.wait_for_single)
            self.CloseHandle = FakeFunction(lambda handle: True)
        def create_mutex(self, unused, initial, name):
            self.last_error = 183 if name in self.mutexes else 0
            self.mutexes.add(name)
            return name
        def create_event(self, unused, manual, initial, name):
            self.events.setdefault(name, Event())
            self.last_error = 183 if name in self.events else 0
            return name
        def set_event(self, name):
            self.events[name].set()
            return True
        def wait_for_single(self, name, milliseconds):
            event = self.events[name]
            if event.is_set():
                event.clear()
                return single_instance.WAIT_OBJECT_0
            return single_instance.WAIT_TIMEOUT

    kernel = FakeKernel()
    monkeypatch.setattr(single_instance, 'os', types.SimpleNamespace(name='nt'))
    monkeypatch.setattr(single_instance.ctypes, 'WinDLL', lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(single_instance.ctypes, 'get_last_error', lambda: kernel.last_error, raising=False)
    gui1 = single_instance.GuiSingleInstance()
    assert gui1.is_primary
    gui2 = single_instance.GuiSingleInstance()
    assert not gui2.is_primary
    assert not gui1.activation_requested()
    assert gui2.activate_existing()
    assert gui1.activation_requested()
    assert not gui1.activation_requested()  # auto-reset
    gui1.close()
    gui2.close()
