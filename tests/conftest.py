from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mikuos.config import Settings  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs a running LM Studio with a loaded model")
    config.addinivalue_line("markers", "desktop: touches the real Windows desktop (opens/moves windows)")


@pytest.fixture
def tmp_settings(tmp_path) -> Settings:
    """Real Settings object rooted at the project (so the real persona prompt is
    used) but with all writable paths in a temp dir."""
    s = Settings(tmp_path / "settings.json", root=ROOT, use_env=False)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    s.update({
        "paths": {"database": str(tmp_path / "mikuos.db"), "backups": str(tmp_path / "backups"),
                  "screenshots": str(tmp_path / "shots"), "logs": str(tmp_path / "logs"),
                  "runtime": str(tmp_path / "runtime")},
        "allowed_directories": {"read": [str(sandbox)], "write": [str(sandbox)]},
        "observation": {"enabled": False},
    })
    return s


@pytest.fixture
def sandbox(tmp_settings) -> Path:
    return Path(tmp_settings.get("allowed_directories", "read")[0])


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def dead_url() -> str:
    """A localhost port with nothing listening: a real 'LM Studio is off' case."""
    return f"http://127.0.0.1:{free_port()}/v1"
