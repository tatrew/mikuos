import math
from pathlib import Path

import pytest

from mikuos_client.animation import AnimationStateMachine, State
from mikuos_client.avatar_controller import AvatarController
from mikuos_client.expression import EmotionController, VoiceAnimationController, HairPhysicsController
from mikuos_client.pose_mixer import ClipLibrary, PoseMixer
from mikuos_client.surface_detector import Box, Surface, SurfaceDetector

ROOT = Path(__file__).resolve().parents[1]


def test_exported_clips_crossfade_continuously():
    lib = ClipLibrary(ROOT / 'assets/miku3d/miku.glb')
    # Constant-pose optimization must not erase their playback duration.
    assert lib.clips['jump'][0] == pytest.approx(1 / 3)
    assert lib.clips['surprised'][0] == pytest.approx(1)
    mixer = PoseMixer(lib)
    for name in ('walk', 'sit', 'sitting_idle', 'stand_up', 'run', 'jump', 'fall', 'land',
                 'wave', 'look_around', 'think', 'sleep', 'surprised', 'happy', 'sad'):
        before = dict(mixer.pose)
        assert mixer.update(name, 0) == before
        for _ in range(30):
            pose = mixer.update(name, 1 / 30)
            for (_, prop), v in pose.items():
                assert all(math.isfinite(x) for x in v)
                if prop == 'rotation':
                    assert sum(x * x for x in v) == pytest.approx(1, abs=1e-5)
    # Interrupted blend must also start at the visible pose.
    mixer.update('walk', .01)
    before = dict(mixer.pose)
    assert mixer.update('wave', 0) == before


def test_emotions_expire_and_reject_nonfinite():
    e = EmotionController()
    assert e.command({'emotion': 'sad', 'intensity': .8, 'duration': 1})
    assert e.update(.1)[2] > 0
    for _ in range(100):
        e.update(.1)
    assert max(e.values) < .001
    assert not e.command({'emotion': 'bone_rotation'})
    e.command({'emotion': 'happy', 'intensity': float('nan')})
    assert all(math.isfinite(v) for v in e.update(.1))


def test_audio_follows_final_pcm_and_stale_packets_cannot_reopen_mouth():
    v = VoiceAnimationController()
    assert v.event({'event': 'start', 'stream_id': 'a'})
    v.feed_pcm([.2, -.2] * 240, 'a')
    assert v.update(.05) > .5
    assert v.event({'event': 'end', 'stream_id': 'a'})
    assert not v.event({'event': 'energy', 'stream_id': 'a', 'rms': 1})
    for _ in range(40):
        v.update(.05)
    assert v.mouth < .001
    v.event({'event': 'start', 'stream_id': 'b'})
    assert not v.event({'event': 'end', 'stream_id': 'a'})
    v.update(3)
    assert not v.active


def test_hair_low_fps_stability_and_disable():
    for dt in (1 / 15, 1 / 30, 1 / 60):
        h = HairPhysicsController()
        for _ in range(100):
            assert abs(h.update(dt, 10000, 10000)) <= 10
        h.enabled = False
        for _ in range(500):
            h.update(dt, 10000, 10000)
        assert abs(h.angle) < .001


def test_seated_transitions_and_airborne_command_priority():
    a = AvatarController(100, 150)
    floor = Surface(1000, 0, 1920)
    a.set_surfaces([floor])
    a.place(500, 500)
    a.command({'action': 'wave'})
    a.update(.03)
    assert a.anim.state == State.FALL
    for _ in range(100):
        a.update(.03)
        if a.anim.state == State.WAVE:
            break
    assert a.anim.state == State.WAVE and a.support == floor
    sm = AnimationStateMachine(State.SIT)
    sm.update(.61)
    assert sm.state == State.SITTING_IDLE
    assert not sm.set(State.WALK)
    assert sm.set(State.STAND_UP)
    sm.update(.61)
    assert sm.state == State.IDLE


def test_window_spanning_monitors_and_maximized_occlusion():
    detector = SurfaceDetector()
    areas = [Box(-1000, 0, 1000, 900), Box(0, 0, 1000, 1000)]
    surfaces = detector.detect(areas, [(1, Box(-300, 200, 600, 400))])
    assert [s for s in surfaces if s.window_id == 1] == [Surface(200, -300, 0, 1), Surface(200, 0, 300, 1)]
    surfaces = detector.detect(areas, [(2, Box(0, 0, 1000, 1000)), (1, Box(100, 200, 400, 300))])
    assert not [s for s in surfaces if s.window_id is not None]


def test_baked_walk_stance_foot_height_and_linear_speed():
    from PySide6.QtGui import QMatrix4x4, QQuaternion, QVector3D
    lib = ClipLibrary(ROOT / 'assets/miku3d/miku.glb')
    nodes = lib.document['nodes']
    parents = {child: i for i, n in enumerate(nodes) for child in n.get('children', [])}
    ankle = next(i for i, n in enumerate(nodes) if n.get('name') == 'Left_Ankle')

    def position(t):
        pose = lib.sample('walk', t)
        chain, node = [], ankle
        while node is not None:
            chain.append(node)
            node = parents.get(node)
        world = QMatrix4x4()
        for i in reversed(chain):
            n = nodes[i]
            tr = pose.get((i, 'translation'), n.get('translation', (0, 0, 0)))
            q = pose.get((i, 'rotation'), n.get('rotation', (0, 0, 0, 1)))
            sc = pose.get((i, 'scale'), n.get('scale', (1, 1, 1)))
            local = QMatrix4x4()
            local.translate(QVector3D(*tr))
            local.rotate(QQuaternion(q[3], q[0], q[1], q[2]))
            local.scale(QVector3D(*sc))
            world *= local
        return world.map(QVector3D())

    points = [position(t) for t in (0, .1, .2, .3)]
    assert max(p.y() for p in points) - min(p.y() for p in points) < .003
    steps = [points[i + 1].z() - points[i].z() for i in range(3)]
    assert all(abs(abs(step) - .05) < .004 for step in steps)
