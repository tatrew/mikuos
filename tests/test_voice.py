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


# ------------------------------------------------------------ Applio (RVC)
from mikuos.voice.rvc import ApplioConverter, default_applio_dir  # noqa: E402


def fake_applio(root, python=None):
    rt = root / "data" / "runtime"
    (root / "app" / "backend" / "applio").mkdir(parents=True)
    (rt / "logs" / "MikuAI_e210_s6300").mkdir(parents=True)
    (rt / "logs" / "IVF1811_Flat_MikuAI").mkdir()
    (rt / "logs" / "Other_e10_s100").mkdir()
    (rt / "logs" / "MikuAI_e210_s6300" / "MikuAI_e210_s6300.pth").write_bytes(b"x")
    (rt / "logs" / "Other_e10_s100" / "Other_e10_s100.pth").write_bytes(b"x")
    (rt / "logs" / "IVF1811_Flat_MikuAI" / "added_IVF1811_Flat_nprobe_1_MikuAI_v2.index").write_bytes(b"x")
    (rt / ".venv" / "Scripts").mkdir(parents=True)
    if python is None:
        (rt / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    import json
    (root / "data" / "model-presets.json").write_text(json.dumps({"presets": {
        "realtime:logs/MikuAI_e210_s6300/MikuAI_e210_s6300.pth": {"f0_method": "fcpe", "index_rate": 0, "pitch": 2}}}))
    return rt


def test_applio_model_discovery_and_presets(tmp_path):
    rt = fake_applio(tmp_path)
    c = ApplioConverter({"applio_dir": str(tmp_path), "protect": 0.4}, tmp_path / "tmp")
    assert c.available and c.model.name == "MikuAI_e210_s6300.pth"
    assert c.index.name == "added_IVF1811_Flat_nprobe_1_MikuAI_v2.index"
    # the user's Applio preset wins over defaults, MikuOS settings win over the preset
    assert c.params() == {"pitch": 2, "f0_method": "fcpe", "index_rate": 0, "protect": 0.4,
                          "volume_envelope": 1.0, "embedder_model": "contentvec"}
    explicit = ApplioConverter({"applio_dir": str(tmp_path), "model": "logs/Other_e10_s100/Other_e10_s100.pth"},
                               tmp_path / "tmp")
    assert explicit.model == rt / "logs" / "Other_e10_s100" / "Other_e10_s100.pth" and explicit.index is None
    assert not ApplioConverter({"applio_dir": str(tmp_path), "enabled": False}, tmp_path).available
    assert not ApplioConverter({"applio_dir": str(tmp_path / "nope")}, tmp_path).available
    assert default_applio_dir().name == "Applio"


@pytest.mark.skipif(not voice_dependencies()["numpy"], reason="numpy missing")
async def test_speech_goes_through_rvc_and_falls_back_to_silero(tmp_settings):
    class FakeRVC:
        available, calls, result = True, [], None

        def convert(self, audio, sr):
            self.calls.append((len(audio), sr))
            return self.result

        def status(self):
            return {}

    v, bus, events, chats, played = make_voice(tmp_settings)
    bus.bind_loop(asyncio.get_running_loop())
    v.rvc = FakeRVC()
    FakeRVC.result = (np.full(1000, 0.2, dtype="float32"), 40000)
    assert await v.speak("привет")
    FakeRVC.result = None  # Applio failed: the Silero voice is played instead of silence
    assert await v.speak("привет")
    assert v.rvc.calls == [(2400, 48000), (2400, 48000)] and played == [1000, 2400]


def test_broken_applio_worker_is_reported_not_fatal(tmp_path):
    """Real worker process and protocol, but no Applio engine behind it: convert() returns None."""
    import shutil
    import sys
    fake_applio(tmp_path, python=False)
    shutil.copy(sys.executable, tmp_path / "data" / "runtime" / ".venv" / "Scripts" / "python.exe")
    c = ApplioConverter({"applio_dir": str(tmp_path), "start_timeout_s": 60}, tmp_path / "tmp")
    assert c.available
    assert c.convert(np.zeros(4800, dtype="float32"), 48000) is None
    assert c.last_error and c.status()["running"] is False
    assert not list((tmp_path / "tmp").glob("*.wav"))  # temp audio is cleaned up
    c.close()


@pytest.mark.skipif(not all(voice_dependencies().values()), reason="voice dependencies not installed")
def test_real_silero_through_applio_is_still_understandable(tmp_settings):
    """Real Applio install + Miku RVC model (skipped if absent): the converted voice keeps its length
    and Whisper still recognizes the words."""
    from mikuos.config import PROJECT_ROOT
    from mikuos.voice.stt import SpeechRecognizer
    from mikuos.voice.tts import SpeechSynthesizer
    c = ApplioConverter({}, tmp_settings.resolve_path("models").parent / "tmp" / "rvc")
    models = PROJECT_ROOT / "data" / "models"
    if not c.available or not (models / "silero").exists() or not (models / "whisper").exists():
        pytest.skip("Applio with a Miku model, or speech models, not installed")
    try:
        audio, sr = SpeechSynthesizer(models / "silero").synthesize("Мику, включи следующий трек.")
        out = c.convert(audio, sr, timeout=240)
        assert out is not None, c.last_error
        conv, csr = out
        assert abs(len(conv) / csr - len(audio) / sr) < 0.3 and float(np.abs(conv).max()) > 0.05
        idx = (np.arange(int(len(conv) * 16000 / csr)) * csr / 16000).astype(int)
        text = SpeechRecognizer("small", models / "whisper").transcribe(conv[idx]).lower()
        # observed: "мику, включи следующий тик." - RVC slightly blurs some words, the phrase stays clear
        assert "мику" in text and "включи" in text and "следующ" in text, text
    finally:
        c.close()
