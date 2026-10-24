"""Pet3DWindow: the real 3D Miku (Qt Quick 3D) in the same transparent,
always-on-top, click-through pet window used by the overlay.

The 3D scene is rendered offscreen by a QQuickWidget (reliable per-pixel alpha on
Windows); the window mask is rebuilt from the rendered alpha a few times per
second, so clicks outside Miku's silhouette pass through to the desktop.
Model assets are produced by tools/build_miku3d.py.
"""
from __future__ import annotations

import logging
import math
import random
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Qt
from PySide6.QtGui import QBitmap, QRegion, QQuaternion, QVector3D
from PySide6.QtQuickWidgets import QQuickWidget

from mikuos.config import PROJECT_ROOT

from .animation import State
from .backend_client import ConnState
from .pet_window import PetWindow
from .pose_mixer import ClipLibrary, PoseMixer
from .expression import EmotionController, VoiceAnimationController, HairPhysicsController, smooth
from .cursor_interaction import CursorInteractionController

log = logging.getLogger("mikuos.client.pet3d")

QML = Path(__file__).resolve().parent / "qml" / "Pet3D.qml"
MODEL_QML = PROJECT_ROOT / "assets" / "miku3d" / "qml" / "Miku.qml"

ANIM = {State.IDLE: "idle", State.WALK: "walk", State.JUMP: "jump", State.FALL: "fall", State.SIT: "sit",
        State.LOOK: "idle", State.SLEEP: "sleep", State.DRAG: "drag", State.CHASE: "chase",
        State.HAPPY: "happy"}
ANIM.update({s: s.value for s in (State.RUN, State.LAND, State.SITTING_IDLE, State.STAND_UP,
                                State.WAVE, State.THINK, State.LOOK_AROUND, State.SURPRISED, State.SAD)})
DOT = {ConnState.ONLINE: "#2ecc71", ConnState.LLM_DOWN: "#f1c40f", ConnState.BACKEND_OFFLINE: "#e74c3c"}
SIT_DROP = 0.47  # hip height as a fraction of Miku's height: sit with legs over the edge


def model_available() -> bool:
    return MODEL_QML.exists()


class _Size:
    """Minimal renderer stand-in so PetWindow can size itself."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h

    def size(self) -> tuple[int, int]:
        return self.w, self.h

    def paint(self, *a) -> None:
        pass


class Pet3DWindow(PetWindow):
    def __init__(self, model_px: int = 220):
        self.model_px = model_px
        self.foot_y = int(model_px * 1.25)  # room above the head for raised arms
        self.foot_offset = self.foot_y
        self.head_offset = self.foot_y - model_px
        w, h = int(model_px * 0.78), int(model_px * 1.62)  # below the foot line: legs dangling when sitting
        super().__init__(_Size(w, h))
        self.quick = QQuickWidget(self)
        self.quick.setAttribute(Qt.WA_AlwaysStackOnTop, True)
        self.quick.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # PetWindow handles the mouse
        self.quick.setClearColor(Qt.transparent)
        self.quick.setResizeMode(QQuickWidget.SizeRootObjectToView)
        self.quick.setGeometry(0, 0, w, h)
        self.quick.setSource(QUrl.fromLocalFile(str(QML)))
        if self.quick.status() != QQuickWidget.Ready:
            errs = "; ".join(e.toString() for e in self.quick.errors())
            raise RuntimeError(f"3D model failed to load: {errs}")
        self.root = self.quick.rootObject()
        self.asset = self.root.findChild(QObject, 'Root')
        if self.asset is None or not self.asset.setProperty('externalAnimation', True):
            raise RuntimeError('Rebuild the model: external animation interface is missing')
        self.library = ClipLibrary(PROJECT_ROOT / 'assets/miku3d/miku.glb')
        self.mixer = PoseMixer(self.library)
        self.joints = {}
        for node, prop in self.library.rest:
            joint = self.asset.findChild(QObject, self.library.names[node])
            if joint is None:
                raise RuntimeError('Missing exported joint: ' + self.library.names[node])
            self.joints[node] = joint
        self.emotion = EmotionController()
        self.voice = VoiceAnimationController()
        self.hair = HairPhysicsController()
        self.cursor_controller = CursorInteractionController(self.asset)
        self._gaze = [0.0, 0.0]
        self._drop = 0.0
        self._drop_source = 0.0
        self.vertical_velocity = 0.0
        self.motion_speed = 1.0
        self._last_joint_values = {}
        self.root.setProperty("modelPx", float(model_px))
        self.root.setProperty("footY", float(self.foot_y))
        self.yaw = 0.0
        self._last = time.monotonic()
        self._blink_at = time.monotonic() + random.uniform(2, 5)
        self._talk_until = 0.0
        self._mask_timer = QTimer(self)
        self._mask_timer.setInterval(200)
        self._mask_timer.timeout.connect(self._update_mask_now)
        self._mask_timer.start()

    # PetWindow draws with QPainter; here Qt Quick does the drawing
    def paintEvent(self, _):
        pass

    def _update_mask(self) -> None:
        pass  # handled by the timer (framebuffer read-back is not per-frame cheap)

    def _update_mask_now(self) -> None:
        if not self.isVisible():
            return
        img = self.quick.grabFramebuffer()
        if img.isNull():
            return
        base = QRegion(QBitmap.fromImage(img.createAlphaMask()))
        # dilate a little so limbs moving between two mask updates are never clipped
        region = QRegion(base)
        for dx, dy in ((-8, 0), (8, 0), (0, -8), (0, 8)):
            region = region.united(base.translated(dx, dy))
        region = region.united(QRegion(self.width() - 36, 0, 36, 18))  # status indicators stay clickable
        self.setMask(region)

    def talk(self, seconds: float) -> None:
        """Text bubbles may cue gestures, never pretend to be audio lip sync."""
        self._talk_until = max(self._talk_until, time.monotonic() + seconds)

    def set_view(self, state: State, frame: int, facing: int, look, t: float) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        if self.estop:
            dt = 0
        self.state = state
        moving = state in (State.WALK, State.RUN, State.CHASE, State.JUMP, State.FALL)
        target = -55.0 * facing if moving else -12.0 * facing  # +yaw turns her front to screen-left
        previous_yaw = self.yaw
        self.yaw = smooth(self.yaw, target, dt, 7)
        r = self.root
        r.setProperty("animState", ANIM.get(state, "idle"))
        r.setProperty("yaw", self.yaw)
        clip = ANIM.get(state, 'idle')
        if clip != self.mixer.name:
            self._drop_source = self._drop
        pose = self.mixer.update(clip, dt, self.motion_speed if moving else 1)
        seated = state in (State.SITTING_IDLE, State.SLEEP)
        progress = min(1, self.mixer.time / .6)
        progress = progress * progress * (3 - 2 * progress)
        drop = SIT_DROP if seated else 0
        if state == State.SIT:
            drop = SIT_DROP * progress
        elif state == State.STAND_UP:
            drop = SIT_DROP * (1 - progress)
        blend = min(1, self.mixer.elapsed / self.mixer.duration)
        blend = blend * blend * (3 - 2 * blend)
        self._drop = self._drop_source + (drop - self._drop_source) * blend
        r.setProperty('drop', self._drop)
        hair_angle = self.hair.update(dt, (self.yaw - previous_yaw) / max(dt, .001), self.vertical_velocity)
        for (node, prop), value in pose.items():
            joint = self.joints[node]
            name = self.library.names[node]
            layered = name in ('Neck', 'Right_Arm', 'Right_Elbow', 'Right_Wrist',
                               'EX_Left_BS_hair_01', 'EX_Right_BS_hair_01',
                               'EX_Left_BS_hair_02', 'EX_Right_BS_hair_02',
                               'EX_Back_skirt_01', 'EX_Center_skirt_01')
            if not layered and self._last_joint_values.get((node, prop)) == value:
                continue
            self._last_joint_values[node, prop] = value
            if prop == 'rotation':
                q = QQuaternion(value[3], value[0], value[1], value[2])
                if name == 'Neck' and state not in (State.SLEEP, State.DRAG):
                    q *= QQuaternion.fromEulerAngles(self._gaze[1] * 5, self._gaze[0] * 12, 0)
                elif name in ('EX_Left_BS_hair_01', 'EX_Right_BS_hair_01',
                              'EX_Left_BS_hair_02', 'EX_Right_BS_hair_02'):
                    q *= QQuaternion.fromEulerAngles(hair_angle * .5, 0, hair_angle * .25)
                elif name in ('EX_Back_skirt_01', 'EX_Center_skirt_01'):
                    q *= QQuaternion.fromEulerAngles(hair_angle * .15, 0, 0)
                joint.setProperty('rotation', q)
            else:
                joint.setProperty('position' if prop == 'translation' else prop, QVector3D(*value))
        self.cursor_controller.update(dt, look, self.model_px, state in (State.IDLE, State.LOOK))
        # blinking, sleep and emotions via morph targets
        if now >= self._blink_at:
            self._blink_at = now + random.uniform(2.5, 6.0)
        eh, mh, es, ms, ec = self.emotion.update(dt)
        r.setProperty("eyeClose", 1.0 if state == State.SLEEP else max(ec, self._blink_value(now)))
        happy = 1.0 if state == State.HAPPY else 0.0
        r.setProperty("eyeHappy", max(eh, happy))
        r.setProperty("mouthHappy", max(mh, happy * .8))
        r.setProperty('eyeSad', es)
        r.setProperty('mouthSad', ms)
        mouth = self.voice.update(dt)
        r.setProperty("mouthOpen", mouth if state != State.SLEEP else 0)
        if look is not None and state != State.SLEEP:
            gx, gy = max(-1., min(1., look[0] / 140)), max(-.6, min(.6, look[1] / 140))
        else:
            gx = gy = 0
        self._gaze = [smooth(self._gaze[0], gx, dt), smooth(self._gaze[1], gy, dt)]
        r.setProperty('lookX', self._gaze[0])
        r.setProperty('lookY', self._gaze[1])
        r.setProperty("dotColor", DOT.get(self.conn, "#e74c3c"))
        r.setProperty("observing", bool(self.observing))
        r.setProperty("estop", bool(self.estop))

    def _blink_value(self, now: float) -> float:
        """A blink is a 160 ms close/open right before each scheduled blink time."""
        left = self._blink_at - now
        if not 0 <= left < 0.16:
            return 0.0
        return 1.0 - abs((0.16 - left) - 0.08) / 0.08
