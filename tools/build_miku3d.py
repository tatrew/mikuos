"""Build the real-time 3D Miku used by the overlay.

  .venv\\Scripts\\python tools\\build_miku3d.py [--blender PATH]

1. Blender (background) runs tools/export_miku.py: cleans the Project SEKAI scene,
   rigs the head to the neck, authors the animations, exports assets/miku3d/miku.glb.
2. balsam (ships with PySide6) converts the glb into a Qt Quick 3D component
   assets/miku3d/qml/Miku.qml (+ meshes, textures, animation data).
3. The generated QML is patched so that exactly one animation plays, selected by
   the `animState` property, morph-target weights (blink, mouth, gaze, emotions)
   are bound to root properties, and materials use flat anime-style shading.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLEND = ROOT / "assets" / "miku_model" / "source" / "GameObject" / "GameObject.blend"
OUT_DIR = ROOT / "assets" / "miku3d"
DEFAULT_BLENDER = Path(r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe")

MORPHS = ["eye_close", "eye_happy", "eye_sad", "mouth_a", "mouth_i", "mouth_u", "mouth_e", "mouth_o",
          "mouth_happy", "mouth_sad", "look_left", "look_right", "look_up", "look_down", "eyeblow_happy",
          "eyeblow_sad"]


def find_blender(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if DEFAULT_BLENDER.exists():
        return DEFAULT_BLENDER
    found = sorted(Path(r"C:\Program Files\Blender Foundation").glob("Blender */blender.exe"))
    if found:
        return found[-1]
    exe = shutil.which("blender")
    if exe:
        return Path(exe)
    raise SystemExit("Blender not found; pass --blender PATH")


def patch_qml(path: Path) -> None:
    s = path.read_text(encoding="utf-8")
    props = ["    property string animState: \"idle\"", "    property bool externalAnimation: false"] + [
        f"    property real {m}: 0" for m in MORPHS]
    s = s.replace('    objectName: "Root"\n', '    objectName: "Root"\n' + "\n".join(props) + "\n", 1)

    # one Timeline at a time
    def timeline(m: re.Match) -> str:
        block = m.group(0)
        name = re.search(r'objectName: "([^"]+)"', block).group(1)
        cond = f'!root.externalAnimation && root.animState === "{name}"'
        block = block.replace("enabled: true", f"enabled: {cond}", 1)
        block = block.replace("running: true", f"running: {cond}", 1)
        return block
    s = re.sub(r"    Timeline \{\n        id: \w+\n        objectName: \"[^\"]+\"(?:.|\n)*?running: true", timeline, s)

    # morph weights
    for m in MORPHS:
        s = s.replace(f'        objectName: "{m}"\n        attributes:',
                      f'        objectName: "{m}"\n        weight: root.{m}\n        attributes:', 1)

    # anime look: unlit base colour, alpha-tested (no blend sorting artefacts)
    s = s.replace("alphaMode: PrincipledMaterial.Blend",
                  "alphaMode: PrincipledMaterial.Mask\n        alphaCutoff: 0.5\n"
                  "        lighting: PrincipledMaterial.NoLighting")
    path.write_text(s, encoding="utf-8")
    n_tl = s.count("enabled: !root.externalAnimation")
    n_mt = sum(f"weight: root.{m}" in s for m in MORPHS)
    print(f"patched {path.name}: {n_tl} timelines switchable, {n_mt} morph weights bound")
    if n_tl < 10:
        raise SystemExit("unexpected balsam output: animations not found")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blender")
    ap.add_argument("--skip-blender", action="store_true", help="reuse existing miku.glb")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    glb = OUT_DIR / "miku.glb"
    if not args.skip_blender:
        if not BLEND.exists():
            raise SystemExit(f"model not found: {BLEND}")
        blender = find_blender(args.blender)
        print("Blender:", blender)
        r = subprocess.run([str(blender), "-b", str(BLEND), "--python", str(ROOT / "tools" / "export_miku.py"),
                            "--", str(glb)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not glb.exists():
            print(r.stdout[-3000:], r.stderr[-3000:])
            raise SystemExit("Blender export failed")
    qml_dir = OUT_DIR / "qml"
    if qml_dir.exists():
        shutil.rmtree(qml_dir)
    import PySide6
    balsam = Path(PySide6.__file__).parent / "balsam.exe"
    r = subprocess.run([str(balsam), "-o", str(qml_dir), str(glb)], capture_output=True, text=True)
    if r.returncode != 0 or not (qml_dir / "Miku.qml").exists():
        print(r.stdout, r.stderr)
        raise SystemExit("balsam conversion failed")
    patch_qml(qml_dir / "Miku.qml")
    print("done:", qml_dir / "Miku.qml")


if __name__ == "__main__":
    sys.exit(main())
