"""Blender (background) script: Project SEKAI Miku scene -> clean, animated glTF.

Usage:
  blender -b assets/miku_model/source/GameObject/GameObject.blend --python tools/export_miku.py -- <out.glb> [preview.png]

Steps: keep only Body/Face/Acc meshes and their two armatures, attach the head
armature to the Neck bone, normalise scale/orientation (feet at origin, height
1.0, facing -Y in Blender == +Z toward the camera in glTF), rebuild materials as
unlit base-colour textures, author procedural actions (idle, walk, jump, fall,
sit, sleep, happy, drag, chase, look) and export them as glTF animations.
"""
import math
import sys

import bpy
from mathutils import Euler, Matrix, Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else "miku.glb"
PREVIEW = argv[1] if len(argv) > 1 else None

KEEP_MESH = ["Body", "Face", "Acc"]
BODY_ARM, HEAD_ARM = "Position", "Position.001"

scene = bpy.context.scene
body = bpy.data.objects[BODY_ARM]
head = bpy.data.objects[HEAD_ARM]
meshes = [bpy.data.objects[n] for n in KEEP_MESH]

# ---------------------------------------------------------------- cleanup
keep = set(meshes) | {body, head}
world = {o.name: o.matrix_world.copy() for o in keep}
for o in keep:  # unparent, keep world transforms
    o.parent = None
    o.matrix_world = world[o.name]
for o in list(bpy.data.objects):
    if o not in keep:
        bpy.data.objects.remove(o, do_unlink=True)
for coll in list(bpy.data.collections):
    if not coll.objects and not coll.children:
        bpy.data.collections.remove(coll)
for a in list(bpy.data.actions):
    bpy.data.actions.remove(a)
for o in keep:
    if o.name not in scene.collection.objects:
        try:
            scene.collection.objects.link(o)
        except RuntimeError:
            pass

# meshes deform with their armature: make the armature their parent
for m in meshes:
    arm = next((md.object for md in m.modifiers if md.type == "ARMATURE"), None)
    if m.name == "Acc":
        # accessory has no vertex groups: rigidly follow the head bone
        arm = head
    mw = m.matrix_world.copy()
    m.parent = arm
    if m.name == "Acc":
        for md in list(m.modifiers):
            m.modifiers.remove(md)
        m.parent = head
        m.parent_type = "BONE"
        m.parent_bone = "Head"
    m.matrix_world = mw

# head armature follows the Neck bone of the body armature
hw = head.matrix_world.copy()
head.parent = body
head.parent_type = "BONE"
head.parent_bone = "Neck"
head.matrix_world = hw

# ---------------------------------------------------- normalise transform
bpy.context.view_layer.update()
pts = []
for m in meshes:
    pts += [m.matrix_world @ Vector(c) for c in m.bound_box]
zmin = min(p.z for p in pts)
zmax = max(p.z for p in pts)
cx = (min(p.x for p in pts) + max(p.x for p in pts)) / 2
cy = (min(p.y for p in pts) + max(p.y for p in pts)) / 2
s = 1.0 / (zmax - zmin)
body.matrix_world = Matrix.Translation((-cx * s, -cy * s, -zmin * s)) @ Matrix.Scale(s, 4) @ body.matrix_world
bpy.context.view_layer.update()

# which way does she face? Face mesh is in front of the body centre on Y
face_y = sum((bpy.data.objects["Face"].matrix_world @ v.co).y for v in list(bpy.data.objects["Face"].data.vertices)[::50])
print("face mean y sign:", math.copysign(1, face_y))

# ---------------------------------------------------------------- materials
TEX = {"Body": "tex_bdy_MIK_all_C.png", "Face": "tex_chr_MIK_all_C.png", "Acc": "tex_acc_MIK_all_C.png"}
for m in meshes:
    img = next((i for i in bpy.data.images if i.filepath.endswith(TEX[m.name]) and i.size[0] > 0), None)
    mat = bpy.data.materials.new(f"miku_{m.name.lower()}")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Roughness"].default_value = 1.0
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    mat.blend_method = "CLIP" if hasattr(mat, "blend_method") else None
    m.data.materials.clear()
    m.data.materials.append(mat)
    if img:
        img.pack()

# ---------------------------------------------------------------- animation
FPS = 30
scene.render.fps = FPS
pb = body.pose.bones
hb = head.pose.bones


from mathutils import Quaternion

# World axes (Blender, after normalisation): she faces -Y, +X is HER left, +Z up.
X, Y, Z = Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))


def wrot(*pairs):
    """Compose world-axis rotations, applied left to right: wrot((Y, 70), (X, -20))."""
    q = Quaternion()
    for axis, deg in pairs:
        q = Quaternion(axis, math.radians(deg)) @ q
    return q


def setw(bone, q):
    """Set a pose-bone rotation given in world axes relative to the rest pose."""
    arm = bone.id_data
    m = (arm.matrix_world.to_3x3() @ bone.bone.matrix_local.to_3x3()).normalized().to_quaternion()
    bone.rotation_mode = "QUATERNION"
    bone.rotation_quaternion = m.inverted() @ q @ m


def reset():
    for arm in (body, head):
        for b in arm.pose.bones:
            b.rotation_mode = "QUATERNION"
            b.rotation_quaternion = (1, 0, 0, 0)
            b.location = (0, 0, 0)
            b.scale = (1, 1, 1)


def key_all(frame):
    for arm in (body, head):
        for b in arm.pose.bones:
            b.keyframe_insert("rotation_quaternion", frame=frame)
            b.keyframe_insert("location", frame=frame)


ARM_DOWN = 72.0


def arms(down_l=ARM_DOWN, down_r=ARM_DOWN, swing_l=0.0, swing_r=0.0):
    """down = degrees from T-pose toward the hips; swing > 0 = forward."""
    setw(pb["Left_Arm"], wrot((Y, down_l), (X, -swing_l)))
    setw(pb["Right_Arm"], wrot((Y, -down_r), (X, -swing_r)))
    setw(pb["Left_Elbow"], wrot((X, -8)))
    setw(pb["Right_Elbow"], wrot((X, -8)))


def legs(thigh_l=0.0, thigh_r=0.0, knee_l=0.0, knee_r=0.0):
    """thigh > 0 = forward, knee > 0 = bend (shin backwards)."""
    setw(pb["Left_Thigh"], wrot((X, -thigh_l)))
    setw(pb["Right_Thigh"], wrot((X, -thigh_r)))
    setw(pb["Left_Knee"], wrot((X, knee_l)))
    setw(pb["Right_Knee"], wrot((X, knee_r)))


def solve_limb(upper_name, lower_name, end_name, target, pole, keep_end=True):
    """Bake a two-joint IK solution into rotations, using joint origins rather
    than imported bone lengths (this rig's bone tails do not follow the limbs).
    No constraints or runtime Blender dependency are exported.
    """
    upper, lower, end = (pb[n] for n in (upper_name, lower_name, end_name))
    bpy.context.view_layer.update()
    world = body.matrix_world
    rest = [world @ b.bone.matrix_local for b in (upper, lower, end)]
    points = [m.translation for m in rest]
    origin = (world @ upper.matrix).translation
    l1, l2 = (points[1] - points[0]).length, (points[2] - points[1]).length
    direction = target - origin
    distance = min(l1 + l2 - .0001, max(abs(l1 - l2) + .0001, direction.length))
    direction.normalize()
    bend = pole - origin
    bend -= direction * bend.dot(direction)
    bend.normalize()
    along = (l1*l1 - l2*l2 + distance*distance) / (2 * distance)
    knee = origin + direction * along + bend * math.sqrt(max(0, l1*l1 - along*along))
    target = origin + direction * distance
    for index, (bone, start, finish) in enumerate(((upper, origin, knee), (lower, knee, target))):
        delta = (points[index + 1] - points[index]).rotation_difference(finish - start)
        matrix = delta.to_matrix().to_4x4() @ rest[index]
        matrix.translation = start
        bone.matrix = world.inverted() @ matrix
        bpy.context.view_layer.update()
    if keep_end:
        matrix = rest[2].copy()
        matrix.translation = target
        end.matrix = world.inverted() @ matrix


REST_FEET = {side: (body.matrix_world @ pb[side + '_Ankle'].bone.matrix_local).translation.copy()
             for side in ('Left', 'Right')}


def make_action(name, frames, fn):
    act_b = bpy.data.actions.new(name)
    act_h = bpy.data.actions.new(f"{name}_head")
    body.animation_data_create().action = act_b
    head.animation_data_create().action = act_h
    for f in frames:
        reset()
        fn(f / frames[-1] if frames[-1] else 0.0)
        key_all(f)
    for arm, act in ((body, act_b), (head, act_h)):
        tr = arm.animation_data.nla_tracks.new()
        tr.name = name
        tr.strips.new(name, 0, act)
        arm.animation_data.action = None


def build_actions():
    tau = 2 * math.pi

    def idle(t):
        br = math.sin(t * tau)
        arms(ARM_DOWN + br, ARM_DOWN + br)
        setw(pb["Chest"], wrot((X, br * 1.2)))
        setw(pb["Neck"], wrot((Y, br * 1.5)))

    def walk(t):
        ph = math.sin(t * tau)
        arms(swing_l=-12 * ph, swing_r=12 * ph)
        bpy.context.view_layer.update()
        matrix = body.matrix_world @ pb['Hip'].matrix
        matrix.translation.z -= .014
        pb['Hip'].matrix = body.matrix_world.inverted() @ matrix
        for side, phase in (('Left', t % 1), ('Right', (t + .5) % 1)):
            target = REST_FEET[side].copy()
            if phase < .5:  # planted foot travels backwards at constant speed
                target.y += -.1 + .4 * phase
            else:
                u = (phase - .5) * 2
                # Match the backwards velocity at both contact boundaries.
                # Smoothstep here caused a sudden velocity reversal/toe snap.
                target.y += .1 + .2 * u - 1.2 * u*u + .8 * u*u*u
                target.z += .035 * math.sin(math.pi * u) ** 2
            pole = target + Vector((0, -1, .3))
            solve_limb(side + '_Thigh', side + '_Knee', side + '_Ankle', target, pole)

    def jump(t):
        arms(38, 38, 22, 22)
        legs(18, 18, 28, 28)

    def fall(t):
        w = math.sin(t * tau) * 3
        arms(28 + w, 28 - w, 12, 12)
        legs(8, 5, 12, 10)

    def sit(t):
        sw = math.sin(t * tau) * 5
        arms(80, 80, swing_l=15, swing_r=15)
        legs(88, 88, 85 + sw, 85 - sw)

    def sleep(t):
        sit(0.0)
        setw(pb["Neck"], wrot((X, 20 + 2 * math.sin(t * tau)), (Y, 8)))
        setw(pb["Chest"], wrot((X, 6)))

    def happy(t):
        arms()
        # A bent-elbow wave beside the head, not an almost vertical arm
        # driven through 165 degrees at the shoulder.
        ph = math.sin(t * 2 * tau)
        target = Vector((-.22 + .018 * ph, -.11, .86))
        solve_limb('Right_Arm', 'Right_Elbow', 'Right_Wrist', target,
                   Vector((-.42, -.08, .60)), keep_end=False)
        setw(pb['Neck'], wrot((Y, 2 * math.sin(t * tau))))

    def drag(t):
        sw = math.sin(t * tau) * 6
        arms(30, 30, 15, 15)
        legs(sw, -sw, 12, 12)

    def chase(t):
        walk(t)
        # Keep the locomotion arm swing. The separate idle reach layer owns IK.

    def look(t):
        arms()
        setw(pb["Neck"], wrot((X, -5)))

    make_action("idle", list(range(0, 91, 5)), idle)
    make_action("walk", list(range(0, 25, 2)), walk)
    make_action("jump", [0, 10], jump)
    make_action("fall", list(range(0, 21, 5)), fall)
    def sit_down(t):
        k = t * t * (3 - 2 * t)
        arms(ARM_DOWN + 8 * k, ARM_DOWN + 8 * k, 15 * k, 15 * k)
        legs(88 * k, 88 * k, 85 * k, 85 * k)

    def land(t):
        k = math.sin(math.pi * t)
        arms(72 - 12 * k, 72 - 12 * k)
        legs(18 * k, 18 * k, 35 * k, 35 * k)

    def run(t):
        walk(t)
        ph = math.sin(t * tau)
        arms(swing_l=-20 * ph, swing_r=20 * ph)
        setw(pb['Chest'], wrot((X, 3)))

    def think(t):
        idle(t)
        target = (body.matrix_world @ pb['Neck'].bone.matrix_local).translation + Vector((-.075, -.08, -.025))
        solve_limb('Right_Arm', 'Right_Elbow', 'Right_Wrist', target,
                   target + Vector((-.3, 0, -.5)), keep_end=False)
        setw(pb['Neck'], wrot((Y, 7), (X, 5)))

    def sad(t):
        idle(t)
        setw(pb['Neck'], wrot((X, 14)))
        setw(pb['Chest'], wrot((X, 5)))

    def surprised(t):
        arms(45, 45, 25, 25)
        setw(pb['Neck'], wrot((X, -8)))

    def around(t):
        idle(t)
        setw(pb['Neck'], wrot((Z, 18 * math.sin(t * tau))))

    make_action("sit", list(range(0, 19, 2)), sit_down)
    make_action("sitting_idle", list(range(0, 61, 5)), sit)
    make_action("stand_up", list(range(0, 19, 2)), lambda t: sit_down(1 - t))
    make_action("land", list(range(0, 13, 2)), land)
    make_action("run", list(range(0, 19, 2)), run)
    make_action("wave", list(range(0, 61, 3)), happy)
    make_action("think", list(range(0, 91, 5)), think)
    make_action("sad", list(range(0, 61, 5)), sad)
    make_action("surprised", [0, 15, 30], surprised)
    make_action("look_around", list(range(0, 91, 5)), around)
    make_action("sleep", list(range(0, 91, 10)), sleep)
    make_action("happy", list(range(0, 31, 3)), happy)
    make_action("drag", list(range(0, 31, 5)), drag)
    make_action("chase", list(range(0, 19, 2)), chase)
    make_action("look", [0, 30], look)


if "--calibrate" not in sys.argv:
    build_actions()

# ------------------------------------------------------------------ preview
if PREVIEW:
    reset()
    if "--pose" in sys.argv:
        name = sys.argv[sys.argv.index("--pose") + 1]
        for arm in (body, head):
            ad = arm.animation_data
            if ad:
                for tr in ad.nla_tracks:
                    tr.mute = tr.name != name
        scene.frame_set(int(sys.argv[sys.argv.index("--frame") + 1]) if "--frame" in sys.argv else 0)
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = 1.2
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    yaw = float(sys.argv[sys.argv.index("--yaw") + 1]) if "--yaw" in sys.argv else 0.0
    d = 3.0
    cam.location = (d * math.sin(math.radians(yaw)), -d * math.cos(math.radians(yaw)), 0.5)
    cam.rotation_euler = (math.radians(90), 0, math.radians(yaw))
    scene.camera = cam
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.rotation_euler = (math.radians(50), 0, math.radians(20))
    scene.collection.objects.link(sun)
    scene.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items] else "BLENDER_WORKBENCH"
    if scene.render.engine == "BLENDER_WORKBENCH":
        scene.display.shading.color_type = "TEXTURE"
        scene.display.shading.light = "FLAT"
    scene.render.film_transparent = True
    scene.render.resolution_x, scene.render.resolution_y = 400, 480
    scene.render.filepath = PREVIEW
    bpy.ops.render.render(write_still=True)
    print("preview written", PREVIEW)

if "--calibrate" not in sys.argv:
    for arm in (body, head):
        if arm.animation_data:
            arm.animation_data.action = None
    reset()
    bpy.ops.export_scene.gltf(
        filepath=OUT, export_format="GLB", use_selection=False, export_animations=True,
        export_animation_mode="NLA_TRACKS", export_morph=True, export_skins=True, export_yup=True,
        export_apply=False, export_force_sampling=True, export_optimize_animation_size=False,
    )
    print("exported", OUT)
