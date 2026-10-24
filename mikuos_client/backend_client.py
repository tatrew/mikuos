"""BackendClient: Qt-native WebSocket + REST connection to the local backend.

Reconnects automatically; the overlay keeps animating while the backend or
LM Studio is down and shows the connection state.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

log = logging.getLogger("mikuos.client.backend")


class ConnState:
    BACKEND_OFFLINE = "backend_offline"
    LLM_DOWN = "llm_down"
    ONLINE = "online"


class BackendClient(QObject):
    state_changed = Signal(str)
    status_received = Signal(dict)
    token = Signal(str, object)  # text, turn_id
    reset = Signal(object)
    tool_event = Signal(dict)
    done = Signal(dict)
    error = Signal(dict)
    event = Signal(dict)  # bus events: initiative_message, permission_request, ...

    def __init__(self, port: int, token_path: Path, parent: QObject | None = None, reconnect_ms: int = 3000):
        super().__init__(parent)
        self.port = port
        self.token_path = Path(token_path)
        self.token_value: str | None = None
        self.state = ConnState.BACKEND_OFFLINE
        self.status: dict = {}
        self.ws = QWebSocket()
        self.ws.textMessageReceived.connect(self._on_message)
        self.ws.connected.connect(self._on_connected)
        self.ws.disconnected.connect(self._on_disconnected)
        self.net = QNetworkAccessManager(self)
        self._timer = QTimer(self)
        self._timer.setInterval(reconnect_ms)
        self._timer.timeout.connect(self._tick)
        self._turn = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()
        self.ws.close()

    def _set_state(self, st: str) -> None:
        if st != self.state:
            self.state = st
            self.state_changed.emit(st)

    def _tick(self) -> None:
        if self.ws.state().name in ("ConnectedState", "ConnectingState"):
            if self.state != ConnState.BACKEND_OFFLINE:
                self.get("/api/status", self._apply_status)
            return
        try:
            self.token_value = self.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            self._set_state(ConnState.BACKEND_OFFLINE)
            return
        self.ws.open(QUrl(f"ws://127.0.0.1:{self.port}/ws?token={self.token_value}"))

    def _on_connected(self) -> None:
        log.info("connected to backend")

    def _on_disconnected(self) -> None:
        self._set_state(ConnState.BACKEND_OFFLINE)

    def _apply_status(self, st: Any) -> None:
        if not isinstance(st, dict):
            return
        self.status = st
        self.status_received.emit(st)
        self._set_state(ConnState.ONLINE if st.get("llm", {}).get("connected") else ConnState.LLM_DOWN)

    def _on_message(self, raw: str) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        t = m.get("type")
        if t == "hello":
            self._apply_status(m.get("status"))
        elif t == "token":
            self.token.emit(m.get("text", ""), m.get("turn_id"))
        elif t == "reset":
            self.reset.emit(m.get("turn_id"))
        elif t in ("tool_call", "tool_result"):
            self.tool_event.emit(m)
        elif t == "done":
            self.done.emit(m)
        elif t == "error":
            self.error.emit(m)
        elif t == "event":
            ev = m.get("event", {})
            if ev.get("type") == "status":
                self._apply_status(ev.get("data"))
            elif ev.get("type") == "llm_status":
                st = dict(self.status)
                st["llm"] = ev.get("data", {})
                self._apply_status(st)
            self.event.emit(ev)

    # ---------------------------------------------------------------- API
    def send_chat(self, text: str) -> int | None:
        if self.state == ConnState.BACKEND_OFFLINE:
            return None
        self._turn += 1
        self.ws.sendTextMessage(json.dumps({"type": "chat", "text": text, "turn_id": self._turn}))
        return self._turn

    def send_pet_event(self, kind: str) -> None:
        if self.state != ConnState.BACKEND_OFFLINE:
            self.ws.sendTextMessage(json.dumps({"type": "pet_event", "kind": kind}))

    def _request(self, method: str, path: str, body: Any = None, cb: Callable[[Any], None] | None = None) -> None:
        req = QNetworkRequest(QUrl(self.base + path))
        req.setRawHeader(b"X-MikuOS-Token", (self.token_value or "").encode())
        req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        data = QByteArray(json.dumps(body if body is not None else {}).encode())
        if method == "GET":
            reply = self.net.get(req)
        elif method == "POST":
            reply = self.net.post(req, data)
        else:
            reply = self.net.sendCustomRequest(req, method.encode(), data)

        def finished():
            payload: Any = None
            if reply.error() == QNetworkReply.NoError:
                try:
                    payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
                except ValueError:
                    payload = None
            else:
                payload = {"error": reply.errorString()}
            reply.deleteLater()
            if cb:
                cb(payload)

        reply.finished.connect(finished)

    def get(self, path: str, cb: Callable[[Any], None]) -> None:
        self._request("GET", path, None, cb)

    def post(self, path: str, body: Any = None, cb: Callable[[Any], None] | None = None) -> None:
        self._request("POST", path, body, cb)

    def patch(self, path: str, body: Any, cb: Callable[[Any], None] | None = None) -> None:
        self._request("PATCH", path, body, cb)
