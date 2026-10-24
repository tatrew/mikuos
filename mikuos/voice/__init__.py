"""Voice: push-to-talk speech recognition (faster-whisper) and speech synthesis (Silero TTS).

The microphone is opened only after an explicit user action (hotkey / button) and
closes after the phrase; audio is never written to disk. The model cannot start
listening: there is no tool for it.
"""
from .service import VoiceService, voice_dependencies

__all__ = ["VoiceService", "voice_dependencies"]
