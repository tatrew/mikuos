"""Conservative visual hand IK in Qt scene coordinates; never moves the cursor."""
import math

from PySide6.QtCore import QObject
from PySide6.QtGui import QQuaternion, QVector3D

from .expression import smooth


class CursorInteractionController:
    def __init__(self, asset):
        self.upper = asset.findChild(QObject, 'Right_Arm')
        self.lower = asset.findChild(QObject, 'Right_Elbow')
        self.hand = asset.findChild(QObject, 'Right_Wrist')
        self.weight = 0.0
        self.available = all(x is not None for x in (self.upper, self.lower, self.hand))

    @staticmethod
    def aim(joint, start, old_end, new_end, weight):
        before = joint.property('rotation')
        delta = QQuaternion.rotationTo(old_end - start, new_end - start)
        world_rotation = delta * joint.property('sceneRotation')
        parent_rotation = joint.parent().property('sceneRotation')
        if not isinstance(parent_rotation, QQuaternion):
            return
        local = parent_rotation.conjugated() * world_rotation
        joint.setProperty('rotation', QQuaternion.slerp(before, local, weight))

    def update(self, dt, look, model_px, allowed):
        close = allowed and look is not None and math.hypot(*look) < 70
        self.weight = smooth(self.weight, 1 if close else 0, dt, 6)
        if not self.available or self.weight < .001:
            return
        shoulder = self.upper.property('scenePosition')
        elbow = self.lower.property('scenePosition')
        hand = self.hand.property('scenePosition')
        if not all(isinstance(x, QVector3D) for x in (shoulder, elbow, hand)):
            return
        dx, dy = look if look is not None else (0, 0)
        target = QVector3D(max(-.22, min(.12, dx / model_px)),
                           max(.62, min(.95, .8 - dy / model_px)), shoulder.z() + .16)
        l1, l2 = (elbow - shoulder).length(), (hand - elbow).length()
        if min(l1, l2) < .001:
            return
        direction = (target - shoulder).normalized()
        distance = max(max(l1, l2), min((target - shoulder).length(), (l1 + l2) * .94))
        target = shoulder + direction * distance
        pole = QVector3D(-.3, -.45, .05)
        bend = (pole - direction * QVector3D.dotProduct(pole, direction)).normalized()
        along = (l1*l1 - l2*l2 + distance*distance) / (2 * distance)
        knee = shoulder + direction * along + bend * math.sqrt(max(0, l1*l1 - along*along))
        self.aim(self.upper, shoulder, elbow, knee, self.weight)
        elbow = self.lower.property('scenePosition')
        hand = self.hand.property('scenePosition')
        self.aim(self.lower, elbow, hand, target, self.weight)
