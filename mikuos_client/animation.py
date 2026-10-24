"""AnimationStateMachine: pure-Python avatar states, no Qt and no LLM."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    IDLE = "idle"
    WALK = "walk"
    JUMP = "jump"
    FALL = "fall"
    SIT = "sit"
    LOOK = "look"
    SLEEP = "sleep"
    DRAG = "drag"
    CHASE = "chase"  # tries to catch the cursor (animation only)
    HAPPY = "happy"  # reaction to a click
    RUN = "run"
    LAND = "land"
    SITTING_IDLE = "sitting_idle"
    STAND_UP = "stand_up"
    WAVE = "wave"
    THINK = "think"
    LOOK_AROUND = "look_around"
    SURPRISED = "surprised"
    SAD = "sad"


# which transitions are legal; anything else is ignored (keeps behaviour sane)
ALLOWED: dict[State, set[State]] = {
    State.IDLE: {State.WALK, State.JUMP, State.FALL, State.SIT, State.LOOK, State.SLEEP, State.DRAG, State.CHASE,
                 State.HAPPY},
    State.WALK: {State.IDLE, State.JUMP, State.FALL, State.SIT, State.LOOK, State.DRAG, State.CHASE, State.HAPPY},
    State.JUMP: {State.FALL, State.IDLE, State.DRAG},
    State.FALL: {State.IDLE, State.SIT, State.DRAG},
    State.SIT: {State.IDLE, State.WALK, State.FALL, State.LOOK, State.SLEEP, State.DRAG, State.HAPPY, State.JUMP},
    State.LOOK: {State.IDLE, State.WALK, State.FALL, State.SIT, State.DRAG, State.CHASE, State.HAPPY, State.JUMP},
    State.SLEEP: {State.IDLE, State.FALL, State.DRAG, State.HAPPY, State.LOOK},
    State.DRAG: {State.FALL, State.IDLE},
    State.CHASE: {State.IDLE, State.LOOK, State.FALL, State.DRAG, State.JUMP, State.HAPPY},
    State.HAPPY: {State.IDLE, State.FALL, State.DRAG, State.SIT},
}
FPS = {State.IDLE: 2, State.WALK: 8, State.JUMP: 6, State.FALL: 6, State.SIT: 1, State.LOOK: 2, State.SLEEP: 1,
       State.DRAG: 6, State.CHASE: 10, State.HAPPY: 8}
FRAMES = {State.IDLE: 2, State.WALK: 4, State.JUMP: 2, State.FALL: 2, State.SIT: 2, State.LOOK: 2, State.SLEEP: 2,
          State.DRAG: 2, State.CHASE: 4, State.HAPPY: 4}
# states that end on their own after a duration (seconds)
TIMED = {State.HAPPY: 1.2}
TIMED.update({State.SIT: 0.6, State.STAND_UP: 0.6, State.LAND: 0.4,
              State.WAVE: 2.0, State.SURPRISED: 1.0, State.THINK: 3.0,
              State.LOOK_AROUND: 3.0, State.SAD: 3.0})
FOLLOW_UP = {State.SIT: State.SITTING_IDLE, State.STAND_UP: State.IDLE}
GROUNDED = {State.IDLE, State.WALK, State.RUN, State.LOOK, State.CHASE,
            State.WAVE, State.HAPPY, State.THINK, State.LOOK_AROUND, State.SAD, State.SURPRISED}
for state in State:
    FPS.setdefault(state, 8)
    FRAMES.setdefault(state, 4)
    ALLOWED.setdefault(state, {State.IDLE, State.FALL, State.DRAG})
    if state in GROUNDED:
        ALLOWED[state].update(GROUNDED | {State.SIT, State.JUMP})
ALLOWED[State.FALL].add(State.LAND)
ALLOWED[State.JUMP].add(State.LAND)
ALLOWED[State.SIT].add(State.SITTING_IDLE)
ALLOWED[State.SITTING_IDLE] = {State.STAND_UP, State.FALL, State.DRAG, State.SLEEP}
ALLOWED[State.STAND_UP] = {State.IDLE, State.FALL, State.DRAG}
ALLOWED[State.SLEEP].add(State.STAND_UP)


@dataclass
class AnimationStateMachine:
    state: State = State.FALL
    time_in_state: float = 0.0
    anim_time: float = 0.0
    history: list[State] = field(default_factory=list)

    def set(self, new: State, force: bool = False) -> bool:
        if new == self.state:
            return False
        if not force and new not in ALLOWED[self.state]:
            return False
        self.history.append(self.state)
        del self.history[:-20]
        self.state = new
        self.time_in_state = 0.0
        self.anim_time = 0.0
        return True

    def update(self, dt: float) -> None:
        self.time_in_state += dt
        self.anim_time += dt
        limit = TIMED.get(self.state)
        if limit and self.time_in_state >= limit:
            self.set(FOLLOW_UP.get(self.state, State.IDLE), force=True)

    @property
    def frame(self) -> int:
        return int(self.anim_time * FPS[self.state]) % FRAMES[self.state]
