"""Avatar renderers.

`AvatarRenderer` is the extension point for a real model later (Live2D Cubism via
a QOpenGLWidget, or VRM via a WebGL/three-vrm view): implement `size()` and
`paint()` and pass the renderer to PetWindow. No third-party Miku assets are
bundled; `PlaceholderRenderer` draws a simple vector chibi with QPainter.
"""
from __future__ import annotations

import math
from typing import Protocol

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen

from .animation import State

TEAL = QColor("#39c5bb")
TEAL_DARK = QColor("#1f8f88")
SKIN = QColor("#ffe3d3")
GRAY = QColor("#5b6670")
WHITE = QColor("#f4f7f8")
PINK = QColor("#ff7aa8")


def _pen(color: QColor, width: float) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    return pen


class AvatarRenderer(Protocol):
    def size(self) -> tuple[int, int]: ...

    def paint(self, p: QPainter, state: State, frame: int, facing: int, look: tuple[float, float] | None,
              t: float) -> None: ...


class PlaceholderRenderer:
    BASE_W, BASE_H = 120, 150

    def __init__(self, scale: float = 1.0):
        self.scale = scale

    def size(self) -> tuple[int, int]:
        return int(self.BASE_W * self.scale), int(self.BASE_H * self.scale)

    def paint(self, p: QPainter, state: State, frame: int, facing: int, look: tuple[float, float] | None,
              t: float) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(self.scale, self.scale)
        w, h = self.BASE_W, self.BASE_H
        cx = w / 2
        bob = 0.0
        if state == State.IDLE:
            bob = math.sin(t * 2.2) * 1.5
        elif state in (State.WALK, State.CHASE):
            bob = -abs(math.sin(t * 10)) * 3
        elif state == State.HAPPY:
            bob = -abs(math.sin(t * 9)) * 8
        sit = state in (State.SIT, State.SLEEP)
        body_top = 86 + (10 if sit else 0) + bob
        head_c = QPointF(cx, 52 + (10 if sit else 0) + bob)
        p.setPen(Qt.NoPen)

        # twin tails behind the head (swing while moving)
        swing = math.sin(t * (10 if state in (State.WALK, State.CHASE, State.FALL, State.JUMP) else 2)) * 6
        if state == State.FALL:
            swing -= 10
        for side in (-1, 1):
            path = QPainterPath()
            base = QPointF(cx + side * 30, head_c.y() - 18)
            path.moveTo(base)
            path.cubicTo(QPointF(cx + side * (52 + swing * side), head_c.y() + 10),
                         QPointF(cx + side * (50 - swing * side), head_c.y() + 60),
                         QPointF(cx + side * (44 + swing), min(h - 6, head_c.y() + 92)))
            path.lineTo(QPointF(cx + side * (34 + swing), min(h - 10, head_c.y() + 88)))
            path.cubicTo(QPointF(cx + side * 38, head_c.y() + 50), QPointF(cx + side * 38, head_c.y() + 6),
                         QPointF(cx + side * 22, head_c.y() - 14))
            p.setBrush(TEAL)
            p.drawPath(path)
            p.setBrush(PINK)
            p.drawRoundedRect(QRectF(base.x() - 5, base.y() - 4, 10, 8), 2, 2)

        # legs
        p.setBrush(GRAY)
        if sit:
            p.drawRoundedRect(QRectF(cx - 20, body_top + 30, 40, 9), 4, 4)
        else:
            phase = math.sin(t * 12) * 5 if state in (State.WALK, State.CHASE) else 0
            for side, ph in ((-1, phase), (1, -phase)):
                p.drawRoundedRect(QRectF(cx + side * 8 - 5 + ph, body_top + 28, 10, 26 - abs(ph) * 0.3), 4, 4)
        # body (gray top + skirt) and teal tie
        p.setBrush(GRAY)
        p.drawRoundedRect(QRectF(cx - 19, body_top, 38, 34), 10, 10)
        p.setBrush(TEAL_DARK)
        tie = QPainterPath()
        tie.moveTo(cx - 4, body_top + 2)
        tie.lineTo(cx + 4, body_top + 2)
        tie.lineTo(cx + 2, body_top + 20)
        tie.lineTo(cx, body_top + 24)
        tie.lineTo(cx - 2, body_top + 20)
        tie.closeSubpath()
        p.drawPath(tie)
        # arms
        p.setBrush(SKIN)
        if state in (State.JUMP, State.HAPPY, State.DRAG):
            for side in (-1, 1):
                p.drawRoundedRect(QRectF(cx + side * 24 - 4, body_top - 18, 8, 22), 4, 4)
        elif state == State.CHASE:
            p.drawRoundedRect(QRectF(cx + facing * 20, body_top + 2, 22 * facing, 8).normalized(), 4, 4)
            p.drawRoundedRect(QRectF(cx - facing * 22 - 4, body_top + 6, 8, 20), 4, 4)
        else:
            for side in (-1, 1):
                p.drawRoundedRect(QRectF(cx + side * 21 - 4, body_top + 4, 8, 22), 4, 4)

        # head + bangs
        p.setBrush(SKIN)
        p.drawEllipse(head_c, 30, 28)
        p.setBrush(TEAL)
        bangs = QPainterPath()
        bangs.moveTo(head_c.x() - 31, head_c.y() + 2)
        bangs.cubicTo(QPointF(head_c.x() - 32, head_c.y() - 40), QPointF(head_c.x() + 32, head_c.y() - 40),
                      QPointF(head_c.x() + 31, head_c.y() + 2))
        for i, dx in enumerate((22, 10, -2, -14, -24)):
            bangs.lineTo(QPointF(head_c.x() + dx, head_c.y() - (4 if i % 2 else 12)))
        bangs.closeSubpath()
        p.drawPath(bangs)

        # eyes follow the cursor
        ex, ey = 0.0, 0.0
        if look is not None:
            dx, dy = look
            n = math.hypot(dx, dy) or 1
            ex, ey = dx / n * 3, dy / n * 2
        elif state in (State.WALK, State.CHASE):
            ex = 2.5 * facing
        eye_y = head_c.y() + 6
        blink = (int(t * 10) % 37) == 0
        if state == State.SLEEP or blink:
            p.setPen(_pen(TEAL_DARK, 2))
            for side in (-1, 1):
                p.drawLine(QPointF(head_c.x() + side * 11 - 5, eye_y), QPointF(head_c.x() + side * 11 + 5, eye_y))
            p.setPen(Qt.NoPen)
        else:
            for side in (-1, 1):
                p.setBrush(WHITE)
                p.drawEllipse(QPointF(head_c.x() + side * 11, eye_y), 6, 7)
                p.setBrush(TEAL_DARK)
                p.drawEllipse(QPointF(head_c.x() + side * 11 + ex, eye_y + ey), 3.6, 4.4)
        # mouth
        p.setPen(_pen(QColor("#b0575f"), 1.6))
        if state in (State.HAPPY, State.CHASE):
            p.drawArc(QRectF(head_c.x() - 5, head_c.y() + 13, 10, 7), 200 * 16, 140 * 16)
        else:
            p.drawLine(QPointF(head_c.x() - 3, head_c.y() + 17), QPointF(head_c.x() + 3, head_c.y() + 17))
        if state == State.SLEEP:
            p.setPen(TEAL_DARK)
            p.setFont(QFont("Segoe UI", 10, QFont.Bold))
            z = "Z" if frame == 0 else "Zz"
            p.drawText(QPointF(head_c.x() + 26, head_c.y() - 26), z)
        p.restore()
