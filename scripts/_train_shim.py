"""lerobot-train launcher with a Windows checkpoint-symlink fallback.

lerobot's ``update_last_checkpoint`` maintains ``checkpoints/last`` as a symlink;
on Windows, ``os.symlink`` needs Developer Mode / admin privileges (WinError 1314),
which we must not require (global-config red line). Directory junctions are the
privilege-free equivalent, so: try lerobot's original first, fall back to a
junction. Everything that READS ``last`` (resume, export) sees a normal directory
either way. scripts/train.py runs this module as its subprocess entry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _install_windows_fallback() -> None:
    if os.name != "nt":
        return
    import lerobot.scripts.lerobot_train as lt
    import lerobot.utils.train_utils as tu

    orig = tu.update_last_checkpoint

    def patched(checkpoint_dir: Path) -> None:
        try:
            orig(checkpoint_dir)
        except OSError:
            import _winapi

            last = checkpoint_dir.parent / "last"
            if last.is_symlink() or last.exists():
                # A junction (or half-made link) unlinks like an empty dir; the
                # checkpoint data itself lives in checkpoint_dir and is untouched.
                try:
                    last.unlink()
                except OSError:
                    os.rmdir(last)
            _winapi.CreateJunction(str(checkpoint_dir.resolve()), str(last))

    tu.update_last_checkpoint = patched
    # lerobot_train imported the name directly; rebind its module-level reference too.
    lt.update_last_checkpoint = patched


def main() -> None:
    _install_windows_fallback()
    from lerobot.scripts.lerobot_train import main as train_main

    train_main()


if __name__ == "__main__":
    sys.exit(main())
