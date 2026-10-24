"""Start the overlay (and the backend if it is not running):  python -m mikuos_client"""
from __future__ import annotations

import argparse
import atexit
import subprocess
import sys
from pathlib import Path

import httpx


def main() -> None:
    ap = argparse.ArgumentParser(prog="mikuos_client")
    ap.add_argument("--no-backend", action="store_true", help="do not start the backend automatically")
    args = ap.parse_args()

    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel32.CreateMutexW(None, False, "Local\\MikuOS-Overlay")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        print("MikuOS overlay is already running")
        sys.exit(0)
    atexit.register(lambda: kernel32.CloseHandle(ctypes.c_void_p(mutex)))

    from mikuos.config import Settings
    from mikuos.logging_setup import setup_logging
    settings = Settings()
    setup_logging(settings.get("logging", "level"), settings.resolve_path("logs") / "client", True)

    port = int(settings.get("server", "port"))
    proc = None
    if not args.no_backend:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.5)
        except httpx.HTTPError:
            # pythonw has no stdio, which breaks uvicorn logging: use the console
            # interpreter without a window and send its output to a log file
            exe = Path(sys.executable)
            if exe.name.lower() == "pythonw.exe":
                exe = exe.with_name("python.exe")
            log_dir = settings.resolve_path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            out = open(log_dir / "backend-stdout.log", "ab")
            proc = subprocess.Popen([str(exe), "-m", "mikuos"], cwd=str(settings.root), stdout=out, stderr=out,
                                    stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            atexit.register(lambda: proc.poll() is None and proc.terminate())

    from PySide6.QtWidgets import QApplication

    from .app import OverlayApp
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    overlay = OverlayApp(qapp, settings)
    overlay.start()
    code = qapp.exec()
    if proc and proc.poll() is None:
        proc.terminate()
    sys.exit(code)


if __name__ == "__main__":
    main()
