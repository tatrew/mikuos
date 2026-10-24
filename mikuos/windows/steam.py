"""Steam integration: installed games from Steam's own library files, launching and
uninstalling them through steam.exe (no shell, fixed argument shapes)."""
from __future__ import annotations

import os
import re
import subprocess
import time
import winreg
from dataclasses import dataclass
from pathlib import Path

import psutil

from .bridge import WindowsBridgeError

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')
# Steam installs these as "apps", but they are not games
_NOT_GAMES = re.compile(r"redistributable|steamworks|proton|steam linux runtime|dedicated server", re.I)


def parse_vdf(text: str) -> dict:
    """Minimal parser for Valve's text KeyValues format (libraryfolders.vdf, *.acf)."""
    root: dict = {}
    stack = [root]
    key: str | None = None
    for m in _TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            child: dict = {}
            stack[-1][key or ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = s.replace("\\\\", "\\")
        else:
            stack[-1][key] = s.replace("\\\\", "\\")
            key = None
    return root


@dataclass
class SteamGame:
    app_id: int
    name: str
    install_dir: Path
    size_bytes: int
    last_played: int

    def as_dict(self) -> dict:
        return {"app_id": self.app_id, "name": self.name, "size_gb": round(self.size_bytes / 1e9, 1),
                "last_played": time.strftime("%Y-%m-%d", time.localtime(self.last_played)) if self.last_played
                else None}


class Steam:
    def __init__(self, steam_exe: str | Path | None = None):
        self._exe = Path(steam_exe) if steam_exe else None

    @property
    def exe(self) -> Path:
        if self._exe is None:
            self._exe = _find_steam_exe()
        if not self._exe or not self._exe.is_file():
            raise WindowsBridgeError("Steam is not installed (steam.exe not found)")
        return self._exe

    def available(self) -> bool:
        try:
            return self.exe.is_file()
        except WindowsBridgeError:
            return False

    def libraries(self) -> list[Path]:
        root = self.exe.parent
        libs = [root]
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace")).get("libraryfolders", {})
            for entry in data.values():
                if isinstance(entry, dict) and entry.get("path"):
                    p = Path(entry["path"])
                    if p not in libs:
                        libs.append(p)
        return libs

    def games(self) -> list[SteamGame]:
        out: dict[int, SteamGame] = {}
        for lib in self.libraries():
            for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
                try:
                    st = parse_vdf(acf.read_text(encoding="utf-8", errors="replace")).get("AppState", {})
                    app_id = int(st["appid"])
                except (OSError, KeyError, ValueError):
                    continue
                name = st.get("name") or str(app_id)
                if _NOT_GAMES.search(name):
                    continue
                out[app_id] = SteamGame(app_id, name, lib / "steamapps" / "common" / st.get("installdir", ""),
                                        int(st.get("SizeOnDisk") or 0), int(st.get("LastPlayed") or 0))
        return sorted(out.values(), key=lambda g: g.name.lower())

    def find(self, query: str | int) -> SteamGame:
        games = self.games()
        if isinstance(query, int) or str(query).isdigit():
            for g in games:
                if g.app_id == int(query):
                    return g
            raise WindowsBridgeError(f"no installed Steam game with app_id {query}")
        q = _norm(str(query))
        exact = [g for g in games if _norm(g.name) == q]
        partial = exact or [g for g in games if q and q in _norm(g.name)]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise WindowsBridgeError(f"no installed Steam game matches '{query}'")
        raise WindowsBridgeError("several games match: " + ", ".join(f"{g.name} ({g.app_id})" for g in partial[:8]))

    def launch(self, game: SteamGame, wait_s: float = 35.0) -> dict:
        running = self._game_process(game)
        if running:
            # -applaunch would only focus / do nothing; do not report a fresh start
            return {"app_id": game.app_id, "name": game.name, "sent_to_steam": False,
                    "already_running": True, "process": running}
        started = time.time() - 1
        self._run(["-applaunch", str(game.app_id)])
        # Steam may update/sync first; report honestly whether a game process showed up
        proc = self._wait_for_game_process(game, wait_s, started)
        res = {"app_id": game.app_id, "name": game.name, "sent_to_steam": True, "game_process_found": bool(proc)}
        if proc:
            res["process"] = proc
        else:
            res["note"] = (f"Steam получил команду запуска, но процесс игры за {int(wait_s)} с не появился "
                           "(возможно, обновление или запуск ещё идёт). Запуск не подтверждён.")
        return res

    def uninstall(self, game: SteamGame) -> dict:
        # Steam shows its own "uninstall?" dialog as well; nothing is removed by us directly
        self._run([f"steam://uninstall/{game.app_id}"])
        return {"app_id": game.app_id, "name": game.name, "sent_to_steam": True,
                "note": "Steam открыл своё окно удаления; игра удалится только после подтверждения в нём."}

    def _run(self, args: list[str]) -> None:
        try:
            subprocess.Popen([str(self.exe), *args], close_fds=True,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        except OSError as e:
            raise WindowsBridgeError(f"failed to start Steam: {e}") from e

    @staticmethod
    def _game_process(game: SteamGame, started_after: float = 0) -> dict | None:
        try:
            root = os.path.normcase(os.path.realpath(game.install_dir)) + os.sep
        except OSError:
            return None
        for p in psutil.process_iter(["pid", "name", "exe", "create_time"]):
            exe = p.info.get("exe")
            if exe and os.path.normcase(exe).startswith(root) and (p.info.get("create_time") or 0) >= started_after:
                return {"pid": p.info["pid"], "name": p.info["name"]}
        return None

    def _wait_for_game_process(self, game: SteamGame, wait_s: float, started_after: float) -> dict | None:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            proc = self._game_process(game, started_after)
            if proc:
                return proc
            time.sleep(0.5)
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def _find_steam_exe() -> Path | None:
    for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
        try:
            with winreg.OpenKey(hive, key) as k:
                v = Path(winreg.QueryValueEx(k, value)[0])
        except OSError:
            continue
        exe = v if v.suffix.lower() == ".exe" else v / "steam.exe"
        if exe.is_file():
            return exe
    default = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "steam.exe"
    return default if default.is_file() else None
