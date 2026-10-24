"""Overlay: avatar physics/surfaces (pure), riding a real moving window, and
Test G — the real Qt client keeps animating and shows connection state while the
backend is up without LM Studio, and while the backend is down entirely."""
import random
import subprocess
import sys
import threading
import time

import pytest

from mikuos_client.animation import AnimationStateMachine, State
from mikuos_client.avatar_controller import AvatarController
from mikuos_client.surface_detector import Box, Surface, SurfaceDetector


def test_state_machine_rules():
    sm = AnimationStateMachine(State.IDLE)
    assert sm.set(State.WALK)
    assert not sm.set(State.SLEEP)  # walk -> sleep is not a legal transition
    assert sm.set(State.SLEEP, force=True)
    sm.set(State.HAPPY, force=True)
    sm.update(2.0)
    assert sm.state == State.IDLE  # timed state ends on its own
    sm.set(State.WALK)
    sm.update(0.26)
    assert sm.frame == 2


def test_surface_detection_with_occlusion():
    wa = [Box(0, 0, 1920, 1040), Box(-1280, 0, 1280, 984)]  # second monitor left of primary (negative x)
    front = (1, Box(100, 300, 400, 300))
    back = (2, Box(300, 300, 600, 400))  # same top edge height, partly covered by `front`
    hidden = (3, Box(150, 350, 200, 100))  # top edge entirely inside `front`
    neg = (4, Box(-1000, 200, 500, 400))
    surfaces = SurfaceDetector().detect(wa, [front, back, hidden, neg])
    by_id = {}
    for s in surfaces:
        by_id.setdefault(s.window_id, []).append(s)
    assert {(s.y, s.x1, s.x2) for s in by_id[None]} == {(1040, 0, 1920), (984, -1280, 0)}
    assert by_id[1] == [Surface(300, 100, 500, 1)]
    assert by_id[2] == [Surface(300, 500, 900, 2)]  # covered part cut away
    assert 3 not in by_id
    assert by_id[4][0].x1 == -1000  # negative coordinates supported


def test_avatar_falls_lands_walks_and_rides_window():
    floor = Surface(1000, 0, 1920)
    win = Surface(500, 200, 800, window_id=7)
    a = AvatarController(120, 150, rng=random.Random(1))
    a.set_surfaces([floor, win])
    a.place(400, 100)
    for _ in range(100):
        a.update(0.033)
    assert a.support == win and a.foot == 500 and a.anim.state != State.FALL
    # the user drags the window: Miku moves with it
    moved = Surface(450, 260, 860, window_id=7)
    a.set_surfaces([floor, moved], {7: (60, -50)})
    assert a.foot == 450 and a.support.window_id == 7 and abs(a.x - 460) < 1e-6
    # the window disappears: she falls to the floor
    a.set_surfaces([floor])
    assert a.anim.state == State.FALL
    for _ in range(100):
        a.update(0.033)
    assert a.support == floor and a.foot == 1000
    # walking stays on the surface
    for _ in range(3000):
        a.update(0.033)
        assert floor.x1 <= a.x <= floor.x2
        if a.support is not None:
            assert a.foot in (1000, 450, 500)


def test_avatar_cursor_reactions_and_sleep():
    floor = Surface(1000, 0, 1920)
    a = AvatarController(120, 150, rng=random.Random(2))
    a.set_surfaces([floor])
    a.place(500, 999)
    a.update(0.05)
    assert a.anim.state == State.LAND
    for _ in range(9):
        a.update(0.05)
    a.update(0.05, cursor=(700, 880))
    assert a.anim.state == State.LOOK and a.facing == 1
    a.update(0.05, cursor=(560, 880))
    assert a.anim.state == State.CHASE
    x0 = a.x
    a.update(0.05, cursor=(580, 880))
    assert a.x > x0  # moves toward the cursor (animation only)
    a.anim.set(State.IDLE, force=True)
    a.update(0.05, cursor=None, user_idle_s=10_000)
    assert a.anim.state == State.SLEEP
    a.poke()
    assert a.anim.state == State.STAND_UP
    a.paused = True
    a.anim.set(State.IDLE, force=True)
    a.update(0.05, cursor=(560, 880))
    assert a.anim.state == State.IDLE  # emergency stop: no reaction


def test_avatar_respawns_if_lost_below_screen():
    floor = Surface(1000, 0, 1920)
    a = AvatarController(120, 150)
    a.set_surfaces([floor])
    a.place(-5000, 3000)  # dragged far off-screen
    for _ in range(200):
        a.update(0.033)
    assert a.support == floor


@pytest.mark.desktop
def test_tracker_reports_real_window_moves():
    from mikuos.windows import WindowsBridge
    from mikuos_client.window_tracker import WindowTracker
    b = WindowsBridge()
    title = "MikuOS-tracker-test"
    proc = subprocess.Popen([sys.executable, "-c", "import tkinter as t; r=t.Tk(); r.title('%s'); "
                             "r.geometry('500x300+400+400'); r.attributes('-topmost', True); "
                             "r.after(30000, r.destroy); r.mainloop()" % title])
    try:
        wid = None
        for _ in range(60):
            time.sleep(0.1)
            ws = [w for w in b.list_windows() if w.title == title]
            if ws:
                wid = ws[0].id
                break
        assert wid
        tr = WindowTracker(b)
        surfaces, _ = tr.poll()
        top = [s for s in surfaces if s.window_id == wid]
        assert top, "window top edge must be a surface"
        g0 = b.get_window_geometry(wid)
        b.move_window(wid, 520, 380)
        surfaces, moves = tr.poll()
        assert moves.get(wid) == (520 - g0["x"], 380 - g0["y"])
        assert [s.y for s in surfaces if s.window_id == wid] == [380]
        a = AvatarController(100, 120)
        a.set_surfaces(surfaces)
        a.place(700, 300)
        for _ in range(60):
            a.update(0.033)
            if a.support:
                break  # check the first landing (later the avatar may jump elsewhere at random)
        assert a.support and a.support.window_id == wid
        a.decision_timer = 100
        a.command({'action': 'sit_on_window', 'target_window_id': wid})
        for _ in range(45):
            a.update(.033)
        assert a.anim.state == State.SITTING_IDLE
        before_x = a.x
        b.move_window(wid, 550, 400)
        surfaces, moves = tr.poll()
        a.set_surfaces(surfaces, moves)
        assert a.x == pytest.approx(before_x + 30)
        assert a.foot == 400 and a.anim.state == State.SITTING_IDLE
        proc.terminate()
        proc.wait(5)
        surfaces, moves = tr.poll()
        a.set_surfaces(surfaces, moves)
        assert a.anim.state == State.FALL and a.support is None
    finally:
        if proc.poll() is None:
            proc.kill()


# ------------------------------------------------------------------ Test G
@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _serve(settings, port):
    import uvicorn

    from mikuos.server import create_app
    cfg = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    return server, t


@pytest.mark.desktop
def test_G_client_survives_lmstudio_and_backend_outage(qapp, tmp_settings, dead_url):
    from PySide6.QtTest import QTest

    from conftest import free_port
    from mikuos_client.app import OverlayApp
    from mikuos_client.backend_client import ConnState

    port = free_port()
    tmp_settings.update({"server": {"port": port}, "hotkey": {"emergency_stop": "ctrl+alt+shift+f11",
                                                              "fallbacks": []},
                         "lmstudio": {"base_url": dead_url, "max_retries": 0, "reconnect_interval_s": 1}})
    # 1) backend down entirely: overlay still animates and shows "backend offline"
    overlay = OverlayApp(qapp, tmp_settings)
    overlay.ctrl.cfg.sleep_after_idle_s = float('inf')  # measure normal, not 10 FPS sleep mode
    overlay.backend._timer.setInterval(300)
    overlay.start()
    try:
        QTest.qWait(1200)
        f0 = overlay.frames
        QTest.qWait(1000)
        assert overlay.backend.state == ConnState.BACKEND_OFFLINE == overlay.pet.conn
        assert overlay.frames - f0 > 15, "animation loop must keep running"
        assert overlay.pet.isVisible() or overlay.hidden_for_fullscreen

        # 2) backend comes up, but LM Studio is unreachable: yellow state
        server, thread = _serve(tmp_settings, port)
        for _ in range(40):
            QTest.qWait(250)
            if overlay.backend.state != ConnState.BACKEND_OFFLINE:
                break
        assert overlay.backend.state == ConnState.LLM_DOWN == overlay.pet.conn
        assert overlay.backend.status["llm"]["connected"] is False
        f1 = overlay.frames
        QTest.qWait(800)
        assert overlay.frames - f1 > 10
        # chat is refused cleanly instead of hanging
        assert not overlay.chat.send_btn.isEnabled()

        # 3) backend stops: overlay detects it and keeps animating
        server.should_exit = True
        thread.join(10)
        for _ in range(40):
            QTest.qWait(250)
            if overlay.backend.state == ConnState.BACKEND_OFFLINE:
                break
        assert overlay.backend.state == ConnState.BACKEND_OFFLINE
        f2 = overlay.frames
        QTest.qWait(600)
        assert overlay.frames - f2 > 8
    finally:
        overlay.timer.stop()
        overlay.backend.stop()
        overlay.pet.close()
        overlay.tray.hide()
        if overlay.hotkey:
            overlay.hotkey.stop()


@pytest.mark.desktop
def test_3d_avatar_loads_switches_animations_and_masks(qapp):
    from PySide6.QtTest import QTest

    from mikuos_client.pet3d_window import ANIM, Pet3DWindow, model_available
    if not model_available():
        pytest.skip("3D model not built (tools/build_miku3d.py)")
    w = Pet3DWindow(200)
    w.move(50, 50)
    w.show()
    try:
        sizes = {}
        for st in (State.IDLE, State.WALK, State.SIT, State.SLEEP, State.HAPPY):
            for _ in range(10):
                w.set_view(st, 0, 1, None, 0)
                QTest.qWait(30)
            assert w.root.property("animState") == ANIM[st]
            if st == State.SLEEP:
                assert w.root.property("eyeClose") == 1.0  # asleep: eyes closed via morph target
            img = w.quick.grabFramebuffer()
            opaque = sum(1 for y in range(0, img.height(), 4) for x in range(0, img.width(), 4)
                         if img.pixelColor(x, y).alpha() > 0)
            sizes[st] = opaque
            assert opaque > 150, f"nothing rendered for {st}"
        QTest.qWait(300)
        mask = w.mask()
        assert not mask.isEmpty()
        assert mask.boundingRect().width() < w.width() + 1
        # most of the window must stay click-through (Miku is slim)
        area = sum(r.width() * r.height() for r in mask)
        assert area < 0.6 * w.width() * w.height()
        for _ in range(30):
            w.set_view(State.LOOK, 0, 1, (-35, 0), 0)
            QTest.qWait(33)
        assert w.cursor_controller.available and w.cursor_controller.weight > .95
        assert w.cursor_controller.hand.property('scenePosition').length() < 2
    finally:
        w.close()


@pytest.mark.desktop
def test_sprite_avatar_renders_every_state_mirrors_and_masks(qapp):
    from PySide6.QtTest import QTest

    from mikuos_client.app import OverlayApp
    from mikuos_client.sprite_renderer import ANIMS, available
    if not available():
        pytest.skip("sprites not in assets/sprites")
    w = OverlayApp._make_pet("auto", 1.0, 220, 2)
    assert type(w.renderer).__name__ == "SpriteRenderer"  # auto prefers the 2D sprites
    r = w.renderer
    # every state has frames; one-shot clips hold their last frame
    for st in State:
        assert st in ANIMS, f"no sprite animation for {st}"
    jump_end, _ = r.pick(State.JUMP, 99)
    assert jump_end == ANIMS[State.JUMP].frames[-1]
    # idle is the GIF loop (frames change over time)
    assert len({r.pick(State.IDLE, t / 10)[0] for t in range(20)}) > 5
    from PySide6.QtGui import QImage, QPainter

    def shot(state, facing, t=0.0):
        img = QImage(*r.size(), QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        r.paint(p, state, 0, facing, None, t)
        p.end()
        return img

    def columns(img):  # opaque pixel count per column
        return [sum(1 for y in range(img.height()) if img.pixelColor(x, y).alpha()) for x in range(img.width())]

    # walking left is the mirror image of walking right (up to 1 px of anchor rounding)
    right, left = columns(shot(State.WALK, 1)), columns(shot(State.WALK, -1))
    assert right != left
    assert min(sum(abs(a - b) for a, b in zip(right[::-1][d:], left)) for d in (0, 1, 2)) < 0.05 * sum(right)
    # the sheet background is keyed out and feet stand on the bottom row
    for st in (State.WALK, State.SLEEP, State.DRAG, State.HAPPY):
        img = shot(st, 1)
        assert img.pixelColor(2, 2).alpha() == 0
        assert any(img.pixelColor(x, img.height() - 1).alpha() for x in range(img.width())), st
    # talking switches to the mouth frames
    w.talk(1.0)
    assert r.pick(State.IDLE, 0)[0] in (53, 61)
    r.talk_until = 0
    w.move(50, 50)
    w.show()
    try:
        w.set_view(State.IDLE, 0, 1, None, 0)
        QTest.qWait(100)
        mask = w.mask()
        assert not mask.isEmpty()
        area = sum(rr.width() * rr.height() for rr in mask)
        assert area < 0.6 * w.width() * w.height()  # clicks pass through around her
        before = mask.boundingRect()
        w.set_view(State.SLEEP, 0, 1, None, 0)
        QTest.qWait(100)
        assert w.mask().boundingRect() != before  # mask follows the silhouette
    finally:
        w.close()


def _grounded(x=900.0):
    floor = Surface(1000, 0, 1920)
    a = AvatarController(120, 150, rng=random.Random(3))
    a.set_surfaces([floor])
    a.place(x, 990)
    for _ in range(60):
        a.update(0.033)
    assert a.support == floor
    return a


def test_cursor_prank_runs_away_and_ends_by_itself():
    a = _grounded(1500)
    assert a.start_prank()
    assert a.pranking and a.anim.state == State.RUN and a.facing == -1  # more room on the left
    hx, hy = a.hand
    assert hx < a.x and 1000 - 150 < hy < 1000  # held in front of her at chest height
    start = a.x
    for _ in range(int(a.cfg.prank_duration_s / 0.033) + 5):
        a.update(0.033, cursor=a.hand)
    assert not a.pranking and a.prank_result == "done" and a.anim.state == State.HAPPY
    assert start - a.x > a.w  # actually ran off with it
    assert 0 + a.w / 2 <= a.x  # stayed on the surface


def test_cursor_prank_escape_and_interruptions():
    a = _grounded()
    assert a.start_prank() and not a.start_prank()  # one at a time
    a.end_prank("escaped")
    assert a.prank_result == "escaped" and a.anim.state == State.SAD
    a.anim.set(State.IDLE, force=True)
    assert a.start_prank()
    a.start_drag()  # the user grabs Miku herself
    assert not a.pranking and a.prank_result == "interrupted"
    a.end_drag()
    a.set_surfaces([Surface(1000, 0, 200)])  # too narrow to run anywhere
    for _ in range(60):
        a.update(0.033)
    assert not a.start_prank()
    a.paused = True  # emergency stop
    assert not a.start_prank()


@pytest.mark.desktop
def test_overlay_cursor_prank_needs_consent_and_lets_go(qapp, tmp_settings):
    """The real OverlayApp logic, with the cursor-moving call recorded instead of executed."""
    from mikuos_client.app import OverlayApp
    from mikuos_client.backend_client import ConnState
    from mikuos.windows import WindowsBridge

    class RecBridge(WindowsBridge):
        def __init__(self):
            super().__init__()
            self.moves, self.button = [], False

        def set_cursor_position(self, x, y):
            self.moves.append((x, y))
            return {"x": x, "y": y}

        def mouse_button_down(self):
            return self.button

    tmp_settings.update({"hotkey": {"emergency_stop": "ctrl+alt+shift+f10", "fallbacks": []},
                         "ui": {"cursor_prank_chance": 1.0}})
    br = RecBridge()
    ov = OverlayApp(qapp, tmp_settings, bridge=br)
    try:
        c = ov.ctrl
        c.set_surfaces([Surface(1000, 0, 1920)])
        c.place(900, 990)
        for _ in range(60):
            c.update(0.033)
        ov.backend.state = ConnState.ONLINE

        def catch():
            c.anim.set(State.CHASE, force=True)
            ov._cursor_prank(c.hand)

        # no consent (default cursor_control=deny): she never grabs it
        ov.backend.status["cursor_control"] = "deny"
        catch()
        assert not c.pranking and br.moves == []
        # consent given, but a mouse button is held: still hands off
        ov.backend.status["cursor_control"] = "allow"
        br.button = True
        ov._last_prank = -1e9
        catch()
        assert not c.pranking and br.moves == []
        # consent and free hands: grab, carry, cursor follows her hand
        br.button = False
        ov._last_prank = -1e9
        catch()
        assert c.pranking
        for _ in range(5):
            c.update(0.033)
            ov._cursor_prank(br.moves[-1] if br.moves else c.hand)
        assert len(br.moves) >= 4 and br.moves[-1] != br.moves[0]
        # the user yanks the mouse away: released immediately, no more moves
        n = len(br.moves)
        yank = (br.moves[-1][0] + 200, br.moves[-1][1] - 150)
        ov._cursor_prank(yank)
        assert not c.pranking and c.prank_result == "escaped" and len(br.moves) == n
        # cooldown: catching again right away does nothing
        catch()
        assert not c.pranking
        # emergency stop in the middle of a prank lets go at once
        ov._last_prank = -1e9
        c.anim.set(State.IDLE, force=True)
        catch()
        assert c.pranking
        ov._apply_estop(True)
        assert not c.pranking and c.prank_result == "interrupted"
    finally:
        ov.pet.close()
        ov.chat.close()
        ov.tray.hide()
