"""WindowTracker: local window geometry polling for the avatar (no backend, no LLM).

Reports surfaces and per-window movement deltas so the avatar can ride a window
that the user drags. Only metadata (rectangles) is used.
"""
from __future__ import annotations

from mikuos.windows import WindowsBridge

from .surface_detector import Box, Surface, SurfaceDetector


class WindowTracker:
    def __init__(self, bridge: WindowsBridge | None = None, detector: SurfaceDetector | None = None):
        self.bridge = bridge or WindowsBridge()
        self.detector = detector or SurfaceDetector()
        self._rects: dict[int, Box] = {}
        self.work_areas: list[Box] = []
        self.enabled = True  # window surfaces off -> only the taskbar floor is used

    def poll(self) -> tuple[list[Surface], dict[int, tuple[int, int]]]:
        mons = self.bridge.monitors()
        self.work_areas = [Box(m["work_area"]["x"], m["work_area"]["y"], m["work_area"]["width"],
                               m["work_area"]["height"]) for m in mons]
        windows: list[tuple[int, Box]] = []
        if self.enabled:
            for w in self.bridge.list_windows():
                if w.is_minimized:
                    continue
                # Maximized windows still occlude windows below them. Their own
                # top edge is excluded by the work-area clipping in detect().
                windows.append((w.id, Box(w.rect.x, w.rect.y, w.rect.width, w.rect.height)))
        moves: dict[int, tuple[int, int]] = {}
        new_rects = dict(windows)
        for wid, box in new_rects.items():
            old = self._rects.get(wid)
            if old and (old.x, old.y) != (box.x, box.y):
                moves[wid] = (box.x - old.x, box.y - old.y)
        self._rects = new_rects
        return self.detector.detect(self.work_areas, windows), moves

    def monitor_scale_at(self, x: float, y: float) -> float:
        for m in self.bridge.monitors():
            r = m["rect"]
            if r["x"] <= x < r["x"] + r["width"] and r["y"] <= y < r["y"] + r["height"]:
                return m["scale"]
        return 1.0
