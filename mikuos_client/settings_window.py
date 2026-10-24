"""Settings window: edits config through PATCH /api/settings (no source edits)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout)

from .backend_client import BackendClient, ConnState

PERM_LABELS = {
    "observe_metadata": "Метаданные окон (заголовки, геометрия)",
    "observe_content": "Содержимое окон (скриншот, элементы UI)",
    "app_control": "Открывать приложения и папки",
    "window_control": "Сворачивать/переключать окна",
    "file_read": "Читать файлы в разрешённых папках",
    "file_write": "Создавать/изменять файлы",
    "memory_write": "Сохранять факты по просьбе",
    "ui_action": "Нажимать элементы интерфейса",
}
POLICY = [("allow", "разрешено"), ("ask", "спрашивать"), ("deny", "запрещено")]
MODES = [("chatty", "Болтливая"), ("normal", "Обычная"), ("quiet", "Тихая"), ("dnd", "Не беспокоить")]


class SettingsWindow(QDialog):
    def __init__(self, backend: BackendClient, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle("MikuOS — настройки")
        self.mode = QComboBox()
        for k, v in MODES:
            self.mode.addItem(v, k)
        self.limit = QSpinBox()
        self.limit.setRange(0, 500)
        self.observe = QCheckBox("Наблюдать за активностью (только метаданные)")
        self.titles = QCheckBox("Сохранять заголовки окон в журнал событий")
        self.chase = QCheckBox("Мику пытается поймать курсор (только анимация)")
        self.cursor = QCheckBox("Разрешить Мику реально сдвигать курсор")
        self.fps = QSpinBox()
        self.fps.setRange(10, 60)
        self.fps.setValue(30)
        self.quality = QComboBox()
        self.quality.addItem('Обычное', 'normal')
        self.quality.addItem('Экономное', 'economy')
        self.hair = QCheckBox('Вторичное движение волос и одежды')
        self.perm_boxes: dict[str, QComboBox] = {}
        form = QFormLayout()
        form.addRow("Инициативность", self.mode)
        form.addRow("Лимит реплик в день", self.limit)
        form.addRow(self.observe)
        form.addRow(self.titles)
        form.addRow(self.chase)
        form.addRow(self.cursor)
        form.addRow('Лимит FPS', self.fps)
        form.addRow('Качество 3D', self.quality)
        form.addRow(self.hair)
        for key, label in PERM_LABELS.items():
            cb = QComboBox()
            for k, v in POLICY:
                cb.addItem(v, k)
            self.perm_boxes[key] = cb
            form.addRow(label, cb)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        save, close = QPushButton("Сохранить"), QPushButton("Закрыть")
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(save)
        btns.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.note)
        lay.addLayout(btns)
        save.clicked.connect(self.save)
        close.clicked.connect(self.close)
        self.cursor.toggled.connect(self._confirm_cursor)
        self.load()

    def load(self) -> None:
        if self.backend.state == ConnState.BACKEND_OFFLINE:
            self.note.setText("Backend не запущен — настройки недоступны.")
            return
        self.backend.get("/api/settings", self._fill)

    def _fill(self, s) -> None:
        if not isinstance(s, dict) or "initiative" not in s:
            return
        self.mode.setCurrentIndex(max(0, self.mode.findData(s["initiative"]["mode"])))
        self.limit.setValue(int(s["initiative"]["daily_llm_limit"]))
        self.observe.setChecked(bool(s["observation"]["enabled"]))
        self.titles.setChecked(bool(s["observation"]["store_window_titles"]))
        self.chase.setChecked(bool(s["ui"].get("cursor_chase", True)))
        self.fps.setValue(int(s['ui'].get('fps', 30)))
        self.quality.setCurrentIndex(max(0, self.quality.findData(s['ui'].get('quality', 'normal'))))
        self.hair.setChecked(bool(s['ui'].get('hair_physics', True)))
        self.cursor.blockSignals(True)
        self.cursor.setChecked(s["permissions"].get("cursor_control") == "allow")
        self.cursor.blockSignals(False)
        for k, cb in self.perm_boxes.items():
            cb.setCurrentIndex(max(0, cb.findData(s["permissions"].get(k, "deny"))))

    def _confirm_cursor(self, on: bool) -> None:
        if not on:
            return
        ans = QMessageBox.question(self, "Управление курсором",
                                   "Разрешить Мику иногда слегка сдвигать системный курсор во время игры с ним?\n"
                                   "Сеанс длится не более 2 минут. Для нового сеанса выключите и включите настройку.\n"
                                   "Отключается здесь или аварийной остановкой (сочетание показано в меню).")
        if ans != QMessageBox.Yes:
            self.cursor.setChecked(False)

    def save(self) -> None:
        patch = {
            "initiative": {"mode": self.mode.currentData(), "daily_llm_limit": self.limit.value()},
            "observation": {"enabled": self.observe.isChecked(), "store_window_titles": self.titles.isChecked()},
            "ui": {"cursor_chase": self.chase.isChecked(), 'fps': self.fps.value(),
                   'quality': self.quality.currentData(), 'hair_physics': self.hair.isChecked()},
            "permissions": {**{k: cb.currentData() for k, cb in self.perm_boxes.items()},
                            "cursor_control": "allow" if self.cursor.isChecked() else "deny"},
        }
        self.backend.patch("/api/settings", patch, lambda r: self.note.setText(
            "Сохранено." if isinstance(r, dict) and "initiative" in r else f"Ошибка: {r}"))
