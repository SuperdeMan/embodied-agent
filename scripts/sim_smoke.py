"""Load the fetched SO-ARM model in MuJoCo, step physics, render one offscreen frame.

M1 scouting (docs/roadmap.md): proves the sim asset + toolchain before real work starts.
Local-only — CI installs neither the sim group nor the assets.

Usage: uv run --group sim python scripts/sim_smoke.py [out.png]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_model(scene: Path):
    """MuJoCo's native XML parser cannot open non-ASCII paths (this repo lives under 产品/);
    fall back to copying the model dir to an ASCII temp location."""
    import mujoco

    try:
        return mujoco.MjModel.from_xml_path(str(scene))
    except ValueError:
        if scene.as_posix().isascii():
            raise
        import shutil
        import tempfile

        td = Path(tempfile.mkdtemp(prefix="embodied_sim_"))
        dst = td / scene.parent.name
        shutil.copytree(scene.parent, dst)
        print(f"non-ASCII repo path; loading via temp copy {dst}")
        return mujoco.MjModel.from_xml_path(str(dst / scene.name))


def main() -> int:
    import mujoco

    candidates = sorted((ROOT / "assets" / "menagerie").glob("*/scene.xml"))
    if not candidates:
        print("no scene.xml under assets/menagerie; run scripts/fetch_assets.py first", file=sys.stderr)
        return 1
    scene = candidates[0]
    model = _load_model(scene)
    data = mujoco.MjData(model)
    for _ in range(200):
        mujoco.mj_step(model, data)
    print(
        f"physics ok: {scene.parent.name} nq={model.nq} nv={model.nv} "
        f"nbody={model.nbody} sim_time={data.time:.3f}s"
    )
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "outputs" / "sim_smoke.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        renderer = mujoco.Renderer(model, height=480, width=640)
        mujoco.mj_forward(model, data)
        renderer.update_scene(data)
        pixels = renderer.render()
        renderer.close()
        from PIL import Image

        Image.fromarray(pixels).save(out)
        print(f"render ok: {pixels.shape} -> {out}")
    except Exception as e:
        print(f"offscreen render unavailable ({type(e).__name__}: {e}); physics-only smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
