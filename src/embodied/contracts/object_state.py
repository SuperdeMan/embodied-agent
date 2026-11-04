"""Local semantic observation lifetime. 本地语义观察的有效期。"""

from __future__ import annotations

import math
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ObjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False,
                             revalidate_instances="always")
    clock_id: str = Field(min_length=1)
    scene_epoch: str = Field(min_length=1)
    calibration_id: str = Field(min_length=1)


class ObjectEvidence(ObjectContext):
    source: Literal["vision", "kinematic"] = "vision"
    sensor_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    captured_sim_t: float = Field(ge=0)
    captured_monotonic: float = Field(ge=0)
    ttl_sim_s: float = Field(gt=0)
    ttl_wall_s: float = Field(gt=0)

    def current(self, context: ObjectContext | None, sim_t: float, now: float) -> bool:
        if context is None or not math.isfinite(sim_t) or not math.isfinite(now):
            return False
        return (all(getattr(self, key) == getattr(context, key) for key in ObjectContext.model_fields)
                and 0 <= sim_t - self.captured_sim_t < self.ttl_sim_s
                and 0 <= now - self.captured_monotonic < self.ttl_wall_s)


def object_available(snapshot, name: str, *, now: float | None = None, sim_t: float | None = None,
                     context: ObjectContext | None = None) -> bool:
    """Reference fixtures without semantic metadata retain their existing contract.

    无语义元数据的参考夹具保留原契约；带元数据的观察必须提供有效证据。
    """
    if name not in snapshot.objects:
        return False
    evidence = getattr(snapshot, "object_evidence", None)
    if evidence is None:
        return True
    try:
        item = ObjectEvidence.model_validate(evidence[name])
        active = context if context is not None else snapshot.object_context
        return item.current(active, snapshot.t if sim_t is None else sim_t,
                            time.monotonic() if now is None else now)
    except (KeyError, AttributeError, ValueError, TypeError):
        return False
