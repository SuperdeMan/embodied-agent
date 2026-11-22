"""Pure contracts for P1 runtime latency, stale-action, intervention, and episode integrity metrics (D035).

P1 运行时延迟、过期动作拦截、人工介入与 Episode 完整性基线纯契约（D035）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MetricsModel(BaseModel):
    """Strict immutable base model rejecting unknown fields and non-finite floats.

    严格不可变指标基类，拒绝多余字段与非有限浮点数。
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


class LatencySummary(MetricsModel):
    """Nearest-rank latency distribution summary in milliseconds.

    毫秒级延迟分布汇总（最近秩分位数）。
    """

    count: int = Field(ge=0)
    min_ms: float | None = Field(default=None, ge=0.0)
    p50_ms: float | None = Field(default=None, ge=0.0)
    p95_ms: float | None = Field(default=None, ge=0.0)
    max_ms: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _consistent_bounds(self):
        vals = (self.min_ms, self.p50_ms, self.p95_ms, self.max_ms)
        if self.count == 0:
            if any(v is not None for v in vals):
                raise ValueError("empty latency summary must leave quantiles null")
            return self
        if any(v is None for v in vals):
            raise ValueError("non-empty latency summary requires all quantiles")
        assert self.min_ms is not None and self.p50_ms is not None and self.p95_ms is not None and self.max_ms is not None
        if not (self.min_ms <= self.p50_ms <= self.p95_ms <= self.max_ms):
            raise ValueError("latency quantiles must be non-decreasing")
        return self


class ActionMetricsSnapshot(MetricsModel):
    """Receiver-side action admission and stale-action rejection counters.

    接收端动作准入与过期动作拒绝计数快照。
    """

    actions_submitted: int = Field(default=0, ge=0)
    actions_accepted: int = Field(default=0, ge=0)
    actions_rejected: int = Field(default=0, ge=0)
    stale_actions_rejected: int = Field(default=0, ge=0)
    replay_rejected: int = Field(default=0, ge=0)
    epoch_mismatch_rejected: int = Field(default=0, ge=0)
    guard_denied: int = Field(default=0, ge=0)
    guard_clamped: int = Field(default=0, ge=0)
    stale_action_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class OperationMetricsSnapshot(MetricsModel):
    """Registry-level operation outcome and operator intervention counters.

    SkillRegistry 操作终态与人工介入计数快照。
    """

    operations_started: int = Field(default=0, ge=0)
    operations_returned: int = Field(default=0, ge=0)
    operations_failed: int = Field(default=0, ge=0)
    operations_unknown: int = Field(default=0, ge=0)
    confirmations_denied: int = Field(default=0, ge=0)
    cancellations_requested: int = Field(default=0, ge=0)
    stop_reconciliations_succeeded: int = Field(default=0, ge=0)
    stop_reconciliations_rejected: int = Field(default=0, ge=0)
    effect_reconciliations_succeeded: int = Field(default=0, ge=0)
    effect_reconciliations_rejected: int = Field(default=0, ge=0)
    physical_control_restarts: int = Field(default=0, ge=0)
    manual_intervention_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class EpisodeAuditResult(MetricsModel):
    """Structured integrity audit verdict for one recorded episode directory.

    单个录制 episode 目录的结构化完整性审计结果。
    """

    episode_dir: str = Field(min_length=1)
    intact: bool
    needs_recovery: bool
    recovered_from_crash: bool = False
    recorded_steps: int = Field(ge=0)
    steps_sha256_verified: bool
    events_count: int = Field(ge=0)
    issues: tuple[str, ...] = ()


class EpisodeIntegritySummary(MetricsModel):
    """Measured episode integrity across normal runs and abrupt hard-kill recovery.

    正常录制与真实进程硬杀恢复场景下的 Episode 数据完整性与损失率汇总。
    """

    checkpoint_every: int = Field(ge=1)
    normal_episodes_total: int = Field(ge=0)
    normal_episodes_intact: int = Field(ge=0)
    normal_data_loss_rate: float = Field(ge=0.0, le=1.0)
    abrupt_kill_expected_steps: int = Field(ge=1)
    abrupt_kill_recovered_steps: int = Field(ge=0)
    abrupt_kill_lost_steps: int = Field(ge=0)
    abrupt_kill_loss_rate: float = Field(ge=0.0, le=1.0)
    abrupt_kill_bounded: bool
    torn_tail_tolerated: bool
    tamper_detected: bool


class StaleActionInjectionSummary(MetricsModel):
    """Verification that injected stale, expired, and replayed commands are 100% blocked.

    验证注入的过期命令、陈旧反馈、陈旧语义目标与重放序号 100% 被拦截且 0 条被执行。
    """

    injected_stale_attempts: int = Field(ge=1)
    intercepted_stale_attempts: int = Field(ge=0)
    stale_applied_count: int = Field(ge=0)
    stale_interception_rate: float = Field(ge=0.0, le=1.0)
    injected_replay_attempts: int = Field(ge=1)
    intercepted_replay_attempts: int = Field(ge=0)


class P1MetricsReport(MetricsModel):
    """Commit-bound P1 latency, stale-action, intervention, and integrity baseline report.

    绑定提交的 P1 仿真停止延迟、过期动作拦截、人工介入与 Episode 完整性基线报告。
    """

    schema_id: Literal["embodied.p1-metrics/v1"] = "embodied.p1-metrics/v1"
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    working_tree_clean: bool
    platform: str = Field(min_length=1)
    environment: Literal["simulation_isolated_process"] = "simulation_isolated_process"
    hardware_stop_measured: Literal[False] = False
    p1_complete: Literal[False] = False
    guard_estop_latch_ms: LatencySummary
    urgent_estop_kill_ms: LatencySummary
    stop_reconciliation_wall_ms: LatencySummary
    stop_reconciliation_sim_ms: LatencySummary
    normal_run_actions: ActionMetricsSnapshot
    normal_run_operations: OperationMetricsSnapshot
    stale_action_injection: StaleActionInjectionSummary
    episode_integrity: EpisodeIntegritySummary
    boundaries: tuple[str, ...]
