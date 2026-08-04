# Ported from car-agent orchestrator/cloud/clients.py (_SENSITIVE_SCOPE + _merge_meta scope filter) @ f0b08f8, changes: mechanism extracted into a standalone module; origin embodiment-state scope key renamed robot_state (battery context key now robot_battery); car-domain GDPR target registry from runtime/privacy_registry.py not carried (adapters/targets reference modules that do not exist here; revisit in M1).
"""Sensitive-context minimization: the ``context_scopes`` declaration/strip mechanism.

Skills declare which sensitive context scopes they need (manifest field
``context_scopes``); the dispatcher calls :func:`strip_sensitive` before
handing per-turn context to a skill, so undeclared sensitive keys are never
broadcast. Scope vocabulary (``CONTEXT_SCOPES``):

- ``location``  — precise position fix keys
- ``robot_state`` — embodiment internals (e.g. battery level)
- ``vision``    — camera frame references (the image itself never rides
  along; only the reference does, and even that is scope-gated)

Filtering semantics are identical to the origin: ``context_scopes=None``
(legacy/local paths) means no filtering; an explicit list — even an empty
one — means minimize.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

# 敏感上下文键 → 所需 scope。技能经 manifest context_scopes 声明后才下发（最小化）。
SENSITIVE_SCOPE: dict[str, str] = {
    "current_lat": "location",
    "current_lng": "location",
    "current_accuracy_m": "location",
    "current_location_at": "location",
    "current_location_source": "location",
    "robot_battery": "robot_state",
    # 相机单帧的**引用**。只有声明 context_scopes: [vision] 的技能收得到——
    # 图像引用属敏感上下文，不该随每轮广播给全部技能。
    "vision_frame_id": "vision",
}

# 合法的 context scope 声明全集（manifest 校验可用）。
CONTEXT_SCOPES: frozenset[str] = frozenset(SENSITIVE_SCOPE.values())


def strip_sensitive(prefs: Mapping | None,
                    context_scopes: Iterable[str] | None = None) -> dict:
    """按声明最小化敏感上下文键，返回过滤后的新 dict（不改入参）。

    context_scopes 非 None 时：未声明 location/robot_state/vision 的调用方
    收不到对应敏感键；非敏感键始终保留。None = 不过滤（保持既有行为）。
    """
    out = dict(prefs or {})
    if context_scopes is None:
        return out
    allowed = set(context_scopes or [])
    return {k: v for k, v in out.items()
            if SENSITIVE_SCOPE.get(k) is None
            or SENSITIVE_SCOPE.get(k) in allowed}


__all__ = ["CONTEXT_SCOPES", "SENSITIVE_SCOPE", "strip_sensitive"]
