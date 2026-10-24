"""Chat window: streaming conversation, tool activity, permission and memory confirmations."""
from __future__ import annotations

import html
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

from .backend_client import BackendClient, ConnState

CONN_TEXT = {ConnState.ONLINE: "🟢 LM Studio подключена", ConnState.LLM_DOWN: "🟡 LM Studio недоступна — чат "
             "не отвечает, память доступна", ConnState.BACKEND_OFFLINE: "🔴 backend не запущен"}


class ChatWindow(QWidget):
    def __init__(self, backend: BackendClient):
        super().__init__(None, Qt.Window)
        self.backend = backend
        self.setWindowTitle("MikuOS — чат с Мику")
        self.resize(520, 640)
        self.status = QLabel(CONN_TEXT[backend.state])
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Напиши Мику…")
        self.send_btn = QPushButton("Отправить")
        self.requests = QListWidget()
        self.requests.setMaximumHeight(110)
        self.req_label = QLabel("Ожидают подтверждения:")
        approve, deny = QPushButton("Разрешить"), QPushButton("Отклонить")
        mem_btn, new_btn = QPushButton("Память…"), QPushButton("Новый разговор")

        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn)
        req_row = QHBoxLayout()
        req_row.addWidget(approve)
        req_row.addWidget(deny)
        req_row.addStretch(1)
        req_row.addWidget(new_btn)
        req_row.addWidget(mem_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.status)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.req_label)
        lay.addWidget(self.requests)
        lay.addLayout(req_row)
        lay.addLayout(row)

        self.send_btn.clicked.connect(self.send)
        self.input.returnPressed.connect(self.send)
        approve.clicked.connect(lambda: self._resolve(True))
        deny.clicked.connect(lambda: self._resolve(False))
        mem_btn.clicked.connect(self.open_memory)
        new_btn.clicked.connect(lambda: self.backend.post("/api/conversations/new", {}, lambda _: self._sys(
            "Начат новый разговор.")))
        backend.state_changed.connect(self._on_state)
        backend.token.connect(self._on_token)
        backend.reset.connect(self._on_reset)
        backend.tool_event.connect(self._on_tool)
        backend.done.connect(self._on_done)
        backend.error.connect(self._on_error)
        backend.event.connect(self._on_event)
        self.entries: list[tuple[str | None, str, str]] = []
        self._stream_idx: int | None = None
        self._on_state(backend.state)

    # ----------------------------------------------------------- display
    def _render(self) -> None:
        parts = []
        for who, text, color in self.entries[-120:]:
            body = html.escape(text).replace(chr(10), "<br>")
            if who is None:
                parts.append(f"<p style='color:#888'><i>{body}</i></p>")
            else:
                parts.append(f"<p><b style='color:{color}'>{html.escape(who)}:</b> {body}</p>")
        self.view.setHtml("".join(parts))
        self.view.moveCursor(QTextCursor.End)

    def _append(self, who: str | None, text: str, color: str = "#888") -> None:
        self.entries.append((who, text, color))
        self._render()

    def _sys(self, text: str) -> None:
        self._append(None, text)

    def _on_state(self, st: str) -> None:
        self.status.setText(CONN_TEXT.get(st, st))
        online = st == ConnState.ONLINE
        self.send_btn.setEnabled(online)
        self.input.setEnabled(online)
        if st != ConnState.BACKEND_OFFLINE:
            self.refresh_requests()

    def send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        if self.backend.send_chat(text) is None:
            self._sys("Нет соединения с backend.")
            return
        self.input.clear()
        self._append("Ты", text, "#2c3e50")
        self._start_stream()
        self.send_btn.setEnabled(False)

    def _start_stream(self) -> None:
        self.entries.append(("Мику", "", "#1f8f88"))
        self._stream_idx = len(self.entries) - 1
        self._render()

    def _set_stream(self, text: str) -> None:
        if self._stream_idx is None:
            return
        who, _, color = self.entries[self._stream_idx]
        self.entries[self._stream_idx] = (who, text, color)
        self._render()

    def _on_token(self, text: str, _turn) -> None:
        if self._stream_idx is not None:
            self._set_stream(self.entries[self._stream_idx][1] + text)

    def _on_reset(self, _turn) -> None:
        self._set_stream("")

    def _on_tool(self, m: dict) -> None:
        if m["type"] != "tool_result" or self._stream_idx is None:
            return
        r = m.get("result", {})
        extra = f" — {r.get('error')}" if r.get("error") else ""
        if not self.entries[self._stream_idx][1]:
            self.entries.pop(self._stream_idx)
        self.entries.append((None, f"⚙ {m['name']} → {r.get('status')}{extra}", "#888"))
        self._start_stream()

    def _on_done(self, m: dict) -> None:
        if self._stream_idx is None:
            self._start_stream()
        self._set_stream(m.get("text", ""))
        self._stream_idx = None
        self.send_btn.setEnabled(self.backend.state == ConnState.ONLINE)
        if m.get("pending_requests"):
            self.refresh_requests()

    def _on_error(self, m: dict) -> None:
        if self._stream_idx is not None and not self.entries[self._stream_idx][1]:
            self.entries.pop(self._stream_idx)
        self._stream_idx = None
        self._sys(f"Ошибка: {m.get('message')}")
        self.send_btn.setEnabled(self.backend.state == ConnState.ONLINE)

    def _on_event(self, ev: dict) -> None:
        t, d = ev.get("type"), ev.get("data", {})
        if t in ("initiative_message", "assistant_message"):
            self._append("Мику", d.get("text", ""), "#1f8f88")
        elif t == "permission_request":
            self.refresh_requests()
            self.show()
            self.raise_()
        elif t == "permission_resolved":
            self._sys(f"Действие {d.get('result', {}).get('status')}")
            self.refresh_requests()
        elif t == "memory_changed":
            pend = [o for o in d.get("outcomes", [d]) if o.get("action") == "pending"]
            if pend:
                self._sys("Есть чувствительный факт для памяти — подтвердите в окне «Память…».")
        elif t == "emergency_stop":
            self._sys("⛔ Аварийная остановка " + ("включена" if d.get("active") else "снята"))

    # --------------------------------------------------------- requests
    def refresh_requests(self) -> None:
        def fill(items):
            self.requests.clear()
            for r in items if isinstance(items, list) else []:
                it = QListWidgetItem(f"{r['tool']} {json.dumps(r['arguments'], ensure_ascii=False)} — {r['reason']}")
                it.setData(Qt.UserRole, r["id"])
                self.requests.addItem(it)
            self.req_label.setVisible(self.requests.count() > 0)
            self.requests.setVisible(self.requests.count() > 0)
        self.backend.get("/api/permissions/requests", fill)

    def _resolve(self, approve: bool) -> None:
        it = self.requests.currentItem() or (self.requests.item(0) if self.requests.count() else None)
        if not it:
            return
        rid = it.data(Qt.UserRole)
        self.backend.post(f"/api/permissions/requests/{rid}", {"approve": approve},
                          lambda res: self.refresh_requests())

    def open_memory(self) -> None:
        MemoryDialog(self.backend, self).exec()


class MemoryDialog(QDialog):
    """View, deactivate and delete memories; confirm sensitive ones."""

    def __init__(self, backend: BackendClient, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle("Память MikuOS")
        self.resize(560, 460)
        self.list = QListWidget()
        self.pending = QListWidget()
        self.pending.setMaximumHeight(120)
        forget, delete = QPushButton("Забыть (отключить)"), QPushButton("Удалить навсегда")
        ok, no = QPushButton("Сохранить чувствительный факт"), QPushButton("Отклонить")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Долговременная память (только то, что хранится в SQLite):"))
        lay.addWidget(self.list, 1)
        r1 = QHBoxLayout()
        r1.addWidget(forget)
        r1.addWidget(delete)
        lay.addLayout(r1)
        lay.addWidget(QLabel("Ожидают подтверждения (чувствительные):"))
        lay.addWidget(self.pending)
        r2 = QHBoxLayout()
        r2.addWidget(ok)
        r2.addWidget(no)
        lay.addLayout(r2)
        forget.clicked.connect(lambda: self._mem("PATCH", {"is_active": False}))
        delete.clicked.connect(lambda: self._mem("DELETE"))
        ok.clicked.connect(lambda: self._pend(True))
        no.clicked.connect(lambda: self._pend(False))
        self.reload()

    def reload(self) -> None:
        def fill(items):
            self.list.clear()
            for m in items if isinstance(items, list) else []:
                it = QListWidgetItem(f"[{m['memory_type']}] {m['content']}  ({m['created_at'][:10]})")
                it.setData(Qt.UserRole, m["id"])
                self.list.addItem(it)

        def fill_p(items):
            self.pending.clear()
            for m in items if isinstance(items, list) else []:
                it = QListWidgetItem(f"{m['content']} — {m['reason']}")
                it.setData(Qt.UserRole, m["id"])
                self.pending.addItem(it)
        self.backend.get("/api/memory", fill)
        self.backend.get("/api/memory/pending", fill_p)

    def _mem(self, method: str, body=None) -> None:
        it = self.list.currentItem()
        if not it:
            return
        mid = it.data(Qt.UserRole)
        self.backend._request(method, f"/api/memory/{mid}", body, lambda _: self.reload())

    def _pend(self, approve: bool) -> None:
        it = self.pending.currentItem()
        if it:
            self.backend.post(f"/api/memory/pending/{it.data(Qt.UserRole)}", {"approve": approve},
                              lambda _: self.reload())
