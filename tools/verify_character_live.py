"""Bounded live overlay check; writes metrics, does not start or query an LLM."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from mikuos.config import Settings
from mikuos_client.app import OverlayApp

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
settings = Settings(ROOT / 'data/validation/live-settings.json', root=ROOT, use_env=False)
settings.update({'ui': {'avatar': '3d', 'fps': 30}, 'server': {'port': 65419},
                 'hotkey': {'emergency_stop': 'ctrl+alt+shift+f10', 'fallbacks': []}})
overlay = OverlayApp(app, settings)
overlay.ctrl.cfg.sleep_after_idle_s = float('inf')
overlay.start()


def events():
    overlay.backend._on_message(json.dumps({'type': 'event', 'event': {
        'type': 'character_command', 'data': {'emotion': 'happy', 'animation': 'wave', 'duration': 2}}}))
    assert overlay.pet.emotion.emotion == 'happy'
    assert overlay.ctrl.pending_action['action'] == 'wave'
    overlay.backend._on_message(json.dumps({'type': 'event', 'event': {
        'type': 'voice_playback', 'data': {'event': 'start', 'stream_id': 'validation'}}}))
    assert overlay.pet.voice.active
    overlay.backend._on_message(json.dumps({'type': 'event', 'event': {
        'type': 'voice_playback', 'data': {'event': 'end', 'stream_id': 'validation'}}}))
    assert not overlay.pet.voice.active


def finish():
    report = {'frames': overlay.frames, 'metrics': overlay.diagnostics,
              'renderer': type(overlay.pet).__name__, 'backend_state': overlay.backend.state,
              'monitors': overlay.bridge.monitors(), 'event_decode': 'passed'}
    (ROOT / 'data/validation/live-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    overlay.pet.quick.grabFramebuffer().save(str(ROOT / 'data/validation/live-overlay.png'))
    print(json.dumps(report))
    overlay.pet.close()
    overlay.quit()


QTimer.singleShot(1000, events)
QTimer.singleShot(11000, finish)
app.exec()
