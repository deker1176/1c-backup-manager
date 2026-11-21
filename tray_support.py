"""Optional Windows tray icon. The Tk thread owns every Tk operation.

The pystray callback thread only posts action names into a queue consumed by
Tk's own periodic after callback, so no Tk widgets are manipulated off-thread.
"""
from __future__ import annotations
import threading

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = ImageDraw = None


def available() -> bool:
    return pystray is not None and Image is not None


def make_image():
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 9, 58, 53), radius=10, fill='#2277bb')
    draw.rectangle((15, 21, 49, 45), fill='white')
    draw.rectangle((23, 14, 41, 22), fill='white')
    draw.rectangle((23, 30, 41, 35), fill='#2277bb')
    return image


class TrayController:
    def __init__(self, post_action):
        self.post_action = post_action
        self.icon = None
        self.thread = None
        self.error = None

    def start(self):
        if not available():
            raise RuntimeError('Системный трей недоступен: требуется pystray и Pillow.')
        if self.thread and self.thread.is_alive():
            return
        menu = pystray.Menu(
            pystray.MenuItem('Открыть', lambda icon, item: self.post_action('show'), default=True),
            pystray.MenuItem('Выполнить все активные', lambda icon, item: self.post_action('run_all')),
            pystray.MenuItem('Закрыть интерфейс', lambda icon, item: self.post_action('exit')),
        )
        self.icon = pystray.Icon('1CBackupManager', make_image(), '1C Backup Manager', menu)
        ready = threading.Event()
        self.error = None

        def worker():
            def setup(icon):
                # CRITICAL: a custom pystray setup overrides pystray's default
                # setup which normally sets visible=True. Without this line the
                # background thread exists but NO icon is displayed on Windows.
                try:
                    icon.visible = True
                except Exception as exc:
                    self.error = exc
                finally:
                    ready.set()
            try:
                self.icon.run(setup=setup)
            except Exception as exc:
                self.error = exc
                ready.set()

        self.thread = threading.Thread(target=worker, name='1CBackupTray', daemon=True)
        self.thread.start()
        if not ready.wait(timeout=5):
            raise RuntimeError('Не удалось показать значок в системном трее.')
        if self.error is not None:
            self.stop()
            raise RuntimeError(f'Системный трей недоступен: {self.error}')
        if not getattr(self.icon, "visible", False):
            self.stop()
            raise RuntimeError('Значок трея не стал видимым. Проверьте Explorer.exe и область уведомлений Windows.')

    def stop(self):
        if self.icon is not None:
            self.icon.stop()
            self.icon = None
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.thread = None
