"""Import shim for generated proto stubs (gen/python, gitignored).

Stubs are build artifacts (D011): regenerate any time with scripts/gen-proto.ps1|.sh.
Generated modules live under the `embodiedrpc.*` python root (proto FILE directory) while
proto PACKAGES stay `embodied.<service>.v1` per convention — the split exists exactly so
generated code can never collide with the real `embodied` package.
"""

from __future__ import annotations

import sys
from pathlib import Path

GEN_DIR = Path(__file__).resolve().parents[3] / "gen" / "python"


def ensure_stubs() -> None:
    if not (GEN_DIR / "embodiedrpc").is_dir():
        raise RuntimeError(
            "proto stubs not generated — run scripts/gen-proto.ps1 (or .sh) first; "
            f"expected them under {GEN_DIR}"
        )
    p = str(GEN_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def import_control():
    ensure_stubs()
    from embodiedrpc.control.v1 import control_pb2, control_pb2_grpc

    return control_pb2, control_pb2_grpc


def import_safety():
    ensure_stubs()
    from embodiedrpc.safety.v1 import safety_pb2, safety_pb2_grpc

    return safety_pb2, safety_pb2_grpc


def import_common():
    ensure_stubs()
    from embodiedrpc.common.v1 import common_pb2

    return common_pb2
