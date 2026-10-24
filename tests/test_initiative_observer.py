"""9. Initiative limits (modes, cooldown, daily limit across restarts, fullscreen,
DND) and ActivityObserver diffing (no titles stored by default)."""
import pytest

from mikuos.events import EventBus
from mikuos.initiative import InitiativeEngine
from mikuos.memory import Database, MemoryStore
from mikuos.observer import ActivityObserver
from mikuos.permissions import PermissionManager


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def engine(settings, store, clock, fullscreen=False, llm=True):
    spoken = []

    async def speaker(trigger, desc, data):
        spoken.append((trigger, desc))
        return f"реплика {len(spoken)}"

    eng = InitiativeEngine(EventBus(), settings, store, PermissionManager(settings), speaker,
                           is_fullscreen=lambda: fullscreen, llm_available=lambda: llm, clock=clock)
    return eng, spoken


@pytest.fixture
def store(tmp_path):
    return MemoryStore(Database(tmp_path / "i.db"))


async def test_modes_and_cooldown(tmp_settings, store):
    clock = Clock()
    eng, spoken = engine(tmp_settings, store, clock)
    assert await eng.consider("user_returned", {"away_s": 1200}) == "реплика 1"
    assert await eng.consider("user_returned", {"away_s": 1200}) is None
    assert eng.last_decision["reason"] == "cooldown"
    clock.t += 901
    assert await eng.consider("long_focus", {"minutes": 95, "process": "code.exe"})
    clock.t += 10_000
    assert await eng.consider("user_returned", {"away_s": 30}) is None  # too short
    assert await eng.consider("app_started", {"process": "notepad.exe"}) is None  # not in normal mode
    tmp_settings.update({"initiative": {"mode": "quiet"}})
    assert await eng.consider("long_focus", {"minutes": 95}) is None
    assert len(spoken) == 2


async def test_dnd_and_disabled_initiative_never_speaks(tmp_settings, store):
    """Test F: with initiative off (DND) no message is sent, whatever happens."""
    tmp_settings.update({"initiative": {"mode": "dnd"}})
    clock = Clock()
    eng, spoken = engine(tmp_settings, store, clock)
    for trig, data in [("user_returned", {"away_s": 99999}), ("pet_interaction", {"kind": "click"}),
                       ("long_focus", {"minutes": 500}), ("greeting", {}), ("app_started", {"process": "code.exe"})]:
        clock.t += 100_000
        await eng.bus.publish(trig, data)
    assert spoken == [] and store.count_events_since("initiative_message",
                                                     __import__("datetime").datetime(2000, 1, 1)) == 0


async def test_daily_limit_survives_restart(tmp_settings, tmp_path):
    tmp_settings.update({"initiative": {"daily_llm_limit": 2, "mode": "chatty"}})
    path = tmp_path / "limit.db"
    store = MemoryStore(Database(path))
    clock = Clock()
    eng, spoken = engine(tmp_settings, store, clock)
    for _ in range(2):
        clock.t += 10_000
        assert await eng.consider("pet_interaction", {"kind": "click"})
    store.db.close()
    store2 = MemoryStore(Database(path))  # "restart"
    eng2, spoken2 = engine(tmp_settings, store2, Clock())
    assert await eng2.consider("pet_interaction", {"kind": "click"}) is None
    assert eng2.last_decision["reason"] == "daily limit reached" and spoken2 == []


async def test_fullscreen_offline_and_emergency_suppress(tmp_settings, store):
    eng, spoken = engine(tmp_settings, store, Clock(), fullscreen=True)
    assert await eng.consider("pet_interaction", {"kind": "click"}) is None
    assert eng.last_decision["reason"].startswith("fullscreen")
    eng, spoken = engine(tmp_settings, store, Clock(), llm=False)
    assert await eng.consider("pet_interaction", {"kind": "click"}) is None
    eng, spoken = engine(tmp_settings, store, Clock())
    eng.perms.emergency_stop()
    assert await eng.consider("pet_interaction", {"kind": "click"}) is None
    assert spoken == []


async def test_observer_diffing_and_privacy(tmp_settings, store):
    tmp_settings.update({"observation": {"enabled": True, "store_window_titles": False},
                         "initiative": {"away_threshold_s": 60, "long_focus_s": 100}})
    bus = EventBus()
    got = []
    bus.subscribe("*", lambda e: got.append(e))
    clock = Clock()
    obs = ActivityObserver(None, bus, tmp_settings, PermissionManager(tmp_settings), store, clock=clock)
    w1 = (1, "code.exe", 0, 0, 800, 600, False)
    await obs.process({"fg": (1, "code.exe", "secret.py — VS Code"), "windows": [w1], "idle": 0, "fullscreen": False})
    w2 = (2, "notepad.exe", 10, 10, 300, 300, False)
    await obs.process({"fg": (1, "code.exe", "x"), "windows": [w1, w2], "idle": 0, "fullscreen": False})
    w1m = (1, "code.exe", 50, 40, 800, 600, False)
    await obs.process({"fg": (1, "code.exe", "x"), "windows": [w1m, w2], "idle": 70, "fullscreen": False})
    clock.t += 200
    await obs.process({"fg": (1, "code.exe", "x"), "windows": [w1m], "idle": 300, "fullscreen": True})
    await obs.process({"fg": (1, "code.exe", "x"), "windows": [w1m], "idle": 1, "fullscreen": False})
    types = [e.type for e in got]
    assert types.count("active_window_changed") == 1
    for t in ["app_started", "window_moved", "user_away", "long_focus", "app_closed", "fullscreen_changed",
              "user_returned"]:
        assert t in types, t
    moved = next(e for e in got if e.type == "window_moved")
    assert (moved.data["dx"], moved.data["dy"]) == (50, 40)
    returned = next(e for e in got if e.type == "user_returned")
    assert returned.data["away_s"] >= 300
    stored = store.recent_events(50)
    assert stored and all("title" not in e["event_data"] for e in stored)
    assert all(e["event_type"] != "window_moved" for e in stored)
    assert obs.current_context_lines() == ["Активное приложение пользователя: code.exe"]


async def test_observer_disabled_by_setting_or_emergency(tmp_settings, store):
    perms = PermissionManager(tmp_settings)
    obs = ActivityObserver(None, EventBus(), tmp_settings, perms, store)
    tmp_settings.update({"observation": {"enabled": True}})
    assert obs.enabled
    perms.emergency_stop()
    assert not obs.enabled
    perms.resume()
    tmp_settings.update({"observation": {"enabled": False}})
    assert not obs.enabled and obs.current_context_lines() == []
