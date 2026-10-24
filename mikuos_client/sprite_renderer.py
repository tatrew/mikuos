"""2D pixel-art Miku from a sprite sheet (Shimeji style) plus an idle GIF.

The sheet has a flat background colour; frames are found once as connected
regions of non-background pixels and cached in `frames.json` next to the sheet
(`slice_sheet`). Frame indices below are in row-major order of that sheet
(assets/sprites/miku_sheet.png); the idle loop comes from miku_idle.gif.
Sprites face right; for facing=-1 directional frames are mirrored.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QImage, QMovie, QPainter, QTransform

from .animation import State

log = logging.getLogger("mikuos.client.sprites")

SPRITE_DIR = Path(__file__).resolve().parent.parent / "assets" / "sprites"
SHEET = SPRITE_DIR / "miku_sheet.png"
IDLE_GIF = SPRITE_DIR / "miku_idle.gif"


@dataclass(frozen=True)
class Anim:
    frames: tuple  # sheet indices, or "idle" for the GIF loop
    fps: float = 8.0
    loop: bool = True
    mirror: bool = True  # frame faces right -> mirror when walking left


RUN_CYCLE = (20, 21, 22, 23, 24, 25, 26, 27)
ANIMS: dict[State, Anim] = {
    State.IDLE: Anim(("idle",), 10, mirror=False),
    State.WALK: Anim(RUN_CYCLE, 10),
    State.RUN: Anim(RUN_CYCLE, 16),
    State.CHASE: Anim(RUN_CYCLE, 16),
    State.JUMP: Anim((28, 31, 32), 10, loop=False),
    State.FALL: Anim((33, 34), 8),
    State.LAND: Anim((35, 36, 28), 12, loop=False),
    # the sheet has no seated pose: on an edge she keeps the idle loop
    State.SIT: Anim(("idle",), 10, mirror=False),
    State.SITTING_IDLE: Anim(("idle",), 10, mirror=False),
    State.STAND_UP: Anim((28, 36), 8, loop=False),
    State.SLEEP: Anim((52,), 1),
    State.DRAG: Anim((40, 41, 42, 43), 8),
    State.HAPPY: Anim((58, 61, 62, 63, 64), 8, mirror=False),
    State.WAVE: Anim((61, 62, 63, 64), 8, mirror=False),
    State.LOOK: Anim((39,), 1),
    State.LOOK_AROUND: Anim((39, 37, 44, 37), 2),
    State.THINK: Anim((60,), 1, mirror=False),
    State.SURPRISED: Anim((50, 29), 4, loop=False),
    State.SAD: Anim((59,), 1, mirror=False),
}
TALK = Anim((53, 61), 6, mirror=False)  # mouth closed / open while a line is shown


def available() -> bool:
    return SHEET.is_file() and IDLE_GIF.is_file()


def _background(img: QImage) -> int:
    w, h = img.width(), img.height()
    return Counter(img.pixel(x, y) for x, y in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]).most_common(1)[0][0]


def slice_sheet(img: QImage, min_pixels: int = 300) -> list[list[int]]:
    """[x0, y0, x1, y1] per frame, row-major. Small fragments (loose hair tips)
    are merged into the nearest frame; rows group frames by their foot line."""
    img = img.convertToFormat(QImage.Format_ARGB32)
    bg = _background(img)
    w, h = img.width(), img.height()
    fg = [[img.pixel(x, y) != bg for x in range(w)] for y in range(h)]
    seen = [[False] * w for _ in range(h)]
    boxes = []
    for y in range(h):
        for x in range(w):
            if not fg[y][x] or seen[y][x]:
                continue
            seen[y][x] = True
            q = deque([(x, y)])
            b = [x, y, x, y, 0]
            while q:
                cx, cy = q.popleft()
                b[4] += 1
                b[0], b[1], b[2], b[3] = min(b[0], cx), min(b[1], cy), max(b[2], cx), max(b[3], cy)
                for nx in (cx - 1, cx, cx + 1):
                    for ny in (cy - 1, cy, cy + 1):
                        if 0 <= nx < w and 0 <= ny < h and fg[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            q.append((nx, ny))
            boxes.append(b)
    big = [b for b in boxes if b[4] >= min_pixels]
    for s in (b for b in boxes if b[4] < min_pixels):
        cx, cy = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
        t = min(big, key=lambda b: max(0, b[0] - cx, cx - b[2]) + max(0, b[1] - cy, cy - b[3]))
        t[0], t[1], t[2], t[3] = min(t[0], s[0]), min(t[1], s[1]), max(t[2], s[2]), max(t[3], s[3])
    bottoms = sorted({b[3] for b in big})
    row_of, row = {}, 0
    for i, v in enumerate(bottoms):
        if i and v - bottoms[i - 1] > 15:
            row += 1
        row_of[v] = row
    big.sort(key=lambda b: (row_of[b[3]], b[0]))
    return [b[:4] for b in big]


def _foot_x(img: QImage) -> float:
    """Horizontal anchor: centre of the opaque pixels in the bottom rows (the feet),
    so frames with flying hair do not jump sideways."""
    xs = [x for y in range(max(0, img.height() - 8), img.height()) for x in range(img.width())
          if (img.pixel(x, y) >> 24) > 128]
    return sum(xs) / len(xs) if xs else img.width() / 2


def _key_out(img: QImage, bg: int) -> QImage:
    """Background colour -> fully transparent (the sheet has no alpha channel)."""
    img = img.convertToFormat(QImage.Format_ARGB32)
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixel(x, y) == bg:
                img.setPixel(x, y, 0)
    return img


def _trim(img: QImage) -> QImage:
    """Crop transparent rows at the bottom so every frame stands on its last row."""
    for y in range(img.height() - 1, -1, -1):
        if any((img.pixel(x, y) >> 24) > 0 for x in range(img.width())):
            return img.copy(0, 0, img.width(), y + 1)
    return img


class SpriteRenderer:
    def __init__(self, scale: int = 2, sheet: Path = SHEET, idle_gif: Path = IDLE_GIF):
        self.scale = max(1, int(scale))
        self.talk_until = 0.0
        sheet_img = QImage(str(sheet)).convertToFormat(QImage.Format_ARGB32)
        if sheet_img.isNull():
            raise FileNotFoundError(sheet)
        boxes = self._boxes(sheet, sheet_img)
        bg = _background(sheet_img)
        used = {i for a in (*ANIMS.values(), TALK) for i in a.frames if i != "idle"}
        if max(used) >= len(boxes):
            raise ValueError(f"sprite sheet has {len(boxes)} frames, animations need {max(used) + 1}")
        self.frames: dict = {}
        for i in used:
            x0, y0, x1, y1 = boxes[i]
            self.frames[i] = self._prep(_key_out(sheet_img.copy(QRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)), bg))
        movie = QMovie(str(idle_gif))
        self.idle = []
        for k in range(movie.frameCount()):
            movie.jumpToFrame(k)
            self.idle.append(self._prep(movie.currentImage().convertToFormat(QImage.Format_ARGB32)))
        if not self.idle:
            raise ValueError(f"cannot read frames from {idle_gif}")
        all_frames = [*self.frames.values(), *self.idle]
        s = self.scale
        # room on both sides of the foot anchor for the widest frame, plus a strip for the status dot
        half = max(max(f[1], f[0].width() - f[1]) for f in all_frames)
        self._w = int(2 * half * s) + 8
        self._top = 16
        self._h = max(f[0].height() for f in all_frames) * s + self._top
        self.head_offset = self._h - self.idle[0][0].height() * s

    @staticmethod
    def _boxes(sheet: Path, img: QImage) -> list:
        cache = sheet.with_name("frames.json")
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("size") == [img.width(), img.height()] and data.get("mtime") == int(sheet.stat().st_mtime):
                return data["frames"]
        except (OSError, ValueError, KeyError):
            pass
        log.info("slicing sprite sheet %s (one time)", sheet.name)
        boxes = slice_sheet(img)
        try:
            cache.write_text(json.dumps({"size": [img.width(), img.height()], "mtime": int(sheet.stat().st_mtime),
                                         "frames": boxes}), encoding="utf-8")
        except OSError:
            log.warning("cannot cache sprite frames in %s", cache)
        return boxes

    @staticmethod
    def _prep(img: QImage) -> tuple[QImage, float]:
        img = _trim(img)
        return img, _foot_x(img)

    # ------------------------------------------------------------ protocol
    def size(self) -> tuple[int, int]:
        return self._w, self._h

    def pick(self, state: State, t: float) -> tuple[object, bool]:
        """(frame id, mirror allowed) for a state at time t since the state began."""
        talking = state == State.IDLE and time.monotonic() < self.talk_until
        anim = TALK if talking else ANIMS.get(state, ANIMS[State.IDLE])
        if talking:
            t = time.monotonic()
        frames = tuple(range(len(self.idle))) if anim.frames == ("idle",) else anim.frames
        n = int(max(0.0, t) * anim.fps)
        k = n % len(frames) if anim.loop else min(n, len(frames) - 1)
        fid = ("idle", frames[k]) if anim.frames == ("idle",) else frames[k]
        return fid, anim.mirror

    def mask_key(self, state: State, facing: int, t: float):
        fid, mirror = self.pick(state, t)
        return fid, facing if mirror else 1

    def paint(self, p: QPainter, state: State, frame: int, facing: int, look, t: float) -> None:
        fid, mirror = self.pick(state, t)
        img, foot_x = self.idle[fid[1]] if isinstance(fid, tuple) else self.frames[fid]
        flip = mirror and facing < 0
        if flip:
            img = img.transformed(QTransform().scale(-1, 1))
            foot_x = img.width() - foot_x
        s = self.scale
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)  # crisp pixel art
        x = self._w / 2 - foot_x * s
        y = self._h - img.height() * s
        p.drawImage(QRectF(x, y, img.width() * s, img.height() * s), img)
        p.restore()


def sprite_window(scale: float = 1.0, pixel_scale: int = 2):
    from .pet_window import PetWindow

    r = SpriteRenderer(max(1, round(pixel_scale * scale)))
    pet = PetWindow(r)
    pet.head_offset = r.head_offset

    def talk(seconds: float) -> None:  # mouth moves while a line is shown in the bubble
        r.talk_until = max(r.talk_until, time.monotonic() + max(0.5, seconds))
        pet.update()

    pet.talk = talk
    return pet
