"""Fetch simulation assets (MuJoCo Menagerie SO-ARM model) into assets/menagerie/.

Pointer-not-payload policy (CLAUDE.md): git tracks this script + assets/README.md;
the downloaded model payload is gitignored. Re-run any time to (re)materialize.

Usage: uv run python scripts/fetch_assets.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "assets" / "menagerie"
REPO = "https://github.com/google-deepmind/mujoco_menagerie.git"
# Prefer SO-ARM101 when Menagerie ships it; fall back to SO-ARM100.
CANDIDATES = ["trs_so_arm101", "so_arm101", "trs_so_arm100"]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", REPO, td],
            check=True,
        )
        subprocess.run(["git", "-C", td, "sparse-checkout", "set", *CANDIDATES], check=True)
        commit = subprocess.run(
            ["git", "-C", td, "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        picked = next(
            (c for c in CANDIDATES if (Path(td) / c).is_dir() and any((Path(td) / c).iterdir())),
            None,
        )
        if picked is None:
            print(f"none of {CANDIDATES} found in menagerie @ {commit}", file=sys.stderr)
            return 1
        target = DEST / picked
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(td) / picked, target)
        (target / ".source").write_text(f"{REPO} @ {commit} : {picked}\n", encoding="utf-8")
    files = sorted(p.name for p in target.iterdir())
    print(f"fetched {picked} @ {commit} -> {target}")
    print(f"files: {', '.join(files[:12])}{' ...' if len(files) > 12 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
