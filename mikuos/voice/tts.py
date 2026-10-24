"""Text-to-speech with Silero TTS (Russian, CPU). The model file comes from the official
Silero server (models.silero.ai) once and is cached in data/models/silero.
Silero models are licensed for non-commercial use."""
from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path

from .text import prepare

log = logging.getLogger("mikuos.voice.tts")

SILERO_URL = "https://models.silero.ai/models/tts/ru/v4_ru.pt"
SPEAKERS = ("xenia", "baya", "kseniya", "aidar", "eugene")


class SpeechSynthesizer:
    def __init__(self, models_dir: Path, speaker: str = "xenia", sample_rate: int = 48000, threads: int = 4,
                 url: str = SILERO_URL):
        self.path = Path(models_dir) / Path(url).name
        self.url = url
        self.speaker = speaker if speaker in SPEAKERS else "xenia"
        self.sample_rate = sample_rate if sample_rate in (8000, 24000, 48000) else 48000
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _download(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".part")
        log.info("downloading Silero TTS model from %s", self.url)
        with urllib.request.urlopen(self.url, timeout=60) as r, open(tmp, "wb") as f:  # noqa: S310 - fixed https URL
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if tmp.stat().st_size < 10_000_000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("Silero model download looks incomplete")
        tmp.replace(self.path)

    def load(self):
        with self._lock:
            if self._model is None:
                import torch
                if not self.path.is_file():
                    self._download()
                torch.set_num_threads(self.threads)
                importer = torch.package.PackageImporter(str(self.path))
                model = importer.load_pickle("tts_models", "model")
                model.to(torch.device("cpu"))
                self._model = model
            return self._model

    def synthesize(self, text: str):
        """Returns (mono float32 numpy array, sample_rate); empty array if nothing speakable."""
        import numpy as np
        model = self.load()
        parts = []
        for chunk in prepare(text):
            audio = model.apply_tts(text=chunk, speaker=self.speaker, sample_rate=self.sample_rate,
                                    put_accent=True, put_yo=True)
            parts.append(audio.numpy().astype("float32"))
            parts.append(np.zeros(int(self.sample_rate * 0.15), dtype="float32"))  # short pause
        return (np.concatenate(parts) if parts else np.zeros(0, dtype="float32")), self.sample_rate
