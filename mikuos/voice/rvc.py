"""Optional RVC voice conversion through a local Applio install: Silero speaks, Applio turns the
voice into the RVC model's (e.g. a Miku model trained in Applio). Applio has its own Python with
torch + CUDA, so the conversion runs there in a long-lived worker (applio_worker.py) that keeps the
model loaded; MikuOS only exchanges small files with it. Any failure -> plain Silero voice."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import uuid
import wave
from pathlib import Path

log = logging.getLogger("mikuos.voice.rvc")

WORKER = Path(__file__).with_name("applio_worker.py")
# conversion parameters; None = take the value from Applio's preset for this model, then this default
PARAMS = {"pitch": 0, "f0_method": "rmvpe", "index_rate": 0.5, "protect": 0.5, "volume_envelope": 1.0,
          "embedder_model": "contentvec"}


def default_applio_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Applio"


def _base_name(pth: Path) -> str:
    return re.sub(r"_e\d+_s\d+$", "", pth.stem)  # MikuAI_e210_s6300 -> MikuAI


class ApplioConverter:
    def __init__(self, cfg: dict, tmp_dir: Path):
        self.cfg = cfg or {}
        self.root = Path(self.cfg.get("applio_dir") or default_applio_dir())
        self.runtime = self.root / "data" / "runtime"
        self.python = self.runtime / ".venv" / "Scripts" / "python.exe"
        self.tmp_dir = Path(tmp_dir)
        self.model, self.index = self._find_model()
        self.last_error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._ready = False

    # ------------------------------------------------------------- discovery
    def _find_model(self) -> tuple[Path | None, Path | None]:
        logs = self.runtime / "logs"
        model = self.cfg.get("model") or ""
        if model:
            model = Path(model) if Path(model).is_absolute() else self.runtime / model
        else:  # the first .pth in Applio's logs whose name mentions the wanted voice
            want = (self.cfg.get("model_match") or "miku").lower()
            found = sorted(p for p in logs.glob("*/*.pth") if want in p.name.lower()) if logs.is_dir() else []
            model = found[0] if found else None
        index = self.cfg.get("index") or ""
        if index:
            index = Path(index) if Path(index).is_absolute() else self.runtime / index
        elif model and logs.is_dir():
            base = _base_name(Path(model)).lower()
            found = sorted(p for p in logs.glob("*/*.index") if base in p.name.lower())
            index = found[0] if found else None
        return (Path(model) if model else None), (Path(index) if index else None)

    def _preset(self) -> dict:
        """Settings the user picked for this model in Applio (model-presets.json)."""
        try:
            presets = json.loads((self.root / "data" / "model-presets.json").read_text(encoding="utf-8"))["presets"]
            rel = self.model.relative_to(self.runtime).as_posix()
        except (OSError, ValueError, KeyError, AttributeError, json.JSONDecodeError):
            return {}
        return next((v for k, v in presets.items() if k.split(":", 1)[-1] == rel), {})

    def params(self) -> dict:
        preset = self._preset()
        out = {}
        for key, default in PARAMS.items():
            value = self.cfg.get(key)
            out[key] = value if value is not None else preset.get(key, default)
        return out

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @property
    def available(self) -> bool:
        return (self.enabled and self.python.is_file() and (self.root / "app" / "backend" / "applio").is_dir()
                and self.model is not None and self.model.is_file())

    def status(self) -> dict:
        return {"enabled": self.enabled, "available": self.available, "running": self._ready,
                "applio_dir": str(self.root), "model": str(self.model) if self.model else None,
                "index": str(self.index) if self.index else None, "error": self.last_error}

    # ---------------------------------------------------------------- worker
    def _start(self, timeout: float) -> None:
        if self._proc and self._proc.poll() is None and self._ready:
            return
        self.close()
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(self.tmp_dir / "applio_worker.log", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        # fixed interpreter + fixed script, no shell; the worker only reads/writes files we name
        self._proc = subprocess.Popen(
            [str(self.python), str(WORKER), str(self.root / "app"), str(self.runtime)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._replies = queue.Queue()
        threading.Thread(target=self._read, args=(self._proc, self._replies), daemon=True,
                         name="applio-reader").start()
        hello = self._reply(timeout)
        if not hello.get("ready"):
            raise RuntimeError(hello.get("error") or "Applio worker did not start")
        self._ready = True
        log.info("Applio RVC worker started (model %s)", self.model.name)

    @staticmethod
    def _read(proc: subprocess.Popen, replies: queue.Queue) -> None:
        for line in proc.stdout:
            try:
                replies.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        replies.put({"ok": False, "ready": False, "error": f"Applio worker exited ({proc.wait()})"})

    def _reply(self, timeout: float) -> dict:
        try:
            return self._replies.get(timeout=timeout)
        except queue.Empty:
            self.close()  # a stuck worker is restarted on the next phrase
            raise TimeoutError(f"Applio did not answer within {timeout:.0f} s") from None

    def close(self) -> None:
        self._ready = False
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    # ------------------------------------------------------------ conversion
    def convert(self, audio, sample_rate: int, timeout: float | None = None):
        """Returns (converted mono float32 audio, sample_rate), or None (caller keeps the Silero voice)."""
        import numpy as np
        if not self.available or not len(audio):
            return None
        timeout = timeout or float(self.cfg.get("timeout_s", 30))
        name = uuid.uuid4().hex[:12]
        src, dst = self.tmp_dir / f"{name}.wav", self.tmp_dir / f"{name}.npy"
        with self._lock:
            try:
                self._start(timeout=float(self.cfg.get("start_timeout_s", 120)))
                self.tmp_dir.mkdir(parents=True, exist_ok=True)
                with wave.open(str(src), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sample_rate)
                    w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
                req = {"input": str(src), "output": str(dst), "model": str(self.model),
                       "index": str(self.index) if self.index else "", **self.params()}
                self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                reply = self._reply(timeout)
                if not reply.get("ok"):
                    raise RuntimeError(reply.get("error") or "conversion failed")
                out = np.load(dst).astype("float32")
                self.last_error = None
                return out, int(reply["sample_rate"])
            except Exception as e:  # noqa: BLE001 - voice conversion is optional
                self.last_error = str(e)
                log.warning("RVC conversion failed, using the plain Silero voice: %s", e)
                if self._proc and self._proc.poll() is not None:
                    self._ready = False
                return None
            finally:
                src.unlink(missing_ok=True)
                dst.unlink(missing_ok=True)
