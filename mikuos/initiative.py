"""InitiativeEngine: decides when Miku may speak on her own.

Gates (all must pass): mode is not DND, trigger allowed for the mode, cooldown
elapsed, daily LLM budget not exhausted (counted in SQLite, survives restarts),
no fullscreen game/presentation, no emergency stop, LLM connected.
Animations never go through here — they are client-side and LLM-free.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Awaitable, Callable

from .config import Settings
from .events import Event, EventBus
from .memory import MemoryStore
from .permissions import PermissionManager

log = logging.getLogger("mikuos.initiative")

MODES = ("chatty", "normal", "quiet", "dnd")
TRIGGERS_BY_MODE = {
    "chatty": {"user_returned", "long_focus", "pet_interaction", "app_started", "greeting"},
    "normal": {"user_returned", "long_focus", "pet_interaction", "greeting"},
    "quiet": {"user_returned", "pet_interaction"},
    "dnd": set(),
}
NOTABLE_APPS = {"notepad.exe", "code.exe", "winword.exe", "excel.exe", "photoshop.exe", "blender.exe"}

DESCRIBE = {
    "user_returned": lambda d: f"Пользователь вернулся к компьютеру после {max(1, d.get('away_s', 0) // 60)} мин. отсутствия.",
    "long_focus": lambda d: f"Пользователь уже {d.get('minutes', 0)} мин. работает в {d.get('process', 'одной программе')}.",
    "pet_interaction": lambda d: f"Пользователь {_PET.get(d.get('kind'), 'взаимодействует с тобой')} на рабочем столе.",
    "app_started": lambda d: f"Пользователь открыл приложение {d.get('process')}.",
    "greeting": lambda d: "MikuOS только что запустилась, пользователь за компьютером.",
}
_PET = {"click": "нажал на тебя", "double_click": "дважды нажал на тебя", "drag": "перетащил тебя",
        "pet": "погладил тебя"}

Speaker = Callable[[str, str, dict], Awaitable[str | None]]


class InitiativeEngine:
    def __init__(self, bus: EventBus, settings: Settings, store: MemoryStore, perms: PermissionManager,
                 speaker: Speaker, is_fullscreen: Callable[[], bool] = lambda: False,
                 llm_available: Callable[[], bool] = lambda: True, clock: Callable[[], float] = time.monotonic):
        self.bus = bus
        self.settings = settings
        self.store = store
        self.perms = perms
        self.speaker = speaker
        self.is_fullscreen = is_fullscreen
        self.llm_available = llm_available
        self.clock = clock
        self._last_spoken: float | None = None
        self._busy = False
        self.last_decision: dict = {}
        for t in ("user_returned", "long_focus", "pet_interaction", "app_started", "greeting"):
            bus.subscribe(t, self.on_event)

    @property
    def mode(self) -> str:
        m = self.settings.get("initiative", "mode", "normal")
        return m if m in MODES else "normal"

    def used_today(self) -> int:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.store.count_events_since("initiative_message", start)

    def decide(self, trigger: str, data: dict | None = None) -> tuple[bool, str]:
        data = data or {}
        mode = self.mode
        cfg = self.settings.get("initiative")
        if self.perms.emergency_stopped:
            return False, "emergency stop"
        if mode == "dnd":
            return False, "do not disturb"
        if trigger not in TRIGGERS_BY_MODE[mode]:
            return False, f"trigger '{trigger}' not enabled in mode '{mode}'"
        if trigger == "app_started" and data.get("process", "").lower() not in NOTABLE_APPS:
            return False, "app not notable"
        if trigger == "user_returned" and data.get("away_s", 0) < float(cfg.get("away_threshold_s", 600)):
            return False, "absence too short"
        if self._busy:
            return False, "already speaking"
        cooldown = float((cfg.get("cooldown_s") or {}).get(mode, 900))
        if trigger == "pet_interaction":
            cooldown = min(cooldown, 120)  # direct interaction deserves faster replies
        if self._last_spoken is not None and self.clock() - self._last_spoken < cooldown:
            return False, "cooldown"
        if self.used_today() >= int(cfg.get("daily_llm_limit", 30)):
            return False, "daily limit reached"
        if cfg.get("suppress_fullscreen", True) and self.is_fullscreen():
            return False, "fullscreen app / presentation"
        if not self.llm_available():
            return False, "LLM unavailable"
        return True, "ok"

    async def on_event(self, ev: Event) -> None:
        await self.consider(ev.type, ev.data)

    async def consider(self, trigger: str, data: dict | None = None) -> str | None:
        data = data or {}
        ok, why = self.decide(trigger, data)
        self.last_decision = {"trigger": trigger, "spoke": ok, "reason": why, "at": time.time()}
        if not ok:
            log.debug("initiative %s suppressed: %s", trigger, why)
            return None
        self._busy = True
        self._last_spoken = self.clock()
        try:
            self.store.add_event("initiative_message", {"trigger": trigger})
            text = await self.speaker(trigger, DESCRIBE[trigger](data), data)
            if text:
                await self.bus.publish("initiative_message", {"trigger": trigger, "text": text})
            return text
        except Exception:
            log.exception("initiative speaker failed")
            return None
        finally:
            self._busy = False
