# 保留来源：执行目标改为 Registry，超时由其负责；确认改为轮内回调且缺失时拒绝。参数保留 JSON 类型，验证改读世界状态；移除旧补槽和流式快捷路径。
# Ported from car-agent orchestrator/cloud/executor.py @ f0b08f8, changes: dispatch target is
# SkillRegistry.invoke instead of agent gRPC endpoints (timeout lives in the registry, not
# re-imposed here); confirm suspension (NEED_CONFIRM + Redis session) becomes a synchronous
# confirm callback — fail-closed when absent; params keep native JSON types (origin str()'d
# slots for proto map<string,string>); verification evaluates world-state predicates via
# cognition/verify.py; NEED_SLOT / proto conversion / streaming shortcut paths dropped.
"""DagExecutor: Kahn topo layers → per-layer asyncio.gather → serial between layers.

Chain per step: resolve refs → dedup fingerprint → confirm gate → invoke → verify.


Kahn 拓扑分层，同层并行、层间串行；每步解析引用、去重、确认、调用并验证。
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
from uuid import NAMESPACE_URL, uuid5

from embodied.cognition import verify as _verify
from embodied.cognition.plan import CyclicPlan, Plan, PlanContext, Step, StepResult, StepStatus
from embodied.contracts.operation import OperationIdentity
from embodied.skills.registry import SkillRegistry, SkillResult

logger = logging.getLogger("cognition.executor")

ConfirmCallback = Callable[[str, dict], Awaitable[bool]]

# 未收到回执不等于动作未发生；仅传输不确定可补查世界状态，不能用状态推翻明确技能失败。
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


        registry 在执行器之下再次强制确认和超时；无确认回调时危险动作拒绝；world_fn 提供验证快照，缺失则 UNKNOWN。
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
            # 按步骤与结果配对，异常分支也保留步骤身份。
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
            # 显式呈现跳过步骤，确保汇报涵盖每一步的去向。
            # Unlike origin (skipped steps vanished silently into `done`), surface them:
            # the deterministic composer must narrate every planned step's fate.
            before = set(done)
            self._mark_skipped(plan.steps, done)
            for sid in set(done) - before:
                yield done[sid]

    async def _exec_step(self, step: Step, done: dict, ctx: PlanContext) -> StepResult:
        self._resolve_param_refs(step, done)

        fingerprint = self._fingerprint(step)
        # 显式操作身份允许合法重复；旧计划保留保守指纹策略。 / Explicit operation IDs permit repeats; legacy plans retain conservative fingerprints.
        prior = None if step.operation_id else self._find_duplicate(fingerprint, done)
        if prior is not None:
            return self._replay_prior(step, prior)

        # 确认权威来自 manifest；Registry 再次独立校验，避免执行器缺陷绕过确认。
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

        identity = OperationIdentity(
            task_id=ctx.task_id, operation_id=step.operation_id or uuid5(NAMESPACE_URL, f"{ctx.task_id}:{step.id}").hex,
            plan_revision=ctx.plan_revision,
        )
        result = await self._invoke_once(step, confirmed, operation=identity)
        result = await self._verify_outcome(step, result)
        if result.status == StepStatus.OK:
            result.fingerprint = fingerprint
        elif result.status == StepStatus.FAILED and (
            result.error == "step_timeout" or result.invocation_state in {"RUNNING", "OUTCOME_UNKNOWN"}
        ):
            # 超时也保留指纹，因为动作可能已发生；重规划不得盲目重复执行。
            # Timeout also fingerprints: the motion may have completed with the response lost;
            # a replan re-issuing the same action must not re-execute it blindly.
            result.fingerprint = fingerprint
        return result

    async def _invoke_once(
        self, step: Step, confirmed: bool, *, operation: OperationIdentity | None = None,
    ) -> StepResult:
        try:
            r: SkillResult = await self._registry.invoke(
                step.skill, dict(step.params), confirmed=confirmed, operation=operation,
            )
        except Exception as e:
            return StepResult(step_id=step.id, status=StepStatus.FAILED, error=f"{type(e).__name__}: {e}")
        metadata = {key: getattr(r, key) for key in ("operation_id", "attempt_id", "invocation_state", "stop_state")}
        if r.ok:
            return StepResult(step_id=step.id, status=StepStatus.OK, detail=r.detail, data=dict(r.data), **metadata)
        error = "step_timeout" if r.detail.startswith("timeout") else (r.detail or "skill_failed")
        return StepResult(
            step_id=step.id, status=StepStatus.FAILED, detail=r.detail, data=dict(r.data), error=error, **metadata,
        )

    # 效果验证。
    # -- verification ----------------------------------------------------------

    async def _verify_outcome(self, step: Step, result: StepResult) -> StepResult:
        """Only verifies steps that CLAIM success; expectations come solely from
        step.verification (manifest authority). UNSAT+retry re-executes once (never for
        require_confirm steps); UNSAT+report keeps OK but records data['_verify'] so the
        composer can be honest. Transport-uncertain failures get one state re-examination
        that may annotate (never flip) the result.

        成功调用按 manifest 期望验证；允许时对 UNSAT 重试一次，需确认动作永不重试。报告模式保留调用状态并附效果证据；传输不确定只补充状态证据，不翻转调用结果。
        """
        verification = step.verification or {}
        if not verification.get("mode"):
            return result
        if not _verify.enabled():
            result.data["_verify"] = {"verdict": _verify.UNKNOWN, "reason": "verification_disabled"}
            return result
        if result.status != StepStatus.OK:
            return await self._verify_uncertain(step, result)

        attempts = 0
        verdict = await self._evaluate(step, result)
        while (
            verdict == _verify.UNSAT
            and not self._registry.get(step.skill).resources
            and _verify.retry_allowed(verification, step.require_confirm, attempts)
        ):
            attempts += 1
            logger.info("step %s(%s): verify unsat, retrying (%d)", step.id, step.skill, attempts)
            retried = await self._invoke_once(step, confirmed=False)
            if retried.status != StepStatus.OK:
                # 重试本身失败，回到正常失败结果通道。
                return retried  # the retry itself failed: back to the normal failure channel
            result = retried
            verdict = await self._evaluate(step, result)

        if verdict in (_verify.UNSAT, _verify.UNKNOWN):
            result.data["_verify"] = {
                "verdict": verdict, "mode": verification.get("mode", ""), "attempts": attempts,
            }
            # 调用状态与效果证据分离；依赖调度和汇报均需查证据才能认定成功。
            # Keep the legacy call status separate from effect evidence. Dependencies
            # and the composer must both consult this evidence before claiming success.
        return result

    async def _verify_uncertain(self, step: Step, result: StepResult) -> StepResult:
        if result.status != StepStatus.FAILED or (result.error or "") not in _UNCERTAIN_ERRORS:
            return result
        if (step.verification or {}).get("mode") != _verify.MODE_STATE:
            return result
        verdict = await self._evaluate(step, result)
        if verdict == _verify.SAT:
            # 状态只能证明效果发生，不能证明本次调用成功；仅补充注释证据。
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
        # 验证异常返回未知，不使主执行链崩溃。
        except Exception as e:  # fail-open: reconciliation never takes down the main chain
            logger.warning("step %s verify errored (ignored): %s", step.id, e)
            return _verify.UNKNOWN

    # 副作用去重。
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
        'reused' into a fake success.

        同轮相同技能参数的成功或超时调用抑制重复；明确失败允许按恢复规则重试，不能回放为成功。
        """
        if not fingerprint or not _dedup_enabled():
            return None
        for prior in done.values():
            if getattr(prior, "fingerprint", "") != fingerprint:
                continue
            if prior.status == StepStatus.OK:
                return prior
            if prior.status == StepStatus.FAILED and (
                prior.error == "step_timeout" or prior.invocation_state in {"RUNNING", "OUTCOME_UNKNOWN"}
            ):
                return prior
        return None

    @staticmethod
    def _replay_prior(step: Step, prior: StepResult) -> StepResult:
        timed_out = prior.status == StepStatus.FAILED and (
            prior.error == "step_timeout" or prior.invocation_state in {"RUNNING", "OUTCOME_UNKNOWN"}
        )
        logger.info(
            "step %s(%s): duplicate suppressed (fingerprint=%s%s)",
            step.id, step.skill, prior.fingerprint, ", prior=timeout" if timed_out else "",
        )
        if timed_out:
            return StepResult(
                step_id=step.id, status=StepStatus.FAILED, error=prior.error,
                detail="prior identical action has uncertain outcome; NOT re-executed — reconcile before retrying",
                fingerprint=prior.fingerprint, data=dict(prior.data or {}),
                operation_id=prior.operation_id, attempt_id=prior.attempt_id,
                invocation_state=prior.invocation_state, stop_state=prior.stop_state,
            )
        return StepResult(
            step_id=step.id, status=StepStatus.OK, detail=prior.detail,
            data=dict(prior.data or {}), fingerprint=prior.fingerprint,
            operation_id=prior.operation_id, attempt_id=prior.attempt_id,
            invocation_state=prior.invocation_state, stop_state=prior.stop_state,
        )

    # 参数引用解析。
    # -- param refs ------------------------------------------------------------

    _REF_RE = re.compile(r"^\$\{([^{}]+)\}$")
    _ALIAS_RE = re.compile(r"^\$ref\.([A-Za-z_][A-Za-z0-9_]*)$")

    def _resolve_param_refs(self, step: Step, done: dict) -> None:
        """Three wire forms, most-explicit first (all ported): `${s1.data.x}` placeholders in
        params, the explicit param_refs map, then `$ref.alias` intra-step aliases.

        依次解析参数中的步骤占位符、显式 param_refs 和步骤内别名。
        """
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
            # 现有值优先，除非该值只是重复填写的引用路径。
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

    # DAG 辅助逻辑。
    # -- dag helpers -----------------------------------------------------------

    @staticmethod
    def _should_run(step: Step, done: dict) -> bool:
        for dep_id in step.depends_on:
            dep = done.get(dep_id)
            if not dep or not DagExecutor._effect_allows_dependents(dep):
                return False
        return True

    @staticmethod
    def _effect_allows_dependents(result: StepResult) -> bool:
        verdict = (result.data.get("_verify") or {}).get("verdict")
        return result.status == StepStatus.OK and verdict not in (_verify.UNSAT, _verify.UNKNOWN)

    @staticmethod
    def _mark_skipped(steps: list[Step], done: dict) -> None:
        for s in steps:
            if s.id in done:
                continue
            for dep_id in s.depends_on:
                dep = done.get(dep_id)
                if dep and not DagExecutor._effect_allows_dependents(dep):
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
                    # 未知依赖保持阻断，按拒绝放行处理。
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
