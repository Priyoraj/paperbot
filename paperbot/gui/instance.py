"""One PaperBot window per install.

Starting the app a second time (the shortcut, a click on a notification, the sign-in task)
doesn't open another window: the second start asks the first to come forward - on a given
page if it says which - and goes away. On Windows this uses a named mutex ("someone is
already here") and a named event ("come forward"); elsewhere every start is the first.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ALREADY_EXISTS = 183        # Windows: the named object was there before we asked for it


class Instance:
    def __init__(self, home: Path, name: str | None = None):
        self.wish = Path(home) / "data" / "app.show"      # which page the next "come forward" wants
        self.first = True
        self._event = self._mutex = self._k32 = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        tag = name or hashlib.sha1(str(Path(home).resolve()).lower().encode()).hexdigest()[:12]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._mutex = k32.CreateMutexW(None, False, f"Local\\PaperBot-app-{tag}")
        self.first = ctypes.get_last_error() != ALREADY_EXISTS
        self._event = k32.CreateEventW(None, False, False, f"Local\\PaperBot-show-{tag}")

    def ask_to_show(self, page: str = "") -> None:
        """Called by a later start: the first window should come forward (on `page`)."""
        try:
            self.wish.parent.mkdir(parents=True, exist_ok=True)
            self.wish.write_text(page, encoding="utf-8")
        except OSError:
            pass
        if self._event:
            try:    # this start came from a click: hand on its right to step in front of
                import ctypes                                  # whatever is on screen now
                ctypes.windll.user32.AllowSetForegroundWindow(-1)      # -1: any process
            except (AttributeError, OSError):
                pass
            self._k32.SetEvent(self._event)

    def asked(self) -> str | None:
        """Polled by the first window. None: nobody asked. '' or a page name: come forward."""
        if not self._event or self._k32.WaitForSingleObject(self._event, 0) != 0:
            return None
        try:
            page = self.wish.read_text(encoding="utf-8").strip()
            self.wish.unlink()
        except OSError:
            page = ""
        return page

    def close(self) -> None:
        for handle in (self._event, self._mutex):
            if handle and self._k32:
                self._k32.CloseHandle(handle)
        self._event = self._mutex = None
