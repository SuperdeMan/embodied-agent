# 受控停止授权与作用域隔离 · D032

> 2026-11-22，D032 在 D021 停止观测与 D030 代理身份基础上扩展受控 HTTP 的一次性 `reconcile_stop` 授权。不新增数据库表，不清除 Guard 闩锁，不授权任务续跑、技能重试或 Guard 复位。

## 显式作用域与最小权限

`OperatorPolicy` 新增可选字段 `scopes`，合法取值仅限 `reconcile_effect` 与 `reconcile_stop`（至少 1 项，禁止未知项）；**缺省为 `["reconcile_effect"]`**。未声明 `scopes` 的已有策略文件升级后保持仅效果重查权限，提交 `reconcile_stop` 直接返回 `403 operator_scope_not_allowed` 且不写审计。

启用双作用域的策略示例（真实主体不进 Git）：

```json
{
  "api_version": "embodied.dev/v1alpha1",
  "trusted_proxy": "172.30.0.1",
  "origin": "https://example.tailnet.ts.net:8449",
  "subjects": ["operator@example.test"],
  "scopes": ["reconcile_effect", "reconcile_stop"]
}
```

`policy_digest` 纳入排序后的 `subjects` 与 `scopes`，`GET /api/operator/identity` 返回实际允许的 `scopes`，`task_execution_authorized` 恒为 `false`。

## 与效果重查的关键差异

| 维度 | `reconcile_effect`（D030） | `reconcile_stop`（D032） |
|---|---|---|
| 目标状态 | 非 `RUNNING`；若为 `OUTCOME_UNKNOWN` 须先 `MOTION_STOPPED_OBSERVED` | 必须为 `OUTCOME_UNKNOWN` 且尚未 `MOTION_STOPPED_OBSERVED` |
| 跨重启（`context_matches=false`） | 拒绝（旧动作不能借重启后场景算成功） | 允许 `INTERRUPTED` 任务在匹配 `StopProfile` 下重新观测停止 |
| Guard 急停闩锁（`guard.halted=true`） | 拒绝（`guard_unavailable`） | 允许观测停止，但**必须保持 `guard.halted=true`**，绝不复位 Guard |
| Guard 限位一致性 | 要求 `guard.limits is controller._limits` | 同样要求 `guard.limits is controller._limits` |
| 签发到消费间变更 | 任意 `review_state` 变更均失效 | 同样要求一致，并新增 `stop_profile_digest`、`_stop_fence` 与在途停止状态进指纹 |
| 历史任务 / 损坏任务 | 拒绝 `LEGACY` / `CORRUPT` | 同样拒绝 `LEGACY` / `CORRUPT` 与 `IDENTITY_MISMATCH` |

## 执行与审计顺序

1. 操作者经 `GET /api/tasks/{task_id}?assess=1` 获取一致审查快照及 `state_digest`。
2. `POST /api/operator/authorizations` 提交 `scope="reconcile_stop"`、目标 `task_id/operation_id/attempt_id` 与 `state_digest`。服务端校验代理 peer、Host/Origin、单一 Serve 用户头、策略作用域、审查一致性与 `SkillRegistry` 停止准入条件（无在途观测/渲染、无活跃调用、同 boot 工作线程已退出、`tracked_workers=true`、`StopProfile` 摘要匹配、隔离归属匹配、无冲突停止围栏），落账 `operator_authorization_issued` 后返回 30 秒一次性 `challenge_id`。
3. `POST /api/operator/confirmations` 仅接受 `challenge_id` 和严格布尔 `confirmed`。先烧毁内存凭据，再复核主体、期限、`review_state` 和停止准入，提交 `operator_authorization_consumed` 后再调用 `registry.reconcile_stop(..., confirmed=True, allow_already_observed=False)`。
4. 停止控制器在维护围栏内推进仿真、连续观测受控关节速度（持物时同步核对双侧接触力），先事务写入 `stop_reconciliation_requested` 与 `stop_observed` 并更新 `attempts.stop_state='MOTION_STOPPED_OBSERVED'`，随后解除该操作的资源隔离与维护围栏，最后追加 `operator_authorization_finished`。

任意审计写失败、过期、快照变化、重复确认或重启均拒绝，不补发、不重放。停止观测期间，控制台 `/api/scene.jpg` 返回 `503 stop_reconciliation_in_progress`，避免预览线程阻塞在仿真状态锁上。原操作保持 `OUTCOME_UNKNOWN`，旧 `operation_id` 永不重放；解除隔离后只有新的显式操作（在 Guard 未闩锁时）可申请资源。

## 边界

- 沿用 schema v2 的 `operation_events`，无数据库迁移，不写 `task_events`。
- 不修改普通 HRI WebSocket 或参考 gRPC 的认证边界；不新增 Guard 复位、任务续跑或多 attempt 接口。
- 物理停止证明仍限定于 MuJoCo `mujoco-hold-current-v1` 参考控制器，不能转借真实机械臂。
