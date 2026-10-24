"""Spotify desktop control without the Web API (no account login, no tokens).

- what is playing: the main Spotify window title is "Artist - Track" while music
  plays and "Spotify" / "Spotify Premium" / "Spotify Free" when paused;
- play/pause/next/previous: WM_APPCOMMAND sent to that window (no synthetic
  keyboard input, nothing else receives it);
- search / open: the registered spotify: URI scheme (Store and classic installs).
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from urllib.parse import quote

import psutil

from .bridge import WindowsBridgeError

user32 = ctypes.windll.user32
WM_APPCOMMAND = 0x0319
APPCOMMANDS = {"play_pause": 14, "stop": 13, "next": 11, "previous": 12}
IDLE_TITLES = {"spotify", "spotify premium", "spotify free"}

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


class Spotify:
    def _pids(self) -> set[int]:
        return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "spotify.exe"}

    def running(self) -> bool:
        return bool(self._pids())

    def main_window(self) -> int | None:
        """The top-level Spotify window that carries the track title."""
        pids, found = self._pids(), []
        if not pids:
            return None

        @_EnumProc
        def cb(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.GetWindow(hwnd, 4) == 0:  # GW_OWNER: not an owned popup
                t = _title(hwnd)
                if t and t not in ("Default IME", "MSCTFIME UI"):
                    found.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        # prefer a window with a track or "Spotify ..." title
        found.sort(key=lambda h: (not (" - " in _title(h) or _title(h).lower() in IDLE_TITLES)))
        return found[0] if found else None

    def now_playing(self) -> dict:
        if not self.running():
            return {"running": False, "playing": False}
        hwnd = self.main_window()
        title = _title(hwnd) if hwnd else ""
        if not title or title.lower() in IDLE_TITLES or " - " not in title:
            return {"running": True, "playing": False}
        artist, track = title.split(" - ", 1)
        return {"running": True, "playing": True, "artist": artist.strip(), "track": track.strip()}

    def control(self, action: str, settle_s: float = 1.2) -> dict:
        """play / pause are idempotent (checked against the current state); play_pause toggles."""
        if action not in APPCOMMANDS and action not in ("play", "pause"):
            raise WindowsBridgeError(f"unknown action '{action}'")
        hwnd = self.main_window()
        if not hwnd:
            raise WindowsBridgeError("Spotify is not running (open it with open_spotify first)")
        before = self.now_playing()
        if (action == "pause" and not before["playing"]) or (action == "play" and before["playing"]):
            return {"action": action, "before": before, "after": before, "changed": False,
                    "note": "already paused" if action == "pause" else "already playing"}
        command = APPCOMMANDS["play_pause" if action in ("play", "pause") else action]
        user32.SendMessageW(hwnd, WM_APPCOMMAND, hwnd, command << 16)
        # report what actually changed, not what we hoped for
        deadline = time.monotonic() + settle_s
        after = before
        while time.monotonic() < deadline:
            time.sleep(0.2)
            after = self.now_playing()
            if after != before:
                break
        return {"action": action, "before": before, "after": after, "changed": after != before}

    def open(self, wait_s: float = 8.0) -> dict:
        was_running = self.running()
        os.startfile("spotify:")
        deadline = time.monotonic() + (0 if was_running else wait_s)
        while not self.running() and time.monotonic() < deadline:
            time.sleep(0.3)
        return {"was_running": was_running, "running": self.running()}

    def search(self, query: str) -> dict:
        """Open Spotify on its search page for `query` (the user picks what to play)."""
        os.startfile("spotify:search:" + quote(query, safe=""))
        return {"opened_search": query, "note": "Spotify показывает результаты поиска; воспроизведение "
                                                "запускает пользователь (или spotify_control play_pause)."}
