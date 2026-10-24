"""WindowsBridge: thin, real Win32 layer (ctypes + DWM + Shell + UI Automation).

This module performs NO permission checks: it is called only through the tool
layer (mikuos.tools), which consults the PermissionManager first. Coordinates
are physical pixels: the process is made per-monitor DPI aware, so multi-monitor
setups with different scaling and negative coordinates work consistently.
"""
from __future__ import annotations

import ctypes
import logging
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import psutil

log = logging.getLogger("mikuos.windows")

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("WindowsBridge requires Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shell32 = ctypes.WinDLL("shell32")
gdi32 = ctypes.WinDLL("gdi32")
try:
    shcore = ctypes.WinDLL("shcore")
except OSError:  # pragma: no cover
    shcore = None

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GW_OWNER = 4
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOW = 5
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                     wintypes.LPARAM)

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MonitorEnumProc, wintypes.LPARAM]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class WindowsBridgeError(Exception):
    pass


@dataclass
class Rect:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_win(cls, r: wintypes.RECT) -> "Rect":
        return cls(r.left, r.top, r.right - r.left, r.bottom - r.top)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass
class WindowInfo:
    id: int
    title: str
    class_name: str
    process: str
    pid: int
    rect: Rect
    is_minimized: bool
    is_maximized: bool
    monitor: int | None
    dpi: int | None

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


_dpi_set = False


def set_dpi_awareness() -> str:
    """Per-monitor v2 so all coordinates are physical pixels across monitors."""
    global _dpi_set
    if _dpi_set:
        return "already"
    _dpi_set = True
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return "per_monitor_v2"
    except AttributeError:
        pass
    try:
        if shcore and shcore.SetProcessDpiAwareness(2) == 0:
            return "per_monitor"
    except OSError:
        pass
    user32.SetProcessDPIAware()
    return "system"


class WindowsBridge:
    def __init__(self, screenshot_dir: Path | None = None):
        set_dpi_awareness()
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.own_pid = os.getpid()
        self._uia = None

    # ------------------------------------------------------------ primitives
    @staticmethod
    def _title(hwnd) -> str:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    @staticmethod
    def _class(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    @staticmethod
    def _pid(hwnd) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    @staticmethod
    def process_name(pid: int) -> str:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            kernel32.CloseHandle(h)

    @staticmethod
    def _rect(hwnd) -> Rect:
        r = wintypes.RECT()
        # extended frame bounds exclude the invisible resize borders of Win10/11
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(r))
        return Rect.from_win(r)

    @staticmethod
    def _cloaked(hwnd) -> bool:
        v = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
        return v.value != 0

    def _monitor_index(self, hmon) -> int | None:
        for i, m in enumerate(self._raw_monitors()):
            if m[0] == hmon:
                return i
        return None

    @staticmethod
    def _dpi_for(hmon) -> int | None:
        if not shcore:
            return None
        x, y = ctypes.c_uint(), ctypes.c_uint()
        if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
            return x.value
        return None

    def is_valid_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(user32.IsWindow(hwnd))

    def window_info(self, hwnd: int) -> WindowInfo:
        if not self.is_valid_window(hwnd):
            raise WindowsBridgeError(f"window {hwnd} does not exist")
        pid = self._pid(hwnd)
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        return WindowInfo(
            id=int(hwnd), title=self._title(hwnd), class_name=self._class(hwnd), process=self.process_name(pid),
            pid=pid, rect=self._rect(hwnd), is_minimized=bool(user32.IsIconic(hwnd)),
            is_maximized=bool(user32.IsZoomed(hwnd)), monitor=self._monitor_index(hmon), dpi=self._dpi_for(hmon),
        )

    def _is_app_window(self, hwnd) -> bool:
        if not user32.IsWindowVisible(hwnd) or self._cloaked(hwnd):
            return False
        if user32.GetWindow(hwnd, GW_OWNER):
            return False
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW or ex & WS_EX_NOACTIVATE:
            return False
        if not user32.GetWindowTextLengthW(hwnd):
            return False
        return self._class(hwnd) not in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")

    # ------------------------------------------------------------ observation
    def get_active_window(self) -> WindowInfo | None:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        try:
            return self.window_info(hwnd)
        except WindowsBridgeError:
            return None

    def list_windows(self, include_own: bool = False) -> list[WindowInfo]:
        """Visible top-level application windows in Z-order (topmost first)."""
        handles: list[int] = []

        @EnumWindowsProc
        def cb(hwnd, _):
            if self._is_app_window(hwnd):
                handles.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        out = []
        for h in handles:
            try:
                info = self.window_info(h)
            except WindowsBridgeError:
                continue
            if not include_own and info.pid == self.own_pid:
                continue
            out.append(info)
        return out

    def get_window_geometry(self, window_id: int) -> dict:
        info = self.window_info(window_id)
        return {"id": info.id, **asdict(info.rect), "monitor": info.monitor, "dpi": info.dpi,
                "is_minimized": info.is_minimized, "is_maximized": info.is_maximized}

    def get_cursor_position(self) -> dict:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        return {"x": pt.x, "y": pt.y, "monitor": self._monitor_index(hmon)}

    def get_user_idle_time(self) -> float:
        lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            raise WindowsBridgeError("GetLastInputInfo failed")
        tick = kernel32.GetTickCount() & 0xFFFFFFFF
        return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0

    def get_running_applications(self) -> list[dict]:
        """Processes that own visible application windows (what the user sees)."""
        apps: dict[str, dict] = {}
        for w in self.list_windows():
            a = apps.setdefault(w.process.lower() or f"pid{w.pid}", {"process": w.process, "pids": set(), "windows": 0})
            a["pids"].add(w.pid)
            a["windows"] += 1
        return [{"process": a["process"], "pids": sorted(a["pids"]), "windows": a["windows"]} for a in apps.values()]

    @staticmethod
    def get_system_time() -> dict:
        now = datetime.now().astimezone()
        return {"iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A"),
                "timezone": str(now.tzinfo)}

    def _raw_monitors(self) -> list[tuple]:
        mons: list[tuple] = []

        @MonitorEnumProc
        def cb(hmon, hdc, lprect, lparam):
            mi = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
            user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
            mons.append((hmon, Rect.from_win(mi.rcMonitor), Rect.from_win(mi.rcWork), bool(mi.dwFlags & 1),
                         mi.szDevice))
            return True

        user32.EnumDisplayMonitors(None, None, cb, 0)
        return mons

    def monitors(self) -> list[dict]:
        out = []
        for i, (hmon, rect, work, primary, dev) in enumerate(self._raw_monitors()):
            dpi = self._dpi_for(hmon)
            out.append({"index": i, "device": dev, "rect": asdict(rect), "work_area": asdict(work),
                        "primary": primary, "dpi": dpi, "scale": round((dpi or 96) / 96, 2)})
        return out

    def user_notification_state(self) -> str:
        """busy / d3d_fullscreen / presentation / quiet_time / accepts / not_present."""
        state = ctypes.c_int(0)
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return "unknown"
        return {1: "not_present", 2: "busy", 3: "d3d_fullscreen", 4: "presentation", 5: "accepts",
                6: "quiet_time", 7: "app"}.get(state.value, "unknown")

    def is_fullscreen_foreground(self) -> bool:
        if self.user_notification_state() in ("busy", "d3d_fullscreen", "presentation"):
            return True
        w = self.get_active_window()
        if not w or w.class_name in ("Progman", "WorkerW"):
            return False
        for _, rect, *_ in self._raw_monitors():
            if (w.rect.x <= rect.x and w.rect.y <= rect.y and w.rect.right >= rect.right
                    and w.rect.bottom >= rect.bottom):
                return True
        return False

    # -------------------------------------------------------------- control
    def open_application(self, path: str, window_process: str | None = None, wait_s: float = 6.0) -> dict:
        """Start an executable (no shell, no arguments) and try to find its window."""
        before = {w.id for w in self.list_windows()}
        fg_before = user32.GetForegroundWindow()
        try:
            proc = subprocess.Popen([path], close_fds=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        except FileNotFoundError as e:
            raise WindowsBridgeError(f"executable not found: {path}") from e
        except OSError as e:
            raise WindowsBridgeError(f"failed to start {path}: {e}") from e
        target = (window_process or os.path.basename(path)).lower()
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            for w in self.list_windows():
                if w.process.lower() == target and (w.id not in before or w.pid == proc.pid):
                    return {"started": True, "pid": proc.pid, "window_found": True, "window": w.as_dict()}
            fg = user32.GetForegroundWindow()
            if fg and fg != fg_before:
                try:
                    info = self.window_info(fg)
                except WindowsBridgeError:
                    continue
                if info.process.lower() == target:
                    # e.g. Win11 Notepad opens a new tab in an already open window
                    return {"started": True, "pid": proc.pid, "window_found": True, "window": info.as_dict(),
                            "reused_existing_window": True}
            code = proc.poll()
            if code not in (None, 0):
                raise WindowsBridgeError(f"{path} exited with code {code}")
        return {"started": True, "pid": proc.pid, "window_found": False}

    def focus_window(self, window_id: int) -> dict:
        self.window_info(window_id)
        if user32.IsIconic(window_id):
            user32.ShowWindow(window_id, SW_RESTORE)
        # Windows blocks focus stealing; attaching to the foreground thread's input
        # queue for the call is the standard way around it without synthetic input.
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        own_thread = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != own_thread and user32.AttachThreadInput(own_thread, fg_thread, True))
        try:
            user32.BringWindowToTop(window_id)
            ok = bool(user32.SetForegroundWindow(window_id))
        finally:
            if attached:
                user32.AttachThreadInput(own_thread, fg_thread, False)
        time.sleep(0.15)
        return {"focused": ok and user32.GetForegroundWindow() == window_id}

    def minimize_window(self, window_id: int) -> dict:
        self.window_info(window_id)
        user32.ShowWindow(window_id, SW_MINIMIZE)
        time.sleep(0.2)
        return {"minimized": bool(user32.IsIconic(window_id))}

    def restore_window(self, window_id: int) -> dict:
        self.window_info(window_id)
        user32.ShowWindow(window_id, SW_RESTORE)
        time.sleep(0.2)
        return {"restored": not bool(user32.IsIconic(window_id))}

    def move_window(self, window_id: int, x: int, y: int, width: int | None = None, height: int | None = None) -> dict:
        info = self.window_info(window_id)
        w = width or info.rect.width
        h = height or info.rect.height
        # SetWindowPos uses the outer rect; compensate for the invisible DWM border
        outer = wintypes.RECT()
        user32.GetWindowRect(window_id, ctypes.byref(outer))
        dx = info.rect.x - outer.left
        dy = info.rect.y - outer.top
        dw = (outer.right - outer.left) - info.rect.width
        dh = (outer.bottom - outer.top) - info.rect.height
        if not user32.SetWindowPos(window_id, None, x - dx, y - dy, w + dw, h + dh, SWP_NOZORDER | SWP_NOACTIVATE):
            raise WindowsBridgeError(f"SetWindowPos failed: {ctypes.get_last_error()}")
        return self.get_window_geometry(window_id)

    def close_window(self, window_id: int) -> None:
        WM_CLOSE = 0x0010
        user32.PostMessageW(window_id, WM_CLOSE, 0, 0)

    @staticmethod
    def open_directory(path: Path) -> dict:
        p = Path(path)
        if not p.is_dir():
            raise WindowsBridgeError(f"not a directory: {p}")
        os.startfile(str(p))  # opens Explorer
        return {"opened": str(p)}

    @staticmethod
    def open_file(path: Path) -> dict:
        p = Path(path)
        if not p.is_file():
            raise WindowsBridgeError(f"not a file: {p}")
        os.startfile(str(p))  # default handler
        return {"opened": str(p)}

    @staticmethod
    def create_text_file(path: Path, content: str) -> dict:
        p = Path(path)
        if p.exists():
            raise WindowsBridgeError(f"file already exists: {p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x", encoding="utf-8") as f:
            f.write(content)
        return {"created": str(p), "bytes": p.stat().st_size}

    @staticmethod
    def read_text_file(path: Path, max_chars: int = 20000) -> dict:
        p = Path(path)
        if not p.is_file():
            raise WindowsBridgeError(f"not a file: {p}")
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(max_chars + 1)
        return {"path": str(p), "content": text[:max_chars], "truncated": len(text) > max_chars}

    @staticmethod
    def write_text_file(path: Path, content: str) -> dict:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".mikuos-tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
        return {"written": str(p), "bytes": p.stat().st_size}

    @staticmethod
    def set_cursor_position(x: int, y: int) -> dict:
        if not user32.SetCursorPos(int(x), int(y)):
            raise WindowsBridgeError("SetCursorPos failed")
        return {"x": x, "y": y}

    @staticmethod
    def mouse_button_down() -> bool:
        """Any mouse button held right now (the user may be dragging something).
        Reads button state only; keyboard keys are never queried."""
        return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))  # L, R, M buttons

    # ------------------------------------------------------------ content
    def capture_window(self, window_id: int, out_path: Path | None = None) -> dict:
        """Screenshot of one window via PrintWindow (works for occluded windows)."""
        info = self.window_info(window_id)
        outer = wintypes.RECT()
        user32.GetWindowRect(window_id, ctypes.byref(outer))
        w, h = outer.right - outer.left, outer.bottom - outer.top
        if w <= 0 or h <= 0 or info.is_minimized:
            raise WindowsBridgeError("window is minimized or has no area")
        hdc_win = user32.GetDC(window_id)
        hdc = gdi32.CreateCompatibleDC(hdc_win)
        bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
        old = gdi32.SelectObject(hdc, bmp)
        try:
            if not user32.PrintWindow(window_id, hdc, PW_RENDERFULLCONTENT):
                raise WindowsBridgeError("PrintWindow failed")
            bih = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                                   biBitCount=32, biCompression=BI_RGB)
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bih), DIB_RGB_COLORS):
                raise WindowsBridgeError("GetDIBits failed")
        finally:
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(hdc)
            user32.ReleaseDC(window_id, hdc_win)
        if out_path is None:
            base = self.screenshot_dir or Path(".")
            base.mkdir(parents=True, exist_ok=True)
            out_path = base / f"window-{window_id}-{time.strftime('%Y%m%d-%H%M%S')}.bmp"
        _write_bmp(Path(out_path), w, h, buf.raw)
        return {"path": str(out_path), "width": w, "height": h, "window": info.title}

    def _uia_client(self):
        if self._uia is None:
            import comtypes
            import comtypes.client
            comtypes.CoInitialize()
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia  # noqa: N813
            self._uia_mod = uia
            self._uia = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
        return self._uia, self._uia_mod

    def list_ui_elements(self, window_id: int, max_elements: int = 80) -> list[dict]:
        """Accessible elements of a window via UI Automation (names are untrusted text)."""
        self.window_info(window_id)
        uia, mod = self._uia_client()
        root = uia.ElementFromHandle(window_id)
        cond = uia.CreateTrueCondition()
        found = root.FindAll(mod.TreeScope_Descendants, cond)
        out = []
        for i in range(min(found.Length, max_elements)):
            el = found.GetElement(i)
            try:
                r = el.CurrentBoundingRectangle
                out.append({
                    "index": i, "name": el.CurrentName or "", "control_type": el.CurrentLocalizedControlType or "",
                    "automation_id": el.CurrentAutomationId or "",
                    "rect": {"x": r.left, "y": r.top, "width": r.right - r.left, "height": r.bottom - r.top},
                    "enabled": bool(el.CurrentIsEnabled),
                })
            except Exception:  # elements may vanish while iterating
                continue
        return out

    def invoke_ui_element(self, window_id: int, automation_id: str = "", name: str = "") -> dict:
        """Invoke (click) one element by AutomationId or exact name via InvokePattern."""
        if not automation_id and not name:
            raise WindowsBridgeError("automation_id or name required")
        self.window_info(window_id)
        uia, mod = self._uia_client()
        root = uia.ElementFromHandle(window_id)
        if automation_id:
            cond = uia.CreatePropertyCondition(mod.UIA_AutomationIdPropertyId, automation_id)
        else:
            cond = uia.CreatePropertyCondition(mod.UIA_NamePropertyId, name)
        el = root.FindFirst(mod.TreeScope_Descendants, cond)
        if not el:
            raise WindowsBridgeError("element not found")
        pattern = el.GetCurrentPattern(mod.UIA_InvokePatternId)
        if not pattern:
            raise WindowsBridgeError("element does not support Invoke")
        pattern.QueryInterface(mod.IUIAutomationInvokePattern).Invoke()
        return {"invoked": el.CurrentName or automation_id}


def _write_bmp(path: Path, w: int, h: int, bgra_top_down: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = w * 4
    header = struct.pack("<2sIHHI", b"BM", 54 + row * h, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, row * h, 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + dib + bgra_top_down)
