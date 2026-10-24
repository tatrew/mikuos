"""AvatarController: movement and behaviour of the pet. Pure Python, frame-driven.

The LLM never drives motion: everything here is deterministic rules + randomness.
Coordinates are physical screen pixels; (x, foot_y) is the bottom-centre of the avatar.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .animation import AnimationStateMachine, State
from .surface_detector import Surface, SurfaceDetector

GRAVITY = 2200.0  # px/s^2
MAX_FALL = 1800.0


@dataclass
class Settings:
    walk_speed: float = 90.0
    cursor_chase: bool = True
    look_radius: float = 260.0
    chase_radius: float = 160.0
    sleep_after_idle_s: float = 300.0
    idle_cooldown_s: float = 4.0
    walk_probability: float = 0.45
    jump_probability: float = 0.15
    sit_probability: float = 0.20


class AvatarController:
    def __init__(self, width: int, height: int, settings: Settings | None = None, rng: random.Random | None = None):
        self.w, self.h = width, height
        self.cfg = settings or Settings()
        self.rng = rng or random.Random()
        self.anim = AnimationStateMachine()
        self.detector = SurfaceDetector()
        self.x = 300.0
        self.foot = 100.0
        self.vx = 0.0
        self.vy = 0.0
        self.facing = 1  # 1 right, -1 left
        self.support: Surface | None = None
        self.target_x: float | None = None
        self.look_at: tuple[float, float] | None = None
        self.decision_timer = 1.0
        self.surfaces: list[Surface] = []
        self.paused = False  # emergency stop: freeze in place, no interaction
        self.pending_action: dict | None = None
        self.pending_age = 0.0

    # -------------------------------------------------------------- inputs
    def set_surfaces(self, surfaces: list[Surface], window_moves: dict[int, tuple[int, int]] | None = None) -> None:
        """Update surfaces; if the window we stand on moved, move with it."""
        if self.support and self.support.window_id is not None and window_moves:
            d = window_moves.get(self.support.window_id)
            if d and self.anim.state != State.DRAG:
                self.x += d[0]
                self.foot += d[1]
        self.surfaces = surfaces
        if self.support is not None and self.anim.state not in (State.DRAG, State.JUMP):
            same = [s for s in surfaces if s.window_id == self.support.window_id and s.contains_x(self.x, 4)
                    and abs(s.y - self.foot) <= 40]
            if same:
                self.support = min(same, key=lambda s: abs(s.y - self.foot))
                self.foot = self.support.y
            else:
                self._start_fall()  # window closed / minimized / covered

    def place(self, x: float, foot: float) -> None:
        self.x, self.foot = x, foot
        self._start_fall()

    def start_drag(self) -> None:
        self.anim.set(State.DRAG, force=True)
        self.support = None
        self.vx = self.vy = 0

    def drag_to(self, x: float, foot: float) -> None:
        if self.anim.state == State.DRAG:
            self.x, self.foot = x, foot

    def end_drag(self) -> None:
        if self.anim.state == State.DRAG:
            self._start_fall()

    def poke(self) -> None:
        if self.anim.state == State.SLEEP:
            self.anim.set(State.STAND_UP)
        elif self.anim.state == State.SITTING_IDLE:
            self.anim.set(State.STAND_UP)
        elif self.support and self.anim.state not in (State.SIT, State.STAND_UP, State.LAND):
            self.anim.set(State.HAPPY)

    def command(self, data: dict) -> bool:
        """Validate high-level requests. Airborne requests wait for safe landing."""
        if not isinstance(data, dict) or self.paused:
            return False
        action = data.get('action', data.get('animation'))
        allowed = {'idle', 'walk', 'run', 'wave', 'think', 'look_around', 'happy', 'sad', 'surprised', 'sleep', 'sit_on_window'}
        if action not in allowed:
            return False
        if action == 'sit_on_window' and not isinstance(data.get('target_window_id'), int):
            return False
        self.pending_action = dict(data, action=action)
        self.pending_age = 0
        return True

    def _start_fall(self) -> None:
        self.support = None
        self.vy = 0.0
        self.anim.set(State.FALL, force=True)

    # --------------------------------------------------------------- tick
    def update(self, dt: float, cursor: tuple[float, float] | None = None, user_idle_s: float = 0.0) -> None:
        dt = min(dt, 0.1)
        if self.paused or self.anim.state == State.DRAG:
            return
        self.anim.update(dt)
        if self.pending_action:
            self.pending_age += dt
            if self.pending_age > 15:
                self.pending_action = None
        st = self.anim.state
        if st in (State.FALL, State.JUMP) or self.support is None:
            self._physics(dt)
            return
        if st in (State.SIT, State.STAND_UP, State.LAND):
            return
        if self.pending_action:
            if st in (State.SITTING_IDLE, State.SLEEP):
                self.anim.set(State.STAND_UP)
                return
            command, self.pending_action = self.pending_action, None
            if command['action'] == 'sit_on_window':
                target = next((s for s in self.surfaces if s.window_id == command['target_window_id']
                               and s.width > self.w and abs(s.y - self.foot) < 420
                               and abs((s.x1 + s.x2) / 2 - self.x) < 700), None)
                if target == self.support:
                    self.anim.set(State.SIT)
                elif target:
                    self.pending_action = command
                    self._jump_to_other_surface(target)
            else:
                action = command['action']
                if action in ('walk', 'run'):
                    s = self.support
                    if s.width > self.w:
                        self.target_x = self.rng.uniform(s.x1 + self.w / 2, s.x2 - self.w / 2)
                        self.anim.set(State(action))
                elif action == 'sleep':
                    self.anim.set(State.IDLE)
                    self.anim.set(State.SLEEP)
                else:
                    self.anim.set(State(action))
            return
        if st in (State.WAVE, State.THINK, State.LOOK_AROUND, State.HAPPY, State.SAD, State.SURPRISED):
            return
        self.decision_timer -= dt
        head = (self.x, self.foot - self.h * 0.8)
        near = cursor is not None and math.dist(cursor, head) < self.cfg.look_radius
        if st == State.SLEEP:
            if near and cursor and math.dist(cursor, head) < 80:
                self.anim.set(State.STAND_UP)
            return
        if user_idle_s >= self.cfg.sleep_after_idle_s and st in (State.IDLE, State.SITTING_IDLE):
            self.anim.set(State.SLEEP)
            return
        if st == State.SITTING_IDLE:
            self.look_at = cursor if near else None
            if self.decision_timer <= 0:
                self.anim.set(State.STAND_UP)
            return
        if near and cursor:
            self.look_at = cursor
            # Eye motion may cross the centreline freely; do not swivel the
            # whole body back and forth for a few pixels of cursor movement.
            if abs(cursor[0] - self.x) > 25:
                self.facing = 1 if cursor[0] >= self.x else -1
            close = math.dist(cursor, head) < self.cfg.chase_radius
            if self.cfg.cursor_chase and close and abs(cursor[1] - head[1]) < self.h:
                self.anim.set(State.CHASE)
                if self._walk_toward(cursor[0], dt, speed_mult=1.6, stop_at=self.w * 0.3):
                    self.anim.set(State.LOOK)
                return
            if st in (State.IDLE, State.WALK, State.RUN, State.CHASE, State.SIT):
                self.anim.set(State.LOOK)
            return
        self.look_at = None
        if st in (State.LOOK, State.CHASE):
            self.anim.set(State.IDLE)
        if st == State.HAPPY:
            return
        if st in (State.WALK, State.RUN) and self.target_x is not None:
            if self._walk_toward(self.target_x, dt, speed_mult=1.6 if st == State.RUN else 1):
                self.target_x = None
                self.anim.set(State.IDLE)
            return
        if self.decision_timer <= 0:
            self._decide()

    def _decide(self) -> None:
        self.decision_timer = self.cfg.idle_cooldown_s + self.rng.uniform(0.0, 4.0)
        s = self.support
        r = self.rng.random()
        if s is None:
            return
        if r < self.cfg.walk_probability and s.width > self.w:
            lo, hi = s.x1 + self.w / 2, s.x2 - self.w / 2
            self.target_x = self.rng.uniform(lo, hi)
            self.facing = 1 if self.target_x > self.x else -1
            self.anim.set(State.WALK)
        elif r < self.cfg.walk_probability + self.cfg.jump_probability:
            self._jump_to_other_surface()
        elif r < self.cfg.walk_probability + self.cfg.jump_probability + self.cfg.sit_probability:
            self.anim.set(State.SIT)
        else:
            self.anim.set(self.rng.choice((State.IDLE, State.LOOK_AROUND, State.THINK, State.WAVE)))

    def _walk_toward(self, tx: float, dt: float, speed_mult: float = 1.0, stop_at: float = 2.0) -> bool:
        s = self.support
        if s is None:
            return True
        lo, hi = s.x1 + self.w * 0.25, s.x2 - self.w * 0.25
        tx = min(max(tx, lo), hi)
        dx = tx - self.x
        if abs(dx) <= stop_at:
            return True
        self.facing = 1 if dx > 0 else -1
        step = self.cfg.walk_speed * speed_mult * dt
        self.x += max(-step, min(step, dx))
        return False

    def _jump_to_other_surface(self, requested: Surface | None = None) -> None:
        options = [s for s in self.surfaces if s != self.support and abs(s.y - self.foot) < 420
                   and s.width > self.w and abs((s.x1 + s.x2) / 2 - self.x) < 700]
        if not options:
            self.anim.set(State.SIT)
            return
        target = requested or self.rng.choice(options)
        tx = min(max(self.x, target.x1 + self.w / 2), target.x2 - self.w / 2)
        if abs(tx - self.x) < 1:
            tx = self.rng.uniform(target.x1 + self.w / 2, target.x2 - self.w / 2)
        rise = max(80.0, self.foot - target.y + 80.0)
        vy = -math.sqrt(2 * GRAVITY * rise)
        t_up = -vy / GRAVITY
        t_down = math.sqrt(2 * max(1.0, rise - (self.foot - target.y)) / GRAVITY)
        self.vx = (tx - self.x) / (t_up + t_down)
        self.vy = vy
        self.facing = 1 if self.vx >= 0 else -1
        self.support = None
        self.anim.set(State.JUMP)

    def _physics(self, dt: float) -> None:
        prev = self.foot
        self.vy = min(self.vy + GRAVITY * dt, MAX_FALL)
        self.x += self.vx * dt
        self.foot += self.vy * dt
        if self.vy > 0:
            if self.anim.state == State.JUMP:
                self.anim.set(State.FALL, force=True)
            land = SurfaceDetector.landing(self.surfaces, self.x, prev, self.foot)
            if land:
                self.foot = land.y
                self.support = land
                self.vx = self.vy = 0.0
                self.anim.set(State.LAND, force=True)
                self.decision_timer = self.rng.uniform(0.5, 2.0)
                return
        floor = max((s.y for s in self.surfaces if s.window_id is None), default=None)
        if floor is not None and self.foot > floor + 400:
            # fell off every monitor: respawn on the nearest floor
            fl = min((s for s in self.surfaces if s.window_id is None), key=lambda s: abs((s.x1 + s.x2) / 2 - self.x))
            self.x = min(max(self.x, fl.x1 + self.w), fl.x2 - self.w)
            self.foot = fl.y - 300
            self.vx = self.vy = 0.0

    @property
    def top_left(self) -> tuple[int, int]:
        return int(round(self.x - self.w / 2)), int(round(self.foot - self.h))
