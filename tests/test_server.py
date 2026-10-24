"""API security (token / origin / host) and endpoints, using FastAPI's in-process client."""
import pytest
from fastapi.testclient import TestClient

from helpers import FakeBridge, ScriptedLLM, text
from mikuos.app import MikuOS
from mikuos.server import create_app

TOKEN = "test-token-123"


@pytest.fixture
def client(tmp_settings):
    core = MikuOS(tmp_settings, bridge=FakeBridge(), llm=ScriptedLLM([text("Привет! Я Мику.")]), enable_hotkey=False)
    app = create_app(tmp_settings, core=core, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.core = core
        yield c


H = {"X-MikuOS-Token": TOKEN}
WSH = {"host": "127.0.0.1:8765"}  # TestClient sends "testserver" for websockets


def test_character_commands_are_validated_and_broadcast(client):
    payload = {'emotion': 'happy', 'intensity': .75, 'animation': 'wave', 'duration': 2.5}
    assert client.post('/api/character/command', json=payload).status_code == 401
    assert client.post('/api/character/command', json={'bone': 'Neck'}, headers=H).status_code == 422
    assert client.post('/api/character/command', json={'action': 'sit_on_window'}, headers=H).status_code == 422
    with client.websocket_connect(f'/ws?token={TOKEN}', headers=WSH) as ws:
        ws.receive_json()
        assert client.post('/api/character/command', json=payload, headers=H).status_code == 200
        event = ws.receive_json()
        assert event['event']['type'] == 'character_command'
        assert event['event']['data']['animation'] == 'wave'
        assert client.post('/api/character/voice', json={'event': 'energy', 'stream_id': 'test', 'rms': .3}, headers=H).status_code == 200
        assert ws.receive_json()['event']['type'] == 'voice_playback'
    client.post('/api/emergency_stop', headers=H)
    assert client.post('/api/character/command', json=payload, headers=H).status_code == 409


def test_auth_origin_host(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-MikuOS-Token": "wrong"}).status_code == 401
    assert client.get("/api/status", headers={**H, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/status", headers={**H, "Host": "evil.example:8765"}).status_code == 403
    r = client.get("/api/status", headers=H)
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
    assert r.json()["llm"]["connected"] is True


def test_chat_and_memory_endpoints(client):
    r = client.post("/api/chat", json={"text": "Привет"}, headers=H)
    assert r.status_code == 200 and r.json()["text"] == "Привет! Я Мику."
    r = client.post("/api/memory", json={"content": "Пользователь любит мандарины"}, headers=H)
    mid = r.json()["memory_id"]
    assert client.get("/api/memory/search", params={"q": "мандарины"}, headers=H).json()[0]["id"] == mid
    r = client.patch(f"/api/memory/{mid}", json={"content": "Пользователь любит апельсины"}, headers=H)
    assert r.json()["content"].endswith("апельсины")
    assert client.delete(f"/api/memory/{mid}", headers=H).status_code == 200
    assert client.delete(f"/api/memory/{mid}", headers=H).status_code == 404
    assert len(client.get("/api/lore", headers=H).json()) >= 8
    msgs = client.get(f"/api/conversations/{client.core.conversation_id}/messages", headers=H).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_settings_tools_estop(client, tmp_settings):
    r = client.patch("/api/settings", json={"initiative": {"mode": "quiet"}}, headers=H)
    assert r.json()["initiative"]["mode"] == "quiet" and tmp_settings.get("initiative", "mode") == "quiet"
    assert client.patch("/api/settings", json={"evil": 1}, headers=H).status_code == 400
    tools = client.get("/api/tools", headers=H).json()
    assert any(t["name"] == "open_application" for t in tools)
    r = client.post("/api/tools/get_system_time", json={}, headers=H)
    assert r.json()["status"] == "ok"
    st = client.post("/api/emergency_stop", headers=H).json()
    assert st["emergency_stop"] is True
    assert client.post("/api/tools/open_application", json={"app_id": "notepad"}, headers=H).json()["status"] == \
        "denied"
    assert client.post("/api/resume", headers=H).json()["emergency_stop"] is False


def test_websocket_auth_and_stream(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=bad", headers=WSH) as ws:
            ws.receive_text()
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws?token={TOKEN}", headers={**WSH, "Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    client.core.llm.script = [text("Привет из потока")]
    with client.websocket_connect(f"/ws?token={TOKEN}", headers=WSH) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "chat", "text": "привет", "turn_id": 7})
        seen = []
        while True:
            m = ws.receive_json()
            if m.get("type") == "event":
                continue
            seen.append(m)
            if m["type"] in ("done", "error"):
                break
        assert seen[-1]["type"] == "done" and seen[-1]["text"] == "Привет из потока" and seen[-1]["turn_id"] == 7
        assert any(m["type"] == "token" for m in seen)
