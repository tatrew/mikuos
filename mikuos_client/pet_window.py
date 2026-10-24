"""PetWindow: transparent, frameless, always-on-top avatar window.

Clicks outside the drawn character pass through: the window mask is rebuilt from
the rendered alpha channel. A status dot shows backend/LLM state, an eye icon
shows that activity observation is on (visible indicator).
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBitmap, QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPolygonF, QRegion
from PySide6.QtWidgets import QWidget

from .animation import State
from .backend_client import ConnState

DOT = {ConnState.ONLINE: QColor("#2ecc71"), ConnState.LLM_DOWN: QColor("#f1c40f"),
       ConnState.BACKEND_OFFLINE: QColor("#e74c3c")}
DOT_TIP = {ConnState.ONLINE: "MikuOS: LM Studio подключена", ConnState.LLM_DOWN: "MikuOS: LM Studio недоступна",
           ConnState.BACKEND_OFFLINE: "MikuOS: backend не запущен"}


class PetWindow(QWidget):
    clicked = Signal()
    double_clicked = Signal()
    context_requested = Signal(QPoint)
    drag_started = Signal()
    dragged = Signal(QPoint)  # global position of the cursor
    drag_finished = Signal()

    def __init__(self, renderer):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("MikuOS Pet")
        self.renderer = renderer
        w, h = renderer.size()
        self.setFixedSize(w, h)
        self.foot_offset = h  # px from the window top to the foot line
        self.head_offset = 0  # px from the window top to the top of the head
        self.state = State.IDLE
        self.frame = 0
        self.facing = 1
        self.look: tuple[float, float] | None = None
        self.t = 0.0
        self.conn = ConnState.BACKEND_OFFLINE
        self.observing = False
        self.estop = False
        self.passive = False
        self._press: QPoint | None = None
        self._dragging = False
        self._mask_key = None
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.clicked.emit)
        self.setToolTip(DOT_TIP[self.conn])

    # ------------------------------------------------------------ drawing
    def set_view(self, state: State, frame: int, facing: int, look, t: float) -> None:
        self.state, self.frame, self.facing, self.look, self.t = state, frame, facing, look, t
        self.update()

    def set_conn(self, conn: str) -> None:
        self.conn = conn
        self.setToolTip(DOT_TIP.get(conn, conn))
        self.update()

    def set_passive(self, passive: bool) -> None:
        """Emergency stop: the pet ignores the mouse entirely (fully click-through)."""
        if passive == self.passive:
            return
        self.passive = passive
        self.setWindowFlag(Qt.WindowTransparentForInput, passive)
        self.show()

    def _render(self, p: QPainter) -> None:
        self.renderer.paint(p, self.state, self.frame, self.facing, self.look, self.t)
        w = self.width()
        p.setPen(QPen(QColor(0, 0, 0, 90), 1))
        p.setBrush(DOT[self.conn] if not self.estop else QColor("#8e44ad"))
        p.drawEllipse(QPointF(w - 9, 9), 5, 5)
        if self.observing and not self.estop:
            p.setPen(QPen(QColor("#1f8f88"), 1.5))
            p.setBrush(QColor(255, 255, 255, 220))
            p.drawEllipse(QRectF(w - 30, 4, 14, 9))
            p.setBrush(QColor("#1f8f88"))
            p.drawEllipse(QPointF(w - 23, 8.5), 2.2, 2.2)
        if self.estop:
            p.setPen(QColor("#8e44ad"))
            p.setFont(QFont("Segoe UI", 8, QFont.Bold))
            p.drawText(QPointF(4, 12), "STOP")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        self._render(p)
        p.end()
        self._update_mask()

    def _update_mask(self) -> None:
        key = (self.state, self.frame, self.facing, int(self.t * 4) if self.state in (State.WALK, State.CHASE,
                                                                                         State.HAPPY) else 0)
        if key == self._mask_key:
            return
        self._mask_key = key
        img = QImage(self.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        self._render(p)
        p.end()
        mask = QBitmap.fromImage(img.createAlphaMask())
        self.setMask(QRegion(mask))

    # -------------------------------------------------------------- mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._dragging = False
        elif e.button() == Qt.RightButton:
            self.context_requested.emit(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._press is None:
            return
        gp = e.globalPosition().toPoint()
        if not self._dragging and (gp - self._press).manhattanLength() > 6:
            self._dragging = True
            self.drag_started.emit()
        if self._dragging:
            self.dragged.emit(gp)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self._dragging:
            self.drag_finished.emit()
        else:
            self._click_timer.start(250)
        self._press = None
        self._dragging = False

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._click_timer.stop()
            self.double_clicked.emit()


class SpeechBubble(QWidget):
    """Click-through bubble with Miku's spontaneous lines (painted manually: a
    translucent top-level window does not draw stylesheet backgrounds)."""

    PAD = 10
    MAX_W = 320

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                         Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.font_ = QFont("Segoe UI", 10)
        self.text = ""
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self.hide)

    def _layout(self) -> None:
        fm = QFontMetrics(self.font_)
        r = fm.boundingRect(QRect(0, 0, self.MAX_W - 2 * self.PAD, 2000), Qt.TextWordWrap, self.text)
        self.setFixedSize(r.width() + 2 * self.PAD + 4, r.height() + 2 * self.PAD + 10)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(1, 1, self.width() - 2, self.height() - 11)
        p.setPen(QPen(QColor("#39c5bb"), 2))
        p.setBrush(QColor(255, 255, 255, 240))
        p.drawRoundedRect(body, 12, 12)
        tail = QPolygonF([QPointF(self.width() / 2 - 8, body.bottom() - 1), QPointF(self.width() / 2 + 8,
                          body.bottom() - 1), QPointF(self.width() / 2, self.height() - 1)])
        p.drawPolygon(tail)
        p.setPen(QColor("#1d2b2a"))
        p.setFont(self.font_)
        p.drawText(body.adjusted(self.PAD, self.PAD - 2, -self.PAD, -self.PAD), Qt.TextWordWrap, self.text)

    def say(self, text: str, anchor_top_center: QPoint, ms: int | None = None) -> None:
        text = text.strip()
        if len(text) > 280:
            text = text[:277] + "…"
        self.text = text
        self._layout()
        self.follow(anchor_top_center, force=True)
        self.show()
        self.update()
        self._hide.start(ms or max(5000, min(15000, 60 * len(text))))

    def follow(self, anchor_top_center: QPoint, force: bool = False) -> None:
        if self.isVisible() or force:
            self.move(anchor_top_center.x() - self.width() // 2, anchor_top_center.y() - self.height() - 2)
