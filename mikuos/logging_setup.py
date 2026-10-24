from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(level: str = "INFO", log_dir: Path | None = None, to_file: bool = True,
                  max_bytes: int = 2_000_000, backups: int = 3) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    has_console = sys.stderr is not None  # None under pythonw.exe
    if has_console and not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                               for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if to_file and log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "mikuos.log", maxBytes=max_bytes, backupCount=backups,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
