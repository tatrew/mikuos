"""Run the MikuOS backend:  python -m mikuos [--settings path]"""
from __future__ import annotations

import argparse

import uvicorn

from .config import Settings
from .logging_setup import setup_logging
from .server import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="mikuos")
    ap.add_argument("--settings", help="path to settings.json")
    args = ap.parse_args()
    settings = Settings(args.settings)
    lg = settings.get("logging")
    setup_logging(lg["level"], settings.resolve_path("logs"), lg["file"], lg["max_bytes"], lg["backups"])
    host = settings.get("server", "host")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"refusing to bind to non-local host {host!r}")
    uvicorn.run(create_app(settings), host=host, port=int(settings.get("server", "port")), log_level="warning",
                ws_max_size=1 << 20)


if __name__ == "__main__":
    main()
