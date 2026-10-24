"""Speech-to-text with faster-whisper on the CPU (int8), so the GPU stays free for LM Studio.
The model is downloaded once from Hugging Face into data/models/whisper."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger("mikuos.voice.stt")


class SpeechRecognizer:
    def __init__(self, model_size: str = "small", models_dir: Path | None = None, language: str = "ru",
                 threads: int = 4):
        self.model_size = model_size
        self.models_dir = models_dir
        self.language = language
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                kw = dict(device="cpu", compute_type="int8", cpu_threads=self.threads,
                          download_root=str(self.models_dir) if self.models_dir else None)
                try:  # already downloaded: no network request at all
                    self._model = WhisperModel(self.model_size, local_files_only=True, **kw)
                except Exception:  # noqa: BLE001 - not cached yet (huggingface_hub raises various errors)
                    log.info("downloading faster-whisper '%s' (one time)", self.model_size)
                    self._model = WhisperModel(self.model_size, **kw)
            return self._model

    def transcribe(self, audio, sample_rate: int = 16000) -> str:
        """audio: mono float32 numpy array in [-1, 1] at 16 kHz."""
        if sample_rate != 16000:
            raise ValueError("faster-whisper expects 16 kHz audio")
        model = self.load()
        segments, _info = model.transcribe(audio, language=self.language, beam_size=1, vad_filter=True,
                                           condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()
