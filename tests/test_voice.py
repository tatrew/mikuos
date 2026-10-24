"""Voice: text preparation for TTS, the push-to-talk turn (record -> STT -> chat -> TTS)
with stubbed audio devices, emergency stop, and a real Silero -> faster-whisper round trip."""
import asyncio

import pytest

from mikuos.events import EventBus
from mikuos.voice import VoiceService, voice_dependencies
from mikuos.voice.text import prepare, spell_number

np = pytest.importorskip("numpy")


def test_numbers_and_text_cleanup():
    assert spell_number(0) == "ноль"
    assert spell_number(21) == "двадцать один"
    assert spell_number(2022) == "две тысячи двадцать два"
    assert spell_number(21000) == "двадцать одна тысяча"
    assert spell_number(1_000_000) == "один миллион"
    chunks = prepare("Включила **World is Mine** 🎵 окно 2560x1392, ссылка https://example.com/x?y=1")
    text = " ".join(chunks)
    assert "*" not in text and "🎵" not in text and "https" not in text and "example" not in text
    assert "майн" in text and "две тысячи пятьсот шестьдесят на одна тысяча" in text
    assert prepare("🎵🎵 :)") == []  # nothing speakable
    long = prepare("Слово. " * 300, max_chunk=200)
    assert len(long) > 5 and all(len(c) <= 200 for c in long)


class FakeSTT:
    loaded = True

    def __init__(self, text):
        self.text = text

    def transcribe(self, audio):
        assert isinstance(audio, np.ndarray)
        return self.text


class FakeTTS:
    loaded = True

    def synthesize(self, text):
        return np.full(2400, 0.3, dtype="float32"), 48000


def make_voice(tmp_settings, heard="привет, Мику", reply="Привет!", blocked=lambda: False, audio=True):
    tmp_settings.update({"voice": {"enabled": True, "speak_replies": "all"}})
    bus = EventBus()
    events, chats, played = [], [], []
    bus.subscribe("*", lambda ev: events.append((ev.type, dict(ev.data))))

    async def chat(text):
        chats.append(text)
        return reply

    v = VoiceService(tmp_settings, bus, chat, tmp_settings.resolve_path("models"), blocked=blocked)
    v.stt, v.tts = FakeSTT(heard), FakeTTS()
    v._record = lambda: np.zeros(16000, dtype="float32") if audio else None  # no real microphone in tests

    def play(a, sr, sid):
        bus.publish_threadsafe("voice_playback", {"event": "start", "stream_id": sid})
        played.append(len(a))
        bus.publish_threadsafe("voice_playback", {"event": "end", "stream_id": sid})

    v._play = play
    type(v).available = property(lambda self: True)
    return v, bus, events, chats, played


async def _turn(v, bus):
    bus.bind_loop(asyncio.get_running_loop())
    assert v.toggle() == "started"
    assert v.toggle() == "stopping"  # second press while listening finishes the recording
    await v._turn
    await asyncio.sleep(0.05)


@pytest.mark.skipif(not voice_dependencies()["numpy"], reason="numpy missing")
async def test_voice_turn_records_recognizes_answers_and_speaks(tmp_settings):
    v, bus, events, chats, played = make_voice(tmp_settings)
    await _turn(v, bus)
    states = [d["state"] for t, d in events if t == "voice_state"]
    assert states[:3] == ["listening", "recognizing", "thinking"] and "speaking" in states and states[-1] == "idle"
    assert chats == ["привет, Мику"]
    assert ("voice_transcript", {"text": "привет, Мику"}) in events
    assert any(t == "assistant_message" and d["kind"] == "voice_reply" and d["text"] == "Привет!" for t, d in events)
    assert played == [2400]
    pb = [d["event"] for t, d in events if t == "voice_playback"]
    assert pb == ["start", "end"] and v.state == "idle"


async def test_no_speech_does_not_reach_the_model(tmp_settings):
    v, bus, events, chats, played = make_voice(tmp_settings, audio=False)
    bus.bind_loop(asyncio.get_running_loop())
    assert v.toggle() == "started"
    await v._turn
    assert chats == [] and played == []
    assert ("voice_state", {"state": "idle", "result": "no_speech"}) in events


async def test_emergency_stop_blocks_the_microphone(tmp_settings):
    v, bus, events, chats, played = make_voice(tmp_settings, blocked=lambda: True)
    bus.bind_loop(asyncio.get_running_loop())
    assert v.toggle() == "blocked"
    assert not await v.speak("привет")
    assert chats == [] and played == [] and not events


async def test_voice_is_not_a_model_tool(tmp_settings):
    from helpers import FakeBridge, ScriptedLLM

    from mikuos.app import MikuOS
    app = MikuOS(tmp_settings, bridge=FakeBridge(), llm=ScriptedLLM([]), enable_hotkey=False)
    names = {t.name for t in app.registry.tools.values()}
    assert not any("voice" in n or "listen" in n or "micro" in n or "record" in n for n in names)
    assert "voice" in app.status()


@pytest.mark.skipif(not all(voice_dependencies().values()), reason="voice dependencies not installed")
def test_real_silero_to_whisper_round_trip(tmp_settings):
    """Real models (downloaded once into data/models): synthesize a phrase, recognize it back."""
    from mikuos.config import PROJECT_ROOT
    from mikuos.voice.stt import SpeechRecognizer
    from mikuos.voice.tts import SpeechSynthesizer
    models = PROJECT_ROOT / "data" / "models"
    if not (models / "silero").exists() or not (models / "whisper").exists():
        pytest.skip("speech models not downloaded yet")
    audio, sr = SpeechSynthesizer(models / "silero").synthesize("Мику, включи следующий трек.")
    assert sr == 48000 and len(audio) > sr
    audio16 = audio[: len(audio) // 3 * 3].reshape(-1, 3).mean(axis=1)
    text = SpeechRecognizer("small", models / "whisper").transcribe(audio16).lower()
    assert "следующ" in text and "трек" in text
