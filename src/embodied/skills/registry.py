"""SkillRegistry: the only gate through which the planner reaches the physical world.

Two invariants are pinned by contract tests and must never regress:
1. Adding a skill = register(manifest, handler); the core is never edited for it.
2. require_confirm skills cannot execute without confirmed=True — the gate lives HERE,
   below the planner, so a planner bug cannot bypass it (SG-3, docs/architecture.md §4.5).
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from embodied.runtime.invocation import CURRENT_INVOCATION, InvocationPermit
from embodied.skills.manifest import SkillManifest


class SkillError(RuntimeError):
    pass


class SkillNotFound(SkillError):
    pass


class DuplicateSkill(SkillError):
    pass


class InvalidParams(SkillError):
    pass


class ConfirmationRequired(SkillError):
    def __init__(self, name: str):
        super().__init__(f"skill {name} requires explicit confirmation")
        self.skill = name


@dataclass
class SkillResult:
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


SkillHandler = Callable[..., Awaitable[SkillResult]]


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, tuple[SkillManifest, SkillHandler]] = {}
        self._retiring: set[asyncio.Task] = set()

    def register(self, manifest: SkillManifest, handler: SkillHandler) -> None:
        if manifest.name in self._skills:
            raise DuplicateSkill(manifest.name)
        if not inspect.iscoroutinefunction(handler):
            raise SkillError(f"handler for {manifest.name} must be an async function")
        self._skills[manifest.name] = (manifest, handler)

    def get(self, name: str) -> SkillManifest:
        try:
            return self._skills[name][0]
        except KeyError:
            raise SkillNotFound(name) from None

    def catalog(self) -> list[SkillManifest]:
        return [m for m, _ in sorted(self._skills.values(), key=lambda t: t[0].name)]

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [m.to_tool_schema() for m in self.catalog()]

    def resolve_tool(self, tool_name: str) -> str:
        name = tool_name.replace("-", ".")
        if name not in self._skills:
            raise SkillNotFound(tool_name)
        return name

    async def invoke(self, name: str, params: dict[str, Any] | None = None, *, confirmed: bool = False) -> SkillResult:
        try:
            manifest, handler = self._skills[name]
        except KeyError:
            raise SkillNotFound(name) from None
        if manifest.require_confirm and not confirmed:
            raise ConfirmationRequired(name)
        merged = self._validate_params(manifest, {} if params is None else params)
        permit = InvocationPermit(manifest.termination.timeout_s)
        binding = CURRENT_INVOCATION.set(permit)
        task = None
        try:
            task = asyncio.create_task(handler(**merged))
            done, _ = await asyncio.wait({task}, timeout=manifest.termination.timeout_s)
            with permit.access() as allowed:
                timed_out = not done or not allowed
            if timed_out:
                return SkillResult(
                    ok=False, detail=f"timeout after {manifest.termination.timeout_s}s",
                    data={"_execution": {"outcome": "OUTCOME_UNKNOWN", "stop_state": "STOP_UNCONFIRMED"}},
                )
            return task.result()
        except SkillError:
            raise
        except Exception as e:  # handler faults are operational failures, not crashes
            return SkillResult(ok=False, detail=f"{type(e).__name__}: {e}")
        finally:
            # Revoke BEFORE coroutine cancellation; to_thread work may continue running.
            permit.revoke()
            CURRENT_INVOCATION.reset(binding)
            if task is not None and not task.done():
                task.cancel()
                self._retiring.add(task)
                task.add_done_callback(self._retired)

    def _retired(self, task: asyncio.Task) -> None:
        self._retiring.discard(task)
        if not task.cancelled():
            task.exception()  # consume late handler faults without reviving the invocation

    @staticmethod
    def _validate_params(manifest: SkillManifest, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
            raise InvalidParams(f"{manifest.name}: params must be an object with string keys")
        unknown = set(params) - set(manifest.params)
        if unknown:
            raise InvalidParams(f"{manifest.name}: unknown params {sorted(unknown)}")
        merged: dict[str, Any] = {}
        for pname, spec in manifest.params.items():
            if pname in params:
                merged[pname] = params[pname]
            elif spec.default is not None:
                merged[pname] = spec.default
            elif spec.required:
                raise InvalidParams(f"{manifest.name}: missing required param {pname!r}")
            if pname in merged:
                try:
                    spec.validate_value(merged[pname])
                except (ValueError, TypeError, RecursionError) as exc:
                    raise InvalidParams(f"{manifest.name}: invalid param {pname!r}: {exc}") from exc
        return merged
