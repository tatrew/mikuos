"""Settings: defaults in code, user values in config/settings.json, overrides from env.

The user edits config/settings.json (or the settings window); source code is never
edited to change behaviour.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "lmstudio": {
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "auto",
        # used when model == "auto": prefer a loaded LLM whose id contains one of these
        "model_preference": ["miku"],
        # never ask LM Studio to JIT-load a model that is not already loaded
        "allow_jit_load": False,
        "request_timeout_s": 120,
        "connect_timeout_s": 5,
        "max_retries": 2,
        "reconnect_interval_s": 10,
        # Gemma 4 has a thinking mode that eats the token budget; "none" disables it
        "reasoning_effort": "none",
        "tool_mode": "auto",  # native | json_fallback | auto
    },
    "generation": {
        "temperature": 0.7,
        "top_p": 0.95,
        "max_tokens": 600,
        "context_length": 0,  # 0 = take loaded_context_length from LM Studio
        "fallback_context_length": 8192,
    },
    "paths": {
        "database": "data/mikuos.db",
        "backups": "data/backups",
        "system_prompt": "miku_data/miku_persona_system_v4.txt",
        "runtime_rules": "prompts/runtime_rules.txt",
        "lore_seed": "prompts/character_lore.json",
        "screenshots": "data/screenshots",
        "logs": "data/logs",
        "runtime": "data/runtime",
    },
    "memory": {
        "auto_memory": True,
        "extract_every_user_messages": 4,
        "require_confirmation_for_sensitive": True,
        "max_memories_in_context": 8,
        "max_lore_in_context": 3,
        "recent_messages": 16,
        "duplicate_threshold": 0.85,
        "summarize_conversations": True,
        "conversation_idle_timeout_min": 30,
        "backup_keep": 10,
    },
    "initiative": {
        "mode": "normal",  # chatty | normal | quiet | dnd
        "cooldown_s": {"chatty": 300, "normal": 900, "quiet": 2700},
        "daily_llm_limit": 30,
        "away_threshold_s": 600,
        "long_focus_s": 5400,
        "suppress_fullscreen": True,
    },
    "observation": {
        "enabled": True,
        "poll_interval_s": 1.0,
        "store_window_titles": False,
        "store_events": True,
        "idle_threshold_s": 120,
    },
    "permissions": {
        # allow | ask | deny
        "observe_metadata": "allow",
        "observe_content": "ask",
        "app_control": "allow",
        "window_control": "allow",
        "file_read": "allow",
        "file_write": "ask",
        "memory_write": "allow",
        "ui_action": "ask",
        "cursor_control": "deny",
        # deleting / uninstalling / overwriting files: "ask" or "deny" ("allow" is treated as "ask")
        "destructive": "ask",
    },
    "allowed_apps": {
        "notepad": {"title": "Блокнот", "path": "notepad.exe"},
        "calculator": {"title": "Калькулятор", "path": "calc.exe"},
        "paint": {"title": "Paint", "path": "mspaint.exe"},
        "explorer": {"title": "Проводник", "path": "explorer.exe"},
        "steam": {"title": "Steam", "path": r"C:\Program Files (x86)\Steam\steam.exe",
                  "window_process": "steamwebhelper.exe"},
    },
    "allowed_directories": {
        "read": ["data/sandbox", "~/Documents/MikuOS"],
        "write": ["data/sandbox", "~/Documents/MikuOS"],
    },
    # windows whose content (screenshot / UI elements) may be read, by process name
    "content_allowed_processes": ["notepad.exe"],
    "agent": {
        "max_tool_iterations": 5,
        "max_consecutive_errors": 2,
        "duplicate_window_s": 20,
        "tool_timeout_s": 15,
        "max_file_read_chars": 20000,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "allowed_origins": [],
    },
    "ui": {
        "avatar": "auto",  # auto (sprite -> 3d -> placeholder) | sprite | 3d | placeholder
        "sprite_scale": 2,  # integer pixel-art zoom of the 2D sprites (x pet_scale)
        # prank: grab the real cursor and run with it; only if permissions.cursor_control == "allow"
        "cursor_prank": True,
        "cursor_prank_cooldown_s": 120,
        "cursor_prank_chance": 0.5,  # per catch
        "model_px": 220,  # height of the 3D Miku on screen, px
        "pet_scale": 1.0,
        "walk_speed": 90,
        "show_status_dot": True,
        "start_position": "bottom_right",
        "cursor_chase": True,
        "fps": 30,
        "background_fps": 10,
        "quality": "normal",
        "hair_physics": True,
        "hair_intensity": 1.0,
        "idle_cooldown_s": 4.0,
        "walk_probability": 0.45,
        "jump_probability": 0.15,
        "sit_probability": 0.20,
    },
    "hotkey": {"emergency_stop": "ctrl+alt+m", "fallbacks": ["ctrl+alt+shift+m", "ctrl+alt+f12"]},
    "logging": {"level": "INFO", "file": True, "max_bytes": 2_000_000, "backups": 3},
}

_ENV_MAP = {
    "MIKUOS_LMSTUDIO_URL": ("lmstudio", "base_url"),
    "MIKUOS_MODEL": ("lmstudio", "model"),
    "MIKUOS_DB": ("paths", "database"),
    "MIKUOS_SYSTEM_PROMPT": ("paths", "system_prompt"),
    "MIKUOS_PORT": ("server", "port"),
    "MIKUOS_LOG_LEVEL": ("logging", "level"),
    "MIKUOS_INITIATIVE": ("initiative", "mode"),
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


class Settings:
    """Thread-safe settings holder persisted to a JSON file."""

    def __init__(self, path: Path | str | None = None, root: Path | None = None, use_env: bool = True):
        self.root = Path(root) if root else PROJECT_ROOT
        self.path = Path(path) if path else self.root / "config" / "settings.json"
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        user = {}
        if self.path.exists():
            user = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = deep_merge(self._data, user)
        else:
            self.save()
        if use_env:
            env = {**_load_dotenv(self.root / ".env"), **os.environ}
            for key, (sec, name) in _ENV_MAP.items():
                if key in env and env[key] != "":
                    val: Any = env[key]
                    if isinstance(DEFAULTS[sec][name], int):
                        val = int(val)
                    self._data[sec][name] = val

    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        with self._lock:
            sec = self._data.get(section, {})
            if key is None:
                return copy.deepcopy(sec)
            return copy.deepcopy(sec.get(key, default)) if isinstance(sec, dict) else default

    def as_dict(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict) -> dict:
        """Merge a partial dict and persist. Unknown top-level sections are rejected."""
        with self._lock:
            unknown = set(patch) - set(DEFAULTS)
            if unknown:
                raise ValueError(f"unknown settings sections: {sorted(unknown)}")
            self._data = deep_merge(self._data, patch)
            self.save()
            return self.as_dict()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def resolve_path(self, key: str) -> Path:
        raw = self.get("paths", key)
        p = Path(os.path.expanduser(raw))
        return p if p.is_absolute() else (self.root / p)

    def resolve_dir_list(self, kind: str) -> list[Path]:
        out = []
        for raw in self.get("allowed_directories", kind) or []:
            p = Path(os.path.expanduser(raw))
            out.append(p if p.is_absolute() else (self.root / p))
        return out
