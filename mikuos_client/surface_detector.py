"""SurfaceDetector: horizontal surfaces the avatar can stand on.

Surfaces are the bottoms of monitor work areas (above the taskbar) and the
visible parts of top edges of application windows. A window's top edge is cut
where windows higher in the Z-order cover it, so Miku never stands "inside" a
window that is in front.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


@dataclass(frozen=True)
class Surface:
    y: int
    x1: int
    x2: int
    window_id: int | None = None  # None = floor (work-area bottom)

    def contains_x(self, x: float, margin: float = 0) -> bool:
        return self.x1 - margin <= x <= self.x2 + margin

    @property
    def width(self) -> int:
        return self.x2 - self.x1


def _subtract(seg: tuple[int, int], cut: tuple[int, int]) -> list[tuple[int, int]]:
    a, b = seg
    c, d = cut
    if d <= a or c >= b:
        return [seg]
    out = []
    if c > a:
        out.append((a, c))
    if d < b:
        out.append((d, b))
    return out


class SurfaceDetector:
    def __init__(self, min_width: int = 60, min_window_height: int = 80):
        self.min_width = min_width
        self.min_window_height = min_window_height

    def detect(self, work_areas: list[Box], windows: list[tuple[int, Box]]) -> list[Surface]:
        """windows: [(id, box)] in Z-order, topmost first; minimized windows excluded by caller."""
        surfaces: list[Surface] = []
        for wa in work_areas:
            surfaces.append(Surface(wa.bottom, wa.x, wa.right, None))
        for i, (wid, box) in enumerate(windows):
            if box.w < self.min_width or box.h < self.min_window_height:
                continue
            # top edge must be on some monitor (not above the screen)
            segs = [(max(box.x, wa.x), min(box.right, wa.right)) for wa in work_areas
                    if wa.y < box.y < wa.bottom and box.right > wa.x and box.x < wa.right]
            for _, above in windows[:i]:
                if above.y <= box.y <= above.bottom:  # covered at the edge height
                    segs = [s for seg in segs for s in _subtract(seg, (above.x, above.right))]
            for a, b in segs:
                if b - a >= self.min_width:
                    surfaces.append(Surface(box.y, a, b, wid))
        return surfaces

    @staticmethod
    def support_under(surfaces: list[Surface], x: float, foot_y: float, tolerance: float = 3) -> Surface | None:
        best = None
        for s in surfaces:
            if s.contains_x(x) and abs(s.y - foot_y) <= tolerance:
                if best is None or s.y < best.y:
                    best = s
        return best

    @staticmethod
    def landing(surfaces: list[Surface], x: float, prev_foot: float, new_foot: float) -> Surface | None:
        """First surface crossed while moving down from prev_foot to new_foot."""
        hits = [s for s in surfaces if s.contains_x(x) and prev_foot <= s.y <= new_foot]
        return min(hits, key=lambda s: s.y) if hits else None
