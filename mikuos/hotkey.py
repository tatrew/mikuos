"""Global emergency hotkey (default Ctrl+Alt+M) via RegisterHotKey on its own thread."""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger("mikuos.hotkey")

MOD = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def parse_hotkey(spec: str) -> tuple[int, int]:
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods, key = 0, None
    for p in parts:
        if p in MOD:
            mods |= MOD[p]
        elif len(p) == 1 and p.isalnum():
            key = ord(p.upper())
        elif p.startswith("f") and p[1:].isdigit():
            key = 0x70 + int(p[1:]) - 1
        else:
            raise ValueError(f"unsupported key '{p}' in hotkey '{spec}'")
    if key is None or not mods:
        raise ValueError(f"hotkey needs a modifier and a key: '{spec}'")
    return mods, key


class HotkeyListener:
    """Registers the first free combination from `spec` + `fallbacks`."""

    def __init__(self, spec: str, callback: Callable[[], None], hotkey_id: int = 0x4D49,
                 fallbacks: list[str] | None = None):
        self.candidates = [spec] + [f for f in (fallbacks or []) if f != spec]
        for c in self.candidates:
            parse_hotkey(c)  # validate early
        self.active_spec: str | None = None
        self.callback = callback
        self.hotkey_id = hotkey_id
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self.registered = False
        self.error: str | None = None
        self._ready = threading.Event()

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self.registered

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        errors = []
        for spec in self.candidates:
            mods, vk = parse_hotkey(spec)
            if user32.RegisterHotKey(None, self.hotkey_id, mods | MOD_NOREPEAT, vk):
                self.active_spec = spec
                break
            errors.append(f"{spec}: error {ctypes.GetLastError()}")
        if not self.active_spec:
            self.error = "RegisterHotKey failed (" + "; ".join(errors) + "); combination is used by another program"
            log.warning(self.error)
            self._ready.set()
            return
        if errors:
            log.warning("emergency hotkey %s is taken (%s); using %s", self.candidates[0], errors[0], self.active_spec)
        self.registered = True
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("hotkey callback failed")
        finally:
            user32.UnregisterHotKey(None, self.hotkey_id)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
