"""Real chat round-trip through the Qt chat window -> WebSocket -> backend -> LM Studio."""
import pytest

from test_live_lmstudio import _lmstudio_ready  # noqa: F401  (module-level skip happens there)

pytestmark = [pytest.mark.live, pytest.mark.desktop]


def test_chat_window_streams_real_reply(tmp_settings):
    import os
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from conftest import free_port
    from mikuos_client.backend_client import BackendClient, ConnState
    from mikuos_client.chat_window import ChatWindow
    from test_overlay import _serve

    if not _lmstudio_ready():
        pytest.skip("LM Studio not ready")
    app = QApplication.instance() or QApplication([])
    port = free_port()
    tmp_settings.update({"server": {"port": port}, "hotkey": {"emergency_stop": "ctrl+alt+shift+f9",
                                                              "fallbacks": []}})
    server, thread = _serve(tmp_settings, port)
    backend = BackendClient(port, tmp_settings.resolve_path("runtime") / "token", reconnect_ms=300)
    chat = ChatWindow(backend)
    backend.start()
    try:
        for _ in range(60):
            QTest.qWait(250)
            if backend.state == ConnState.ONLINE:
                break
        assert backend.state == ConnState.ONLINE
        tokens = []
        backend.token.connect(lambda t, _: tokens.append(t))
        done, errors, events = [], [], []
        backend.done.connect(done.append)
        backend.error.connect(errors.append)
        backend.tool_event.connect(events.append)
        chat.input.setText("Привет, Мику! Ответь одной фразой.")
        chat.send()
        for _ in range(240):
            QTest.qWait(250)
            if done or errors:
                break
        assert not errors, errors
        assert done and done[0]["text"], f"no reply; tokens={len(tokens)} tools={events} ws={backend.state}"
        assert tokens, "reply must stream token by token"
        assert chat.entries[-1][0] == "Мику" and chat.entries[-1][1] == done[0]["text"]
        print("\nchat window reply:", done[0]["text"])
    finally:
        backend.stop()
        server.should_exit = True
        thread.join(10)
