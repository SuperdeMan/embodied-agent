# Ported from car-agent orchestrator/cloud/executor.py @ f0b08f8, changes: dispatch target is
# SkillRegistry.invoke instead of agent gRPC endpoints (timeout lives in the registry, not
# re-imposed here); confirm suspension (NEED_CONFIRM + Redis session) becomes a synchronous
# confirm callback — fail-closed when absent; params keep native JSON types (origin str()'d
# slots for proto map<string,string>); verification evaluates world-state predicates via
# cognition/verify.py; NEED_SLOT / proto conversion / streaming shortcut paths dropped.
"""DagExecutor: Kahn topo layers → per-layer asyncio.gather → serial between layers.

Chain per step: resolve refs → dedup fingerprint → confirm gate → invoke → verify.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from typing import Any, AsyncIterator, Awaitable, Callable

from embodied.cognition import verify as _verify
from embodied.cognition.plan import CyclicPlan, Plan, PlanContext, Step, StepResult, StepStatus
from embodied.skills.registry import SkillRegistry, SkillResult

logger = logging.getLogger("cognition.executor")

ConfirmCallback = Callable[[str, dict], Awaitable[bool]]

# Transport-uncertain failures: the response didn't come back, which is NOT the same as
# "it didn't happen". Only this family may be re-examined against world state; an explicit
# skill failure is a definite failure and must never be overturned by state evidence.
_UNCERTAIN_ERRORS = ("step_timeout", "timeout")


def _dedup_enabled() -> bool:
    return os.getenv("PLANNER_DEDUP", "on").strip().lower() != "off"


class DagExecutor:
    def __init__(
        self,
        registry: SkillRegistry,
        confirm: ConfirmCallback | None = None,
        world_fn: Callable[[], Any] | None = None,
    ) -> None:
        """
        registry: the only gate to the physical world (invoke enforces its own confirm check
                  and per-skill timeout — defense in depth below this executor).
        confirm:  user confirmation channel; None = headless → dangerous steps DENIED
                  (fail-closed, never fail-open).
        world_fn: () -> WorldSnapshot for state_predicate verification; None → UNKNOWN.
        """
        self._registry = registry
        self._confirm = confirm
        self._world_fn = world_fn

    async def run(
        self, plan: Plan, ctx: PlanContext, done: dict[str, StepResult] | None = None
    ) -> AsyncIterator[StepResult]:
        done = dict(done) if done else {}
        try:
            layers = self._topo_layers(plan.steps, completed_ids=set(done))
        except CyclicPlan as e:
            logger.error("cyclic plan: %s", e)
            yield StepResult(step_id="plan", status=StepStatus.FAILED, error=str(e))
            return

        for layer in layers:
            runnable = [s for s in layer if s.id not in done and self._should_run(s, done)]
            results = []
            if runnable:
                results = await asyncio.gather(
                    *(self._exec_step(s, done, ctx) for s in runnable), return_exceptions=True
                )
            # zip(runnable, results) so an exception branch can never lose its step identity
            for step, res in zip(runnable, results):
                if isinstance(res, Exception):
                    res = StepResult(step_id=step.id, status=StepStatus.FAILED, error=str(res))
                elif not isinstance(res, StepResult):
                    res = StepResult(
                        step_id=step.id, status=StepStatus.FAILED, error=f"unexpected result: {res}"
                    )
                done[res.step_id] = res
                yield res
            # Unlike origin (skipped steps vanished silently into `done`), surface them:
            # the deterministic composer must narrate every planned step's fate.
            before = set(done)
            self._mark_skipped(plan.steps, done)
            for sid in set(done) - before:
                yield done[sid]

    async def _exec_step(self, step: Step, done: dict, ctx: PlanContext) -> StepResult:
        self._resolve_param_refs(step, done)

        fingerprint = self._fingerprint(step)
        prior = self._find_duplicate(fingerprint, done)
        if prior is not None:
            return self._replay_prior(step, prior)

        # Confirm gate (SG-3). Authority is the manifest-populated step field; the registry
        # below re-checks independently (ConfirmationRequired) so an executor bug cannot
        # bypass it either.
        confirmed = False
        if step.require_confirm:
            if self._confirm is None:
                return StepResult(
                    step_id=step.id, status=StepStatus.NEED_CONFIRM,
                    error="confirm_unavailable",
                    detail="dangerous step denied: no confirmation channel",
                )
            confirmed = await self._confirm(step.skill, dict(step.params))
            if not confirmed:
                return StepResult(
                    step_id=step.id, status=StepStatus.NEED_CONFIRM,
                    error="confirm_denied", detail="user denied confirmation",
                )

        result = await self._invoke_once(step, confirmed)
        result = await self._verify_outcome(step, result)
        if result.status == StepStatus.OK:
            result.fingerprint = fingerprint
        elif result.status == StepStatus.FAILED and result.error == "step_timeout":
            # Timeout also fingerprints: the motion may have completed with the response lost;
            # a replan re-issuing the same action must not re-execute it blindly.
            result.fingerprint = fingerprint
        return result

    async def _invoke_once(self, step: Step, confirmed: bool) -> StepResult:
        try:
            r: SkillResult = await self._registry.invoke(step.skill, dict(step.params), confirmed=confirmed)
        except Exception as e:
            return StepResult(step_id=step.id, status=StepStatus.FAILED, error=f"{type(e).__name__}: {e}")
        if r.ok:
            return StepResult(step_id=step.id, status=StepStatus.OK, detail=r.detail, data=dict(r.data))
        error = "step_timeout" if r.detail.startswith("timeout") else (r.detail or "skill_failed")
        return StepResult(
            step_id=step.id, status=StepStatus.FAILED, detail=r.detail, data=dict(r.data), error=error
        )

    # -- verification ----------------------------------------------------------

    async def _verify_outcome(self, step: Step, result: StepResult) -> StepResult:
        """Only verifies steps that CLAIM success; expectations come solely from
        step.verification (manifest authority). UNSAT+retry re-executes once (never for
        require_confirm steps); UNSAT+report keeps OK but records data['_verify'] so the
        composer can be honest. Transport-uncertain failures get one state re-examination
        that may annotate (never flip) the result."""
        verification = step.verification or {}
        if not verification.get("mode") or not _verify.enabled():
            return result
        if result.status != StepStatus.OK:
            return await self._verify_uncertain(step, result)

        attempts = 0
        verdict = await self._evaluate(step, result)
        while (
            verdict == _verify.UNSAT
            and _verify.retry_allowed(verification, step.require_confirm, attempts)
        ):
            attempts += 1
            logger.info("step %s(%s): verify unsat, retrying (%d)", step.id, step.skill, attempts)
            retried = await self._invoke_once(step, confirmed=False)
            if retried.status != StepStatus.OK:
                return retried  # the retry itself failed: back to the normal failure channel
            result = retried
            verdict = await self._evaluate(step, result)

        if verdict == _verify.UNSAT:
            result.data["_verify"] = {
                "verdict": _verify.UNSAT, "mode": verification.get("mode", ""), "attempts": attempts,
            }
            # Honest reporting without flipping status is an aggregator concern in origin;
            # here the deterministic composer reads _verify and words the caveat.
        return result

    async def _verify_uncertain(self, step: Step, result: StepResult) -> StepResult:
        if result.status != StepStatus.FAILED or (result.error or "") not in _UNCERTAIN_ERRORS:
            return result
        if (step.verification or {}).get("mode") != _verify.MODE_STATE:
            return result
        verdict = await self._evaluate(step, result)
        if verdict == _verify.SAT:
            # State proves "it happened"; it cannot prove "this step succeeded". Annotate only.
            result.data["_verify"] = {
                "verdict": _verify.SAT, "mode": _verify.MODE_STATE, "exec": "uncertain_confirmed",
            }
        return result

    async def _evaluate(self, step: Step, result: StepResult) -> str:
        try:
            return await _verify.evaluate(
                step.verification or {}, result.data or {},
                world_fn=self._world_fn, params=dict(step.params or {}),
            )
        except Exception as e:  # fail-open: reconciliation never takes down the main chain
            logger.warning("step %s verify errored (ignored): %s", step.id, e)
            return _verify.UNKNOWN

    # -- dedup -----------------------------------------------------------------

    @staticmethod
    def _fingerprint(step: Step) -> str:
        try:
            params = json.dumps(step.params or {}, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            params = str(sorted((step.params or {}).items(), key=lambda kv: kv[0]))
        raw = f"{step.skill}|{params}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:12]

    @staticmethod
    def _find_duplicate(fingerprint: str, done: dict) -> StepResult | None:
        """Same (skill, params) already executed this turn → suppress the duplicate. OK
        results replay; timeout results also hit (the side effect may have happened). An
        explicit failure never hits — it must be allowed to re-run, else a failure gets
        'reused' into a fake success."""
        if not fingerprint or not _dedup_enabled():
            return None
        for prior in done.values():
            if getattr(prior, "fingerprint", "") != fingerprint:
                continue
            if prior.status == StepStatus.OK:
                return prior
            if prior.status == StepStatus.FAILED and prior.error == "step_timeout":
                return prior
        return None

    @staticmethod
    def _replay_prior(step: Step, prior: StepResult) -> StepResult:
        timed_out = prior.status == StepStatus.FAILED and prior.error == "step_timeout"
        logger.info(
            "step %s(%s): duplicate suppressed (fingerprint=%s%s)",
            step.id, step.skill, prior.fingerprint, ", prior=timeout" if timed_out else "",
        )
        if timed_out:
            return StepResult(
                step_id=step.id, status=StepStatus.OK,
                detail="prior identical action timed out; NOT re-executed — verify state before retrying",
                fingerprint=prior.fingerprint,
            )
        return StepResult(
            step_id=step.id, status=StepStatus.OK, detail=prior.detail,
            data=dict(prior.data or {}), fingerprint=prior.fingerprint,
        )

    # -- param refs ------------------------------------------------------------

    _REF_RE = re.compile(r"^\$\{([^{}]+)\}$")
    _ALIAS_RE = re.compile(r"^\$ref\.([A-Za-z_][A-Za-z0-9_]*)$")

    def _resolve_param_refs(self, step: Step, done: dict) -> None:
        """Three wire forms, most-explicit first (all ported): `${s1.data.x}` placeholders in
        params, the explicit param_refs map, then `$ref.alias` intra-step aliases."""
        for name, raw in list(step.params.items()):
            if not isinstance(raw, str):
                continue
            m = self._REF_RE.fullmatch(raw.strip())
            if not m:
                continue
            value = self._resolve_ref(m.group(1), done)
            if value is not None:
                step.params[name] = value
            else:
                logger.warning("param placeholder %s -> %s resolved to None", name, m.group(1))

        for name, ref_path in step.param_refs.items():
            existing = step.params.get(name)
            # "Existing value wins" — unless the value IS the ref path written twice.
            if name in step.params and str(existing).strip() != str(ref_path).strip():
                continue
            value = self._resolve_ref(ref_path, done)
            if value is not None:
                step.params[name] = value
            else:
                logger.warning("param_ref %s -> %s resolved to None", name, ref_path)

        for name, raw in list(step.params.items()):
            if not isinstance(raw, str):
                continue
            m = self._ALIAS_RE.fullmatch(raw.strip())
            if not m:
                continue
            value = step.params.get(m.group(1))
            if value is not None and value != raw:
                step.params[name] = value
            else:
                logger.warning("param alias %s -> %s resolved to None", name, m.group(1))

    @staticmethod
    def _resolve_ref(ref_path: str, done: dict) -> Any:
        parts = str(ref_path).split(".")
        if len(parts) < 3 or parts[1] != "data":
            return None
        result = done.get(parts[0])
        if not result:
            return None
        obj: Any = result.data
        for key in parts[2:]:
            if isinstance(obj, dict):
                obj = obj.get(key)
            elif isinstance(obj, (list, tuple)):
                try:
                    obj = obj[int(key)]
                except (IndexError, ValueError):
                    return None
            else:
                return None
        return obj

    # -- dag helpers -----------------------------------------------------------

    @staticmethod
    def _should_run(step: Step, done: dict) -> bool:
        for dep_id in step.depends_on:
            dep = done.get(dep_id)
            if not dep or dep.status != StepStatus.OK:
                return False
        return True

    @staticmethod
    def _mark_skipped(steps: list[Step], done: dict) -> None:
        for s in steps:
            if s.id in done:
                continue
            for dep_id in s.depends_on:
                dep = done.get(dep_id)
                if dep and dep.status in (
                    StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.NEED_CONFIRM
                ):
                    done[s.id] = StepResult(
                        step_id=s.id, status=StepStatus.SKIPPED,
                        error=f"dependency {dep_id} {dep.status.value}",
                    )
                    break

    @staticmethod
    def _topo_layers(steps: list[Step], completed_ids: set[str] | None = None) -> list[list[Step]]:
        by_id = {s.id: s for s in steps}
        completed_ids = completed_ids or set()
        in_degree: dict[str, int] = defaultdict(int)
        children: dict[str, list[str]] = defaultdict(list)
        for s in steps:
            for dep in s.depends_on:
                if dep in by_id:
                    in_degree[s.id] += 1
                    children[dep].append(s.id)
                elif dep not in completed_ids:
                    in_degree[s.id] += 1  # unknown dependency stays blocked: fail closed

        layers: list[list[Step]] = []
        remaining = set(by_id)
        while remaining:
            layer_ids = [sid for sid in remaining if in_degree[sid] == 0]
            if not layer_ids:
                raise CyclicPlan(f"cycle detected in plan: {sorted(remaining)}")
            layers.append([by_id[sid] for sid in sorted(layer_ids)])
            for sid in layer_ids:
                remaining.discard(sid)
                for child in children[sid]:
                    in_degree[child] -= 1
        return layers
