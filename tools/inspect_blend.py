import bpy
print("=== OBJECTS ===")
for o in bpy.data.objects:
    dims = tuple(round(d, 3) for d in o.dimensions)
    extra = ""
    if o.type == "MESH":
        extra = f" verts={len(o.data.vertices)} mats={[m.name for m in o.data.materials if m]}"
        mods = [m.type for m in o.modifiers]
        if mods: extra += f" mods={mods}"
        if o.data.shape_keys: extra += f" shapekeys={len(o.data.shape_keys.key_blocks)}"
    if o.type == "ARMATURE":
        extra = f" bones={len(o.data.bones)}"
    print(f"{o.name!r} type={o.type} parent={o.parent.name if o.parent else None} dims={dims} loc={tuple(round(v,3) for v in o.location)}{extra}")
print("=== ACTIONS ===")
for a in bpy.data.actions:
    print(a.name, a.frame_range[:])
print("=== IMAGES ===")
for i in bpy.data.images:
    print(i.name, i.filepath, i.size[:])
