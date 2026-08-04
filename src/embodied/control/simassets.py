"""Scene materialization for MuJoCo: ASCII-safe staging + model patching + composition.

MuJoCo's XML parser cannot open non-ASCII paths (this repo may live under a Chinese
path), so every scene is staged into an ASCII temp dir per process. The Menagerie arm
model is staged verbatim except for one patch: a `grasp` site injected into the
Fixed_Jaw body — the upstream model ships no end-effector site and differential IK
needs one. The patch anchors on the Wrist_Roll joint line, which is stable because
assets are pinned by commit (see assets/README.md and <model>/.source).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

_GRASP_SITE = '<site name="grasp" pos="0 -0.08 0" size="0.004" rgba="1 0 0 0.35" group="4"/>'
_ANCHOR = '<joint name="Wrist_Roll"'
_ARM_PLACEHOLDER = "__ARM_XML__"

_STAGE_CACHE: dict[tuple[str, str], Path] = {}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def find_menagerie_model(root: Path | None = None) -> Path:
    root = root or repo_root()
    hits = sorted((root / "assets" / "menagerie").glob("*/so_arm*.xml"))
    if not hits:
        raise FileNotFoundError(
            "no SO-ARM model under assets/menagerie — run `uv run python scripts/fetch_assets.py` first"
        )
    return hits[0].parent


def stage_tabletop(model_dir: Path | None = None, scene_template: Path | None = None) -> Path:
    """Stage arm model + tabletop scene into an ASCII dir; return the loadable scene path."""
    model_dir = model_dir or find_menagerie_model()
    scene_template = scene_template or repo_root() / "sim" / "tabletop.xml"
    key = (str(model_dir), str(scene_template))
    cached = _STAGE_CACHE.get(key)
    if cached is not None and cached.exists():
        return cached

    stage = Path(tempfile.mkdtemp(prefix="embodied_scene_"))
    for item in model_dir.iterdir():
        if item.is_dir():
            shutil.copytree(item, stage / item.name)
        else:
            shutil.copy2(item, stage / item.name)

    arm_xml = next(stage.glob("so_arm*.xml"))
    _patch_grasp_site(arm_xml)

    scene_text = scene_template.read_text(encoding="utf-8")
    if _ARM_PLACEHOLDER not in scene_text:
        raise RuntimeError(f"{scene_template} lacks {_ARM_PLACEHOLDER} include placeholder")
    scene = stage / "tabletop.xml"
    scene.write_text(scene_text.replace(_ARM_PLACEHOLDER, arm_xml.name), encoding="utf-8")
    _STAGE_CACHE[key] = scene
    return scene


def _patch_grasp_site(arm_xml: Path) -> None:
    text = arm_xml.read_text(encoding="utf-8")
    if 'name="grasp"' in text:
        return
    if _ANCHOR not in text:
        raise RuntimeError(f"anchor {_ANCHOR!r} not found in {arm_xml.name}; upstream model changed, re-pin the patch")
    out: list[str] = []
    for line in text.splitlines():
        out.append(line)
        if _ANCHOR in line and "<joint" in line:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + _GRASP_SITE)
    arm_xml.write_text("\n".join(out) + "\n", encoding="utf-8")
