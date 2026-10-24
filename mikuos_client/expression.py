"""High-level expressions and final-playback audio envelope. No LLM bone access."""
from __future__ import annotations

import math


def smooth(current, target, dt, rate=9):
    return current + (target - current) * (1 - math.exp(-rate * max(0, dt)))


def finite(value, default=0.0, low=0.0, high=1.0):
    try:
        value = float(value)
        return max(low, min(high, value)) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


class EmotionController:
    # eye happy, mouth happy, eye sad, mouth sad, eyelid closure
    PRESETS = {'neutral': (0, 0, 0, 0, 0), 'happy': (.7, .8, 0, 0, 0),
               'excited': (1, 1, 0, 0, 0), 'shy': (.2, .3, .2, 0, .2),
               'curious': (0, .15, 0, 0, 0), 'confused': (0, 0, .4, .2, 0),
               'surprised': (0, 0, 0, 0, 0), 'sad': (0, 0, .85, .7, .1),
               'sleepy': (0, 0, .2, 0, .65), 'thoughtful': (0, 0, .25, 0, .15)}

    def __init__(self):
        self.emotion = 'neutral'
        self.intensity = 0.0
        self.remaining = 0.0
        self.values = [0.0] * 5

    def command(self, data):
        if not isinstance(data, dict) or data.get('emotion') not in self.PRESETS:
            return False
        self.emotion = data['emotion']
        self.intensity = finite(data.get('intensity', 1), 1)
        self.remaining = finite(data.get('duration', 2.5), 2.5, 0, 60)
        return True

    def update(self, dt):
        self.remaining = max(0, self.remaining - dt)
        target = self.PRESETS[self.emotion] if self.remaining else self.PRESETS['neutral']
        self.values = [smooth(v, t * self.intensity, dt) for v, t in zip(self.values, target)]
        return self.values


class VoiceAnimationController:
    """Feed the samples actually being played AFTER RVC, or their RMS envelope.

    stream_id prevents delayed packets from a previous utterance reopening the
    mouth. The playback owner sends start/energy/end; a stale stream times out.
    """
    def __init__(self):
        self.stream_id = None
        self.energy = 0.0
        self.age = 0.0
        self.mouth = 0.0
        self.active = False

    def event(self, data):
        if not isinstance(data, dict):
            return False
        kind, sid = data.get('event'), data.get('stream_id')
        if not isinstance(sid, str) or not sid or len(sid) > 128:
            return False
        if kind == 'start':
            self.stream_id, self.active, self.age, self.energy = sid, True, 0.0, 0.0
        elif sid != self.stream_id or not self.active:
            return False
        elif kind == 'energy':
            self.energy = finite(data.get('rms'))
            self.age = 0.0
        elif kind == 'end':
            self.active, self.energy = False, 0.0
        else:
            return False
        return True

    def feed_pcm(self, samples, stream_id):
        # Normalized mono/multichannel float samples from the playback callback.
        if not samples:
            return
        rms = math.sqrt(sum(finite(x, 0, -1, 1) ** 2 for x in samples) / len(samples))
        self.event({'event': 'energy', 'stream_id': stream_id, 'rms': rms})

    def update(self, dt):
        self.age += dt
        if self.age > 0.3:
            self.energy = 0.0
        if self.age > 2.0:
            self.active = False
        target = min(1, max(0, self.energy - .015) * 5) if self.active else 0
        self.mouth = smooth(self.mouth, target, dt, 22 if target > self.mouth else 12)
        return self.mouth


class HairPhysicsController:
    """Bounded damped angular spring, substepped independently of render FPS.

    Small cone limits reduce penetration; this is NOT mesh collision physics.
    Only the first two joints of each tail and skirt roots receive offsets.
    """
    def __init__(self):
        self.angle = self.velocity = 0.0
        self.enabled = True
        self.intensity = 1.0

    def update(self, dt, yaw_velocity, vertical_velocity):
        target = max(-8, min(8, -yaw_velocity * .025 + vertical_velocity * .002))
        target *= self.intensity if self.enabled else 0
        dt = max(0, min(.1, dt))
        steps = max(1, math.ceil(dt * 120))
        h = dt / steps
        for _ in range(steps):
            self.velocity += (70 * (target - self.angle) - 16 * self.velocity) * h
            self.angle = max(-10, min(10, self.angle + self.velocity * h))
        return self.angle
