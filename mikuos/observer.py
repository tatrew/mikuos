"""ActivityObserver: local, metadata-only polling of permitted desktop state.

Collected: foreground window changes (process; title only if enabled), app
start/close (by process), window moves, idle/away/return, long focus, fullscreen.
Never collected: keystrokes, typed text, window contents, screenshots.
Observation can be switched off at runtime; its state is exposed so the overlay
can show a visible indicator.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .config import Settings
from .events import EventBus
from .memory import MemoryStore
from .permissions import Decision, Perm, PermissionManager

log = logging.getLogger("mikuos.observer")

PERSISTED = {"active_window_changed", "app_started", "app_closed", "user_away", "user_returned", "long_focus",
             "fullscreen_changed"}


class ActivityObserver:
    def __init__(self, bridge, bus: EventBus, settings: Settings, perms: PermissionManager,
                 store: MemoryStore | None = None, clock: Callable[[], float] = time.monotonic):
        self.bridge = bridge
        self.bus = bus
        self.settings = settings
        self.perms = perms
        self.store = store
        self.clock = clock
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.active = False
        self.last_snapshot: dict = {}
        self._fg: tuple | None = None
        self._fg_since = clock()
        self._long_focus_sent = False
        self._processes: set[str] | None = None
        self._geoms: dict[int, tuple] = {}
        self._away = False
        self._away_idle = 0.0
        self._fullscreen = False

    @property
    def enabled(self) -> bool:
        return (bool(self.settings.get("observation", "enabled"))
                and self.perms.policy(Perm.OBSERVE_METADATA) != Decision.DENY
                and not self.perms.emergency_stopped)

    def start(self) -> None:
        if not self._task:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="activity-observer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            interval = float(self.settings.get("observation", "poll_interval_s") or 1.0)
            if self.enabled:
                self.active = True
                try:
                    snap = await asyncio.to_thread(self.snapshot)
                    await self.process(snap)
                except Exception:
                    log.exception("observer tick failed")
            else:
                self.active = False
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        self.active = False

    def snapshot(self) -> dict:
        windows = self.bridge.list_windows()
        fg = self.bridge.get_active_window()
        return {
            "fg": (fg.id, fg.process, fg.title) if fg else None,
            "windows": [(w.id, w.process, w.rect.x, w.rect.y, w.rect.width, w.rect.height, w.is_minimized)
                        for w in windows],
            "idle": self.bridge.get_user_idle_time(),
            "fullscreen": self.bridge.is_fullscreen_foreground(),
        }

    async def _emit(self, etype: str, data: dict) -> None:
        if self.store and etype in PERSISTED and self.settings.get("observation", "store_events"):
            stored = dict(data)
            if not self.settings.get("observation", "store_window_titles"):
                stored.pop("title", None)
            self.store.add_event(etype, stored)
        await self.bus.publish(etype, data)

    async def process(self, snap: dict) -> None:
        """Turn a snapshot into events (pure diffing; unit-testable)."""
        self.last_snapshot = snap
        now = self.clock()
        cfg = self.settings.get("initiative")
        fg = snap.get("fg")
        if fg and (self._fg is None or fg[1] != self._fg[1] or fg[0] != self._fg[0]):
            if self._fg is None or fg[1] != self._fg[1]:
                self._fg_since = now
                self._long_focus_sent = False
            self._fg = fg
            await self._emit("active_window_changed", {"window_id": fg[0], "process": fg[1], "title": fg[2]})
        elif fg and not self._long_focus_sent and now - self._fg_since >= float(cfg.get("long_focus_s", 5400)):
            self._long_focus_sent = True
            await self._emit("long_focus", {"process": fg[1], "minutes": int((now - self._fg_since) / 60)})

        procs = {w[1] for w in snap.get("windows", []) if w[1]}
        if self._processes is not None:
            for p in sorted(procs - self._processes):
                await self._emit("app_started", {"process": p})
            for p in sorted(self._processes - procs):
                await self._emit("app_closed", {"process": p})
        self._processes = procs

        geoms = {w[0]: w for w in snap.get("windows", [])}
        for wid, g in geoms.items():
            old = self._geoms.get(wid)
            if old and old[2:6] != g[2:6]:
                await self.bus.publish("window_moved", {"window_id": wid, "process": g[1], "x": g[2], "y": g[3],
                                                        "width": g[4], "height": g[5], "dx": g[2] - old[2],
                                                        "dy": g[3] - old[3]})
        self._geoms = geoms

        idle = float(snap.get("idle", 0))
        away_after = float(cfg.get("away_threshold_s", 600))
        if not self._away and idle >= away_after:
            self._away = True
            await self._emit("user_away", {"idle_s": int(idle)})
        elif self._away and idle < 5:
            self._away = False
            await self._emit("user_returned", {"away_s": int(max(self._away_idle, away_after))})
        if self._away:
            self._away_idle = idle

        fs = bool(snap.get("fullscreen"))
        if fs != self._fullscreen:
            self._fullscreen = fs
            await self._emit("fullscreen_changed", {"fullscreen": fs})

    def current_context_lines(self) -> list[str]:
        """Permitted facts for the LLM context (process name only, never titles)."""
        if not self.enabled or not self._fg:
            return []
        lines = [f"Активное приложение пользователя: {self._fg[1]}"]
        if self._away:
            lines.append("Пользователь сейчас отошёл от компьютера")
        return lines
