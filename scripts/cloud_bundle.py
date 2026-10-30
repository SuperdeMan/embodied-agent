"""Prepare an immutable cloud build context without deploying it.

准备不可变云端构建上下文，不执行部署。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(output_root: Path) -> Path:
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise RuntimeError("cloud bundle requires a clean committed worktree")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    destination = output_root.resolve() / sha
    destination.mkdir(parents=True, exist_ok=False)
    archive = subprocess.check_output(["git", "archive", "--format=tar", sha], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")
    models = list((ROOT / "assets" / "menagerie").glob("*/.source"))
    if len(models) != 1:
        raise RuntimeError("exactly one pinned Menagerie model is required")
    model = models[0].parent
    if any(path.is_symlink() for path in model.rglob("*")):
        raise RuntimeError("model payload may not contain symbolic links")
    target = destination / "assets" / "menagerie" / model.name
    shutil.copytree(model, target)
    digests = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob("*")) if path.is_file()
    }
    manifest = {"commit": sha, "profile": "offline-simulation", "model_source": models[0].read_text(),
                "sha256": digests, "activation": "not_deployed"}
    (destination / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "cloud")
    args = parser.parse_args()
    print(prepare(args.out))


if __name__ == "__main__":
    main()
