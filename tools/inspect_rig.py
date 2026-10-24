import bpy
from mathutils import Vector
for name in ("Position", "Position.001"):
    a = bpy.data.objects[name]
    print("==", name, "constraints:", [(c.type, getattr(c, 'target', None) and c.target.name, getattr(c,'subtarget','')) for c in a.constraints])
    chain = []; p = a
    while p: chain.append((p.name, tuple(round(v,3) for v in p.location), tuple(round(v,3) for v in p.rotation_euler), tuple(round(v,3) for v in p.scale))); p = p.parent
    print("   parents:", chain)
    print("   bones:", [b.name for b in a.data.bones][:150])
for name in ("Body", "Face", "Acc"):
    o = bpy.data.objects[name]
    pts = [o.matrix_world @ Vector(c) for c in o.bound_box]
    mn = [round(min(p[i] for p in pts),3) for i in range(3)]; mx = [round(max(p[i] for p in pts),3) for i in range(3)]
    print(name, "world bbox", mn, mx, "vgroups", len(o.vertex_groups))
f = bpy.data.objects["Face"]
print("shapekeys:", [k.name for k in f.data.shape_keys.key_blocks])
for m in ("mtl_bdy_00.003", "mtl_chr_00.003", "mtl_acc_00.003"):
    mat = bpy.data.materials[m]
    print(m, "nodes:", [(n.type, n.name, getattr(n, 'image', None) and n.image.name) for n in mat.node_tree.nodes] if mat.use_nodes else "no nodes")
