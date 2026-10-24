"""Read-only Blender source audit. Run with blender -b SOURCE --python this.py."""
import json
from pathlib import Path
import bpy

report = {'source': bpy.data.filepath, 'armatures': {}, 'meshes': {}, 'actions': []}
for name in ('Position', 'Position.001'):
    arm = bpy.data.objects[name]
    report['armatures'][name] = {
        'bones': len(arm.data.bones),
        'scale': list(arm.scale),
        'constraints': sum(len(b.constraints) for b in arm.pose.bones),
        'secondary_bones': [b.name for b in arm.data.bones if 'hair' in b.name.lower() or 'skirt' in b.name.lower()],
        'leg_rest': {b.name: {'head': list(b.head_local), 'tail': list(b.tail_local), 'length': b.length}
                     for b in arm.data.bones if b.name in ('Left_Thigh', 'Left_Knee', 'Left_Ankle')},
    }
for name in ('Body', 'Face', 'Acc'):
    mesh = bpy.data.objects[name]
    sums = [sum(g.weight for g in v.groups) for v in mesh.data.vertices]
    report['meshes'][name] = {
        'vertices': len(mesh.data.vertices), 'groups': len(mesh.vertex_groups),
        'unweighted': sum(s < .0001 for s in sums),
        'nonunit_weight_sum': sum(abs(s - 1) > .01 for s in sums),
        'weight_range': [min(sums), max(sums)],
        'scale': list(mesh.scale), 'modifiers': [m.type for m in mesh.modifiers],
        'shape_keys': [k.name for k in mesh.data.shape_keys.key_blocks] if mesh.data.shape_keys else [],
    }
report['actions'] = [{'name': a.name, 'range': list(a.frame_range)} for a in bpy.data.actions]
out = Path(__file__).resolve().parents[1] / 'data/validation/source-rig-audit.json'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(out)
