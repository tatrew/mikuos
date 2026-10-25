"""VoiceService: one push-to-talk turn = record -> recognize -> chat -> speak.

States (published as `voice_state`): idle | listening | recognizing | thinking | speaking.
The overlay shows a microphone indicator while `listening`. Playback publishes
`voice_playback` start/energy/end (same contract the avatars already use for lip sync).
"""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from .rvc import ApplioConverter
from .stt import SpeechRecognizer
from .tts import SpeechSynthesizer

log = logging.getLogger("mikuos.voice")

ChatFn = Callable[[str], Awaitable[str]]


@functools.lru_cache(maxsize=1)
def _dependencies() -> tuple[tuple[str, bool], ...]:
    # find_spec walks sys.path on the filesystem: do it once, not on every status() poll
    return tuple((m, importlib.util.find_spec(m) is not None) for m in ("numpy", "sounddevice", "faster_whisper", "torch"))


def voice_dependencies() -> dict[str, bool]:
    return dict(_dependencies())


class VoiceService:
    def __init__(self, settings, bus, chat: ChatFn, models_dir: Path, blocked: Callable[[], bool] = lambda: False):
        self.settings = settings
        self.bus = bus
        self.chat = chat
        self.blocked = blocked  # emergency stop
        cfg = self.cfg
        self.stt = SpeechRecognizer(cfg.get("stt_model", "small"), models_dir / "whisper", cfg.get("language", "ru"))
        self.tts = SpeechSynthesizer(models_dir / "silero", cfg.get("tts_speaker", "xenia"))
        self.rvc = ApplioConverter(cfg.get("rvc") or {}, models_dir.parent / "tmp" / "rvc")
        self.state = "idle"
        self.last_error: str | None = None
        self._stop_recording = threading.Event()
        self._stop_playback = threading.Event()
        self._turn: asyncio.Task | None = None

    @property
    def cfg(self) -> dict:
        return self.settings.get("voice") or {}

    @property
    def available(self) -> bool:
        return bool(self.cfg.get("enabled", True)) and all(voice_dependencies().values())

    def status(self) -> dict:
        return {"available": self.available, "state": self.state, "dependencies": voice_dependencies(),
                "stt_loaded": self.stt.loaded, "tts_loaded": self.tts.loaded, "error": self.last_error,
                "hotkey": self.cfg.get("hotkey"), "speak_replies": self.cfg.get("speak_replies", "all"),
                "rvc": self.rvc.status()}

    async def _set_state(self, state: str, **extra) -> None:
        self.state = state
        await self.bus.publish("voice_state", {"state": state, **extra})

    # ---------------------------------------------------------------- input
    def toggle(self) -> str:
        """Hotkey / button: start a voice turn, or finish recording early if listening."""
        if self.state == "listening":
            self._stop_recording.set()
            return "stopping"
        if self.state == "speaking":
            self.stop_speaking()
            return "stopped_speaking"
        if self._turn and not self._turn.done():
            return "busy"
        if not self.available:
            return "unavailable"
        if self.blocked():
            return "blocked"
        # set synchronously: a quick second press must already see "listening"
        self._stop_recording.clear()
        self.state = "listening"
        self._turn = asyncio.get_running_loop().create_task(self._voice_turn(), name="voice-turn")
        return "started"

    async def _voice_turn(self) -> None:
        try:
            await self._set_state("listening")
            audio = await asyncio.to_thread(self._record)
            if audio is None or self.blocked():
                await self._set_state("idle", result="no_speech")
                return
            await self._set_state("recognizing")
            text = await asyncio.to_thread(self.stt.transcribe, audio)
            if not text:
                await self._set_state("idle", result="no_speech")
                return
            await self.bus.publish("voice_transcript", {"text": text})
            await self._set_state("thinking")
            reply = await self.chat(text)
            await self.bus.publish("assistant_message", {"text": reply, "kind": "voice_reply", "user_text": text})
            if reply and self.cfg.get("speak_replies", "all") != "off":
                await self.speak(reply)
        except Exception as e:  # noqa: BLE001 - the turn must always end in "idle"
            self.last_error = str(e)
            log.exception("voice turn failed")
            await self.bus.publish("voice_error", {"error": str(e)})
        finally:
            if self.state != "idle":
                await self._set_state("idle")

    def _record(self):
        """Record from the default microphone until a pause after speech, the max length,
        or a second press. Audio stays in memory only."""
        import numpy as np
        import sounddevice as sd
        sr, block = 16000, 1600  # 100 ms blocks
        max_s = float(self.cfg.get("max_record_s", 15))
        silence_s = float(self.cfg.get("silence_s", 1.2))
        chunks, voiced, quiet, noise = [], False, 0.0, []
        started = time.monotonic()
        with sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block) as stream:
            while not self._stop_recording.is_set() and time.monotonic() - started < max_s:
                if self.blocked():
                    return None
                data, _ = stream.read(block)
                mono = data[:, 0].copy()
                chunks.append(mono)
                rms = float(np.sqrt(np.mean(mono ** 2)))
                if len(noise) < 4:  # first 0.4 s: background level
                    noise.append(rms)
                    continue
                threshold = max(0.012, 3.0 * float(np.median(noise)))
                if rms > threshold:
                    voiced, quiet = True, 0.0
                elif voiced:
                    quiet += block / sr
                    if quiet >= silence_s:
                        break
                elif time.monotonic() - started > 6:  # nothing said at all
                    return None
        if not voiced and not self._stop_recording.is_set():
            return None
        audio = np.concatenate(chunks) if chunks else None
        return audio if audio is not None and len(audio) > sr // 2 else None

    # --------------------------------------------------------------- output
    async def speak(self, text: str) -> bool:
        if not self.available or self.blocked() or not text:
            return False
        audio, sr = await asyncio.to_thread(self.tts.synthesize, text)
        if not len(audio):
            return False
        if self.rvc.available:  # Silero -> Applio (RVC) voice; on any failure the Silero voice is kept
            converted = await asyncio.to_thread(self.rvc.convert, audio, sr)
            if converted is not None:
                audio, sr = converted
            if self.blocked():
                return False
        await self._set_state("speaking")
        sid = uuid.uuid4().hex[:12]
        self._stop_playback.clear()
        try:
            await asyncio.to_thread(self._play, audio, sr, sid)
        finally:
            if self.state == "speaking":
                await self._set_state("idle")
        return True

    def _play(self, audio, sr: int, sid: str) -> None:
        import numpy as np
        import sounddevice as sd
        volume = float(self.cfg.get("volume", 1.0))
        audio = np.clip(audio * volume, -1, 1).astype("float32")
        pos, block, last_sent = 0, sr // 20, 0.0  # 50 ms blocks
        done = threading.Event()
        self.bus.publish_threadsafe("voice_playback", {"event": "start", "stream_id": sid})

        def callback(out, frames, _time, _status):
            nonlocal pos, last_sent
            chunk = audio[pos:pos + frames]
            out[:len(chunk), 0] = chunk
            out[len(chunk):, 0] = 0
            pos += frames
            now = time.monotonic()
            if now - last_sent > 0.045 and len(chunk):
                last_sent = now
                rms = float(np.sqrt(np.mean(chunk ** 2)))
                self.bus.publish_threadsafe("voice_playback", {"event": "energy", "stream_id": sid,
                                                               "rms": min(1.0, rms * 4)})
            if pos >= len(audio) or self._stop_playback.is_set() or self.blocked():
                raise sd.CallbackStop

        try:
            with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block, callback=callback,
                                 finished_callback=done.set):
                done.wait(timeout=len(audio) / sr + 5)
        finally:
            self.bus.publish_threadsafe("voice_playback", {"event": "end", "stream_id": sid})

    def stop_speaking(self) -> None:
        self._stop_playback.set()

    def cancel(self) -> None:
        """Emergency stop: close the microphone and stop talking right away."""
        self._stop_recording.set()
        self._stop_playback.set()

    def warm_up_rvc(self) -> bool:
        """Start the Applio worker and run one short phrase: the first conversion compiles GPU
        kernels and can take ~30 s, so it happens at startup instead of on the first reply."""
        if not self.rvc.available:
            return False
        audio, sr = self.tts.synthesize("Привет.")
        return self.rvc.convert(audio, sr, timeout=float(self.cfg.get("rvc", {}).get("warmup_timeout_s", 180))) is not None

    def close(self) -> None:
        self.cancel()
        self.rvc.close()
