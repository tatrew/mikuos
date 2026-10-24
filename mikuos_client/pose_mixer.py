"""Sample the exported glTF clips; one writer owns every animated joint.

Qt timelines are disabled while this mixer runs. Crossfades start from the
currently displayed pose, including when a transition is interrupted.
"""
from __future__ import annotations

import bisect
import json
import math
import struct
from pathlib import Path


def mix(a, b, weight, rotation=False):
    if weight <= 0:
        return a
    if rotation:
        dot = sum(x * y for x, y in zip(a, b))
        if dot < 0:
            b, dot = tuple(-x for x in b), -dot
        if dot < 0.9995:
            angle = math.acos(max(-1, min(1, dot)))
            sa = math.sin((1 - weight) * angle) / math.sin(angle)
            sb = math.sin(weight * angle) / math.sin(angle)
            return tuple(sa * x + sb * y for x, y in zip(a, b))
    value = tuple(x + (y - x) * weight for x, y in zip(a, b))
    if rotation:
        norm = math.sqrt(sum(x * x for x in value)) or 1
        return tuple(x / norm for x in value)
    return value


class ClipLibrary:
    def __init__(self, path: Path):
        raw = path.read_bytes()
        magic, version, size = struct.unpack_from('<4sII', raw)
        if magic != b'glTF' or version != 2 or size != len(raw):
            raise ValueError('Invalid GLB')
        offset, binary, doc = 12, None, None
        while offset < len(raw):
            length, kind = struct.unpack_from('<II', raw, offset)
            chunk = raw[offset + 8:offset + 8 + length]
            if kind == 0x4E4F534A:
                doc = json.loads(chunk)
            elif kind == 0x004E4942:
                binary = chunk
            offset += 8 + length
        if doc is None or binary is None:
            raise ValueError('GLB requires JSON and BIN')
        self.document = doc
        self.names = {i: n.get('name', '') for i, n in enumerate(doc['nodes'])}
        self.rest = {}
        self.clips = {}
        cache = {}

        def accessor(index):
            if index in cache:
                return cache[index]
            a = doc['accessors'][index]
            if a['componentType'] != 5126 or 'sparse' in a:
                raise ValueError('Animation accessors must be dense floats')
            count = {'SCALAR': 1, 'VEC3': 3, 'VEC4': 4}[a['type']]
            view = doc['bufferViews'][a['bufferView']]
            start = view.get('byteOffset', 0) + a.get('byteOffset', 0)
            stride = view.get('byteStride', count * 4)
            result = [struct.unpack_from('<' + 'f' * count, binary, start + i * stride)
                      for i in range(a['count'])]
            cache[index] = result
            return result

        for clip in doc.get('animations', []):
            tracks = {}
            duration = 0
            for channel in clip['channels']:
                target = channel['target']
                prop = target['path']
                if prop not in ('rotation', 'translation', 'scale'):
                    continue
                key = (target['node'], prop)
                n = doc['nodes'][key[0]]
                self.rest[key] = tuple(n.get(prop, {'rotation': [0, 0, 0, 1],
                                                   'translation': [0, 0, 0], 'scale': [1, 1, 1]}[prop]))
                sampler = clip['samplers'][channel['sampler']]
                interpolation = sampler.get('interpolation', 'LINEAR')
                if interpolation not in ('LINEAR', 'STEP'):
                    raise ValueError('Unsupported clip interpolation: ' + interpolation)
                times = [x[0] for x in accessor(sampler['input'])]
                values = accessor(sampler['output'])
                duration = max(duration, times[-1])
                if all(all(abs(x - y) < 1e-7 for x, y in zip(values[0], v)) for v in values):
                    times, values = [times[0]], [values[0]]
                tracks[key] = (times, values, interpolation)
            self.clips[clip['name']] = (duration, tracks)

    def sample(self, name, time, loop=True):
        duration, tracks = self.clips[name]
        t = time % duration if loop and duration else min(time, duration)
        pose = dict(self.rest)
        for key, (times, values, interp) in tracks.items():
            i = max(0, bisect.bisect_right(times, t) - 1)
            j = min(i + 1, len(times) - 1)
            w = (t - times[i]) / (times[j] - times[i]) if j != i and interp != 'STEP' else 0
            pose[key] = mix(values[i], values[j], w, key[1] == 'rotation')
        return pose


class PoseMixer:
    ONE_SHOT = {'sit', 'stand_up', 'land', 'wave', 'surprised', 'happy', 'jump'}

    def __init__(self, library):
        self.library = library
        self.name = 'idle'
        self.time = 0.0
        self.elapsed = 1.0
        self.duration = 0.28
        self.pose = library.sample('idle', 0)
        self.source = dict(self.pose)

    def update(self, name, dt, speed=1.0):
        if name not in self.library.clips:
            raise ValueError('Missing animation: ' + name)
        dt = max(0, min(0.1, dt))
        if name != self.name:
            self.source = dict(self.pose)
            self.name, self.time, self.elapsed = name, 0.0, 0.0
        else:
            self.time += dt * max(0, speed)
            self.elapsed += dt
        target = self.library.sample(name, self.time, name not in self.ONE_SHOT)
        w = min(1, self.elapsed / self.duration)
        w = w * w * (3 - 2 * w)
        self.pose = {k: v if self.source[k] == v else mix(self.source[k], v, w, k[1] == 'rotation')
                     for k, v in target.items()}
        return self.pose
