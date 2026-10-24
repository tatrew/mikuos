"""Blender source diagnostics for material slots, UVs and limb weights."""
import bpy
import json
from collections import Counter
from pathlib import Path

result = {}
for name in ('Body', 'Face', 'Acc'):
    obj = bpy.data.objects[name]
    mats = []
    for mat in obj.data.materials:
        mats.append({'name': mat.name, 'images': [
            {'name': n.image.name, 'path': n.image.filepath, 'size': list(n.image.size),
             'packed': bool(n.image.packed_file)}
            for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image],
            'links': [(l.from_node.name, l.from_socket.name, l.to_node.name, l.to_socket.name)
                      for l in mat.node_tree.links]})
    groups = {}
    for group in obj.vertex_groups:
        indices = [v.index for v in obj.data.vertices if any(g.group == group.index and g.weight > .001 for g in v.groups)]
        if 'Arm' in group.name or 'Elbow' in group.name or 'Wrist' in group.name or 'Head' in group.name:
            coords = [obj.matrix_world @ obj.data.vertices[i].co for i in indices]
            groups[group.name] = {'count': len(indices), 'bounds':
                                 [[min(c[i] for c in coords), max(c[i] for c in coords)] for i in range(3)] if coords else []}
    result[name] = {'materials': mats, 'material_faces': dict(Counter(p.material_index for p in obj.data.polygons)),
                    'uv_layers': [(l.name, l.active_render, len(l.data)) for l in obj.data.uv_layers],
                    'groups': groups, 'matrix_world': [list(row) for row in obj.matrix_world]}
out = Path(__file__).resolve().parents[1] / 'data/validation/skin-material-audit.json'
out.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(out)
