"""Render actual Qt Quick 3D frames for visual QA; exits without a backend."""
import os
import sys
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
pet = Pet3DWindow(260)
pet.show()
out = ROOT / 'data/validation'
out.mkdir(parents=True, exist_ok=True)
states = [State.IDLE, State.WALK, State.SIT, State.SITTING_IDLE, State.STAND_UP,
          State.WAVE, State.THINK, State.SAD, State.JUMP, State.LAND, State.SLEEP, State.RUN]
sheet = QImage(pet.width() * 6, (pet.height() + 25) * 2, QImage.Format_ARGB32)
sheet.fill(QColor('#303640'))
painter = QPainter(sheet)
for i, state in enumerate(states):
    for frame in range(28):
        pet.set_view(state, 0, 1, (100, -20), frame / 30)
        QTest.qWait(33)
    shot = pet.quick.grabFramebuffer()
    shot.save(str(out / (state.value + '.png')))
    x, y = (i % 6) * pet.width(), (i // 6) * (pet.height() + 25)
    painter.drawImage(x, y + 25, shot)
    painter.setPen(QColor('white'))
    painter.drawText(x + 8, y + 18, state.value)
painter.end()
sheet.save(str(out / 'contact-sheet.png'))
pet.close()
print(out / 'contact-sheet.png')
