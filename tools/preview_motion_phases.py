"""Deterministic phase samples from the real Qt renderer, no backend required."""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_ENABLE_HIGHDPI_SCALING'] = '0'
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtGui import QImage, QPainter, QColor
from mikuos_client.animation import State
from mikuos_client.pet3d_window import Pet3DWindow

app = QApplication([])
pet = Pet3DWindow(300)
pet.show()
states = [State.WALK, State.RUN, State.WAVE, State.THINK, State.SITTING_IDLE, State.JUMP]
phases = [0, .2, .4, .6, .8]
sheet = QImage(pet.width() * len(phases), (pet.height() + 25) * len(states), QImage.Format_ARGB32)
sheet.fill(QColor('#303640'))
painter = QPainter(sheet)
for row, state in enumerate(states):
    duration = pet.library.clips[state.value][0]
    for column, phase in enumerate(phases):
        for _ in range(12):
            pet.mixer.name = state.value
            pet.mixer.time = max(0, duration * phase - 1 / 30)
            pet.mixer.elapsed = 1
            pet._last = time.monotonic() - 1 / 30
            pet.set_view(state, 0, 1, None, 0)
            QTest.qWait(16)
        x, y = column * pet.width(), row * (pet.height() + 25)
        painter.drawImage(x, y + 25, pet.quick.grabFramebuffer())
        painter.setPen(QColor('white'))
        painter.drawText(x + 6, y + 18, f'{state.value}  {phase:.1f}')
painter.end()
out = ROOT / 'data/validation/motion-phases.png'
sheet.save(str(out))
pet.close()
print(out)
