"""Overlay application: pet + speech bubble + tray + chat/settings windows.

Frame loop (30 FPS): WindowTracker (every 250 ms) -> AvatarController -> PetWindow.
None of this calls the LLM; the backend is only used for chat, events and settings.
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")  # work in physical pixels like Win32

from PySide6.QtCore import QPoint, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

from mikuos.config import Settings  # noqa: E402
from mikuos.windows import WindowsBridge  # noqa: E402

from .animation import State  # noqa: E402
from .avatar_controller import AvatarController, Settings as AvatarSettings  # noqa: E402
from .backend_client import BackendClient, ConnState  # noqa: E402
from .chat_window import ChatWindow  # noqa: E402
from .pet_window import PetWindow, SpeechBubble  # noqa: E402
from .renderer import PlaceholderRenderer  # noqa: E402
from .settings_window import SettingsWindow  # noqa: E402
from .window_tracker import WindowTracker  # noqa: E402
from .expression import finite, smooth  # noqa: E402

log = logging.getLogger("mikuos.client")


def tray_icon() -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#39c5bb"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.setBrush(QColor("white"))
    p.drawEllipse(10, 12, 5, 6)
    p.drawEllipse(18, 12, 5, 6)
    p.end()
    return QIcon(pm)


class OverlayApp:
    def __init__(self, qapp: QApplication, settings: Settings, bridge: WindowsBridge | None = None):
        self.qapp = qapp
        self.settings = settings
        self.bridge = bridge or WindowsBridge()
        ui = settings.get("ui")
        scale = float(ui.get("pet_scale", 1.0))
        self.pet = self._make_pet(ui.get("avatar", "auto"), scale, int(ui.get("model_px", 220)),
                                  int(ui.get("sprite_scale", 2)))
        self.ctrl = AvatarController(self.pet.width(), self.pet.foot_offset,
                                     AvatarSettings(walk_speed=float(ui.get("walk_speed", 90)),
                                                    cursor_chase=bool(ui.get("cursor_chase", True))))
        self.tracker = WindowTracker(self.bridge)
        self.bubble = SpeechBubble()
        self.backend = BackendClient(int(settings.get("server", "port")), settings.resolve_path("runtime") / "token")
        self.chat = ChatWindow(self.backend)
        self.settings_win: SettingsWindow | None = None
        self.estop = False
        self.hidden_for_fullscreen = False
        self._last_tick = time.monotonic()
        self._last_track = 0.0
        self._last_fs_check = 0.0
        self._last_prank = -1e9
        self._prank_cursor: tuple[int, int] | None = None
        self.frames = 0  # for tests: proves the animation loop keeps running
        self.hotkey = None
        self._visual_offset = [0.0, 0.0]
        self._metrics_at = time.monotonic()
        self._metrics_frames = 0
        self.diagnostics = {}
        import psutil
        self._process = psutil.Process()
        self._process.cpu_percent()
        self._configure_visual(ui)

        self.pet.clicked.connect(self._on_click)
        self.pet.double_clicked.connect(self.open_chat)
        self.pet.context_requested.connect(self._menu)
        self.pet.drag_started.connect(self.ctrl.start_drag)
        self.pet.dragged.connect(self._on_drag)
        self.pet.drag_finished.connect(self._on_drag_end)
        self.backend.state_changed.connect(self._on_conn)
        self.backend.status_received.connect(self._on_status)
        self.backend.event.connect(self._on_event)

        self.tray = QSystemTrayIcon(tray_icon())
        self.tray.setToolTip("MikuOS")
        self.tray.setContextMenu(self._build_menu())
        self.tray.activated.connect(lambda r: self.open_chat() if r == QSystemTrayIcon.Trigger else None)

        self.timer = QTimer()
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(round(1000 / self.fps))
        self.timer.timeout.connect(self.tick)

    @staticmethod
    def _make_pet(kind: str, scale: float, model_px: int, sprite_scale: int = 2):
        """2D sprites when assets/sprites exists, else 3D Miku when the converted model exists
        (tools/build_miku3d.py), else the vector placeholder."""
        if kind in ("auto", "sprite"):
            from .sprite_renderer import available, sprite_window
            if available():
                try:
                    return sprite_window(scale, sprite_scale)
                except Exception:
                    log.exception("sprite avatar failed to load")
            elif kind == "sprite":
                log.warning("sprites not found in assets/sprites; trying the next avatar")
        if kind in ("auto", "3d", "sprite"):
            from .pet3d_window import Pet3DWindow, model_available
            if model_available():
                try:
                    return Pet3DWindow(int(model_px * scale))
                except Exception:
                    log.exception("3D avatar failed to load; using the placeholder")
            elif kind == "3d":
                log.warning("3D model not built (run tools/build_miku3d.py); using the placeholder")
        return PetWindow(PlaceholderRenderer(scale))

    # ---------------------------------------------------------- lifecycle
    def start(self) -> None:
        surfaces, _ = self.tracker.poll()
        self.ctrl.set_surfaces(surfaces)
        wa = self.tracker.work_areas[0]
        self.ctrl.place(wa.x + wa.w - 220, wa.y + wa.h - 1)
        self.ctrl.foot = wa.y + wa.h  # start standing on the taskbar
        self.ctrl.support = next((s for s in surfaces if s.window_id is None and s.y == wa.y + wa.h), None)
        if self.ctrl.support:
            self.ctrl.anim.set(State.IDLE, force=True)
        self.pet.move(*self.ctrl.top_left)
        self.pet.show()
        self.tray.show()
        self.backend.start()
        self.timer.start()
        self._register_hotkey()

    def _register_hotkey(self) -> None:
        from mikuos.hotkey import HotkeyListener
        spec = self.settings.get("hotkey", "emergency_stop") or "ctrl+alt+m"
        self.hotkey = HotkeyListener(spec, lambda: QTimer.singleShot(0, self.emergency_stop), hotkey_id=0x4D4A,
                                     fallbacks=self.settings.get("hotkey", "fallbacks") or [])
        if not self.hotkey.start():
            # usually the backend already owns it; its event reaches us via the WebSocket
            log.info("hotkey held by backend (%s)", self.hotkey.error)

    def quit(self) -> None:
        self.timer.stop()
        self.backend.stop()
        if self.hotkey:
            self.hotkey.stop()
        self.tray.hide()
        self.qapp.quit()

    # --------------------------------------------------------------- loop
    def tick(self) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        self.frames += 1
        self._metrics_frames += 1
        if now - self._metrics_at >= 5:
            self.diagnostics = {'fps': round(self._metrics_frames / (now - self._metrics_at), 1),
                                'rss_mb': round(self._process.memory_info().rss / 1048576, 1),
                                'cpu_percent': self._process.cpu_percent(),
                                'gpu_ms': None}
            log.info('visual diagnostics: %s', self.diagnostics)
            self._metrics_at, self._metrics_frames = now, 0
        try:
            if now - self._last_fs_check > 1.0:
                self._last_fs_check = now
                self._fullscreen_guard()
            if self.hidden_for_fullscreen:
                interval = round(1000 / self.background_fps)
                if self.timer.interval() != interval:
                    self.timer.setInterval(interval)
                return
            interval = round(1000 / (self.background_fps if self.ctrl.anim.state == State.SLEEP else self.fps))
            if self.timer.interval() != interval:
                self.timer.setInterval(interval)
            if now - self._last_track > 0.25:
                self._last_track = now
                surfaces, moves = self.tracker.poll()
                old_x, old_y = self.ctrl.top_left
                self.ctrl.set_surfaces(surfaces, moves)
                new_x, new_y = self.ctrl.top_left
                self._visual_offset[0] += old_x - new_x
                self._visual_offset[1] += old_y - new_y
            cursor = None if self.estop else self._cursor()
            idle = self.bridge.get_user_idle_time()
            self.ctrl.update(dt, cursor, idle)
            self._cursor_prank(cursor)
        except Exception:
            log.exception("tick failed")
        x, y = self.ctrl.top_left
        self._visual_offset = [smooth(v, 0, min(dt, .1), 14) for v in self._visual_offset]
        x, y = round(x + self._visual_offset[0]), round(y + self._visual_offset[1])
        if (self.pet.x(), self.pet.y()) != (x, y):
            self.pet.move(x, y)
        look = None
        if self.ctrl.look_at:
            look = (self.ctrl.look_at[0] - self.ctrl.x, self.ctrl.look_at[1] - (self.ctrl.foot - self.ctrl.h * 0.8))
        a = self.ctrl.anim
        if hasattr(self.pet, 'mixer'):
            self.pet.vertical_velocity = self.ctrl.vy
            self.pet.motion_speed = max(.1, self.ctrl.cfg.walk_speed / (self.pet.model_px * .4096))
            if a.state in (State.RUN, State.CHASE):
                # These cycles are 0.6 s vs Walk's 0.8 s. Combine that with
                # 1.2x playback to match the controller's 1.6x travel speed.
                self.pet.motion_speed *= 1.2
        self.pet.set_view(a.state, a.frame, self.ctrl.facing, look, a.anim_time)
        self.bubble.follow(QPoint(x + self.pet.width() // 2, y + self.pet.head_offset))

    def _cursor(self) -> tuple[float, float]:
        p = QCursor.pos()
        return float(p.x()), float(p.y())

    def _fullscreen_guard(self) -> None:
        fs = False
        try:
            fs = self.bridge.is_fullscreen_foreground()
        except Exception:
            pass
        if fs and not self.hidden_for_fullscreen:
            self.hidden_for_fullscreen = True
            self.pet.hide()
            self.bubble.hide()
        elif not fs and self.hidden_for_fullscreen:
            self.hidden_for_fullscreen = False
            self.pet.show()

    def _cursor_prank_allowed(self) -> bool:
        """Real cursor movement needs the user's consent (cursor_control=allow, off by
        default, confirmed in Settings) and is never done while a mouse button is held,
        in full-screen apps, offline or during emergency stop."""
        return (not self.estop and not self.hidden_for_fullscreen and self.prank_enabled
                and self.backend.state != ConnState.BACKEND_OFFLINE
                and (self.backend.status.get("cursor_control") or "deny") == "allow"
                and not self.bridge.mouse_button_down())

    def _cursor_prank(self, cursor) -> None:
        """Grab the cursor when she catches it and run away with it for a moment.
        A sharp pull of the mouse frees it; she releases it on her own after a few seconds."""
        c = self.ctrl
        if cursor is None:
            return
        if c.pranking:
            if not self._cursor_prank_allowed():
                c.end_prank("interrupted")
            elif self._prank_cursor and math.dist(cursor, self._prank_cursor) > c.cfg.prank_escape_px:
                c.end_prank("escaped")
            else:
                g = QApplication.primaryScreen().virtualGeometry()
                hx = min(max(round(c.hand[0]), g.left()), g.right())
                hy = min(max(round(c.hand[1]), g.top()), g.bottom())
                self.bridge.set_cursor_position(hx, hy)
                self._prank_cursor = (hx, hy)
                return
            log.info("cursor prank ended: %s", c.prank_result)
            self._prank_cursor = None
            return
        self._prank_cursor = None
        now = time.monotonic()
        if (c.anim.state != State.CHASE or now - self._last_prank < self.prank_cooldown_s
                or math.dist(cursor, c.hand) > c.w * 0.3):
            return
        # caught it: one roll per catch, then wait a little before rolling again
        self._last_prank = now - self.prank_cooldown_s + 8
        if c.rng.random() < self.prank_chance and self._cursor_prank_allowed() and c.start_prank():
            self._last_prank = now
            log.info("cursor prank: Miku grabbed the cursor")

    # ------------------------------------------------------------- events
    def _on_click(self) -> None:
        if self.estop:
            return
        self.ctrl.poke()
        self.backend.send_pet_event("click")

    def _on_drag(self, gp: QPoint) -> None:
        self.ctrl.drag_to(gp.x(), gp.y() + self.ctrl.h * 0.45)

    def _on_drag_end(self) -> None:
        self.ctrl.end_drag()
        self.backend.send_pet_event("drag")

    def _on_conn(self, st: str) -> None:
        self.pet.set_conn(st)
        self.tray.setToolTip({ConnState.ONLINE: "MikuOS — онлайн", ConnState.LLM_DOWN: "MikuOS — LM Studio недоступна",
                              ConnState.BACKEND_OFFLINE: "MikuOS — backend не запущен"}[st])

    def _on_status(self, st: dict) -> None:
        self.pet.observing = bool(st.get("observation", {}).get("active"))
        self._apply_estop(bool(st.get("emergency_stop")))
        self.backend.get("/api/settings", self._on_settings)

    def _on_settings(self, s) -> None:
        if isinstance(s, dict) and "permissions" in s:
            self.backend.status["cursor_control"] = s["permissions"].get("cursor_control", "deny")
            self.ctrl.cfg.cursor_chase = bool(s.get("ui", {}).get("cursor_chase", True))
            self._configure_visual(s.get('ui', {}))

    def _configure_visual(self, ui):
        self.fps = finite(ui.get('fps', 30), 30, 10, 60)
        self.background_fps = finite(ui.get('background_fps', 10), 10, 5, self.fps)
        self.ctrl.cfg.walk_speed = finite(ui.get('walk_speed', 90), 90, 20, 240)
        self.ctrl.cfg.idle_cooldown_s = finite(ui.get('idle_cooldown_s', 4), 4, 1, 120)
        self.prank_enabled = bool(ui.get('cursor_prank', True))
        self.prank_cooldown_s = finite(ui.get('cursor_prank_cooldown_s', 120), 120, 20, 3600)
        self.prank_chance = finite(ui.get('cursor_prank_chance', 0.5), 0.5, 0, 1)
        for key in ('walk_probability', 'jump_probability', 'sit_probability'):
            setattr(self.ctrl.cfg, key, finite(ui.get(key, getattr(self.ctrl.cfg, key))))
        if hasattr(self.pet, 'hair'):
            self.pet.hair.enabled = bool(ui.get('hair_physics', True))
            self.pet.hair.intensity = finite(ui.get('hair_intensity', 1), 1, 0, 2)
            self.pet.root.setProperty('economy', ui.get('quality') == 'economy')
            self.pet._mask_timer.setInterval(350 if ui.get('quality') == 'economy' else 200)

    def _on_event(self, ev: dict) -> None:
        t, d = ev.get("type"), ev.get("data", {})
        if not isinstance(d, dict):
            return
        if t in ('character_command', 'emotion', 'voice_playback'):
            if self.estop:
                return
            if t == 'voice_playback' and hasattr(self.pet, 'voice'):
                self.pet.voice.event(d)
                if d.get('event') in ('start', 'energy'):
                    self._hold_still_while_speaking()
            else:
                if hasattr(self.pet, 'emotion'):
                    self.pet.emotion.command(d)
                if 'action' in d or 'animation' in d:
                    self.ctrl.command(d)
            return
        if t in ("initiative_message", "assistant_message") and d.get("text") and not self.estop:
            self.bubble.say(d["text"], QPoint(self.pet.x() + self.pet.width() // 2, self.pet.y() + self.pet.head_offset))
            if hasattr(self.pet, "talk"):
                self.pet.talk(min(8.0, 0.06 * len(d["text"])))
        elif t == "voice_state":
            st = d.get("state")
            self.pet.listening = st == "listening"
            self.pet.update()
            pos = QPoint(self.pet.x() + self.pet.width() // 2, self.pet.y() + self.pet.head_offset)
            if st == "listening":
                self.bubble.say("Слушаю… 🎤", pos)
            elif st in ("recognizing", "thinking"):
                self.bubble.say("…", pos)
            elif st == "idle" and d.get("result") == "no_speech":
                self.bubble.say("Не расслышала…", pos)
        elif t == "emergency_stop":
            self._apply_estop(bool(d.get("active")))
        elif t == "settings_changed":
            self.backend.get("/api/settings", self._on_settings)

    def _hold_still_while_speaking(self) -> None:
        """Talking Miku stops walking (the walk cycle has no mouth frames) and does not
        start a new wander until she has finished the phrase."""
        c = self.ctrl
        if c.anim.state in (State.WALK, State.RUN) and c.support is not None and not c.pranking:
            c.target_x = None
            c.anim.set(State.IDLE, force=True)
        c.decision_timer = max(c.decision_timer, 1.5)

    def _apply_estop(self, active: bool) -> None:
        self.estop = active
        self.pet.estop = active
        self.pet.set_passive(active)
        self.ctrl.paused = active
        if active:
            self.ctrl.end_prank("interrupted")
            self.ctrl.pending_action = None
            if hasattr(self.pet, 'voice'):
                self.pet.voice.active = False
        self.ctrl.cfg.cursor_chase = self.ctrl.cfg.cursor_chase and not active
        if active:
            self.bubble.hide()
            self.backend.status["cursor_control"] = "deny"

    def emergency_stop(self) -> None:
        self._apply_estop(True)
        self.backend.post("/api/emergency_stop", {})

    def resume(self) -> None:
        self.backend.post("/api/resume", {})
        self._apply_estop(False)

    # --------------------------------------------------------------- menus
    def _build_menu(self) -> QMenu:
        m = QMenu()
        m.addAction("Открыть чат", self.open_chat)
        vhk = (self.backend.status.get("voice") or {}).get("hotkey_active")
        m.addAction("🎤 Сказать голосом" + (f" ({vhk})" if vhk else ""),
                    lambda: self.backend.post("/api/voice/toggle", {}))
        m.addAction("Настройки…", self.open_settings)
        modes = m.addMenu("Инициативность")
        for key, label in [("chatty", "Болтливая"), ("normal", "Обычная"), ("quiet", "Тихая"),
                           ("dnd", "Не беспокоить")]:
            act = QAction(label, modes)
            act.triggered.connect(lambda _=False, k=key: self.backend.patch("/api/settings",
                                                                            {"initiative": {"mode": k}}))
            modes.addAction(act)
        m.addAction("Наблюдение вкл/выкл", self._toggle_observation)
        m.addAction("Спать", lambda: self.ctrl.command({'action': 'sleep'}))
        m.addSeparator()
        hk = (self.backend.status.get("hotkey") or {}).get("active") or "Ctrl+Alt+M"
        m.addAction(f"⛔ Аварийная остановка ({hk})", self.emergency_stop)
        m.addAction("Снять аварийную остановку", self.resume)
        m.addSeparator()
        m.addAction("Выход", self.quit)
        return m

    def _menu(self, pos: QPoint) -> None:
        self._build_menu().exec(pos)

    def _toggle_observation(self) -> None:
        def flip(s):
            if isinstance(s, dict) and "observation" in s:
                self.backend.patch("/api/settings", {"observation": {"enabled": not s["observation"]["enabled"]}})
        self.backend.get("/api/settings", flip)

    def open_chat(self) -> None:
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()

    def open_settings(self) -> None:
        self.settings_win = SettingsWindow(self.backend)
        self.settings_win.show()
