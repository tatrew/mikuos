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
import re
import time
from _ctypes import COMError
from ctypes import wintypes
from urllib.parse import quote

import psutil

from .bridge import WindowsBridgeError

user32 = ctypes.windll.user32
WM_APPCOMMAND = 0x0319
APPCOMMANDS = {"play_pause": 14, "stop": 13, "next": 11, "previous": 12}
IDLE_TITLES = {"spotify", "spotify premium", "spotify free"}
# play buttons of result rows: "Слушать плейлист «World is Mine»" (ru UI), "Play World is Mine by X" (en UI)
_PLAY_QUOTED = re.compile(r"^(?:Слушать|Воспроизвести|Play)\b.*?«(.+)»\s*$")
_PLAY_EN = re.compile(r"^Play (.+)$")
BARE_PLAY = {"Слушать", "Воспроизвести", "Play"}  # the first (top) row's button carries no title
BARE_PAUSE = {"Пауза", "Pause"}


def play_button_title(name: str) -> str | None:
    """Track/playlist title from a result row's play button name; None for other buttons."""
    m = _PLAY_QUOTED.match(name) or _PLAY_EN.match(name)
    return m.group(1).strip() if m else None

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


class Spotify:
    def __init__(self, bridge=None):
        self._bridge = bridge  # WindowsBridge, for UI Automation (created lazily)
        self._last_query = ""

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

    # ------------------------------------------------- play a track by name
    def _uia(self):
        if self._bridge is None:
            from .bridge import WindowsBridge
            self._bridge = WindowsBridge()
        return self._bridge._uia_client()

    def _first_result(self, hwnd: int) -> dict | None:
        """The first result row inside Spotify's "Search results" area (its accessible name
        is not localized): {element: play button, title, playing, rid: row id}.
        The first row's button is named just "Слушать"/"Play" (or "Пауза"/"Pause" when
        that track is the one playing); the title then comes from the row itself.
        None while there are no results or Spotify is re-rendering the list."""
        try:
            return self._read_first_result(hwnd)
        except COMError:  # an element vanished mid-read: the caller polls again
            return None

    def _read_first_result(self, hwnd: int) -> dict | None:
        uia, mod = self._uia()
        root = uia.ElementFromHandle(hwnd)
        area = root.FindFirst(4, uia.CreatePropertyCondition(30005, "Search results"))  # UIA_NamePropertyId
        if not area:
            return None
        buttons = area.FindAll(4, uia.CreatePropertyCondition(30003, 50000))  # ControlType == Button
        walker = uia.ControlViewWalker
        for i in range(buttons.Length):
            el = buttons.GetElement(i)
            name = (el.CurrentName or "").strip()
            title, bare_play, bare_pause = play_button_title(name), name in BARE_PLAY, name in BARE_PAUSE
            if not (title or bare_play or bare_pause):
                continue  # context menu, "add to playlist", ...
            row = walker.GetParentElement(el)
            while row and row.CurrentControlType != 50026:  # the result row is a Group
                row = walker.GetParentElement(row)
            if not title and row:
                title = row.CurrentName or ""
            rid = tuple(row.GetRuntimeId()) if row else tuple(el.GetRuntimeId())
            return {"element": el, "title": title, "playing": bare_pause, "rid": rid}
        return None

    def play_search(self, query: str, wait_s: float = 10.0, settle_s: float = 4.0) -> dict:
        """Search Spotify for `query` and press play on the first result.
        Reports what actually plays afterwards (from the window title)."""
        hwnd = self.main_window()
        if not hwnd:
            raise WindowsBridgeError("Spotify is not running (open it with open_spotify first)")
        old = self._first_result(hwnd)  # results of a previous search, if that page is open
        same_query = query.strip().lower() == self._last_query
        os.startfile("spotify:search:" + quote(query, safe=""))
        self._last_query = query.strip().lower()
        started = time.monotonic()
        deadline = started + wait_s
        res = None
        while time.monotonic() < deadline:
            time.sleep(0.5)
            res = self._first_result(hwnd)
            fresh = res is not None and (old is None or res["rid"] != old["rid"])
            # the same query again: Spotify may keep the very same page, which is then correct
            kept_same_page = res is not None and same_query and time.monotonic() - started > 2.5
            if fresh or kept_same_page:
                break
        else:
            res = None
        if res is None:
            raise WindowsBridgeError(f"no search results appeared in Spotify for '{query}'")
        before = self.now_playing()
        if res["playing"]:
            # pressing would PAUSE it: the requested track is already the one playing
            return {"query": query, "pressed_play_on": None, "already_playing": res["title"],
                    "now_playing": before, "started": False}
        el, title = res["element"], res["title"]
        _, mod = self._uia()
        el.GetCurrentPattern(10000).QueryInterface(mod.IUIAutomationInvokePattern).Invoke()  # InvokePattern
        after = before
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            time.sleep(0.3)
            after = self.now_playing()
            if after.get("playing") and after != before:
                break
        started = bool(after.get("playing")) and after != before
        res = {"query": query, "pressed_play_on": title, "now_playing": after, "started": started}
        if not started:
            res["note"] = "Нажата кнопка воспроизведения, но смена трека не подтверждена."
        return res

    def search(self, query: str) -> dict:
        """Open Spotify on its search page for `query` (the user picks what to play)."""
        os.startfile("spotify:search:" + quote(query, safe=""))
        return {"opened_search": query, "note": "Spotify показывает результаты поиска; воспроизведение "
                                                "запускает пользователь (или spotify_control play_pause)."}
