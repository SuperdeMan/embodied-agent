# RPC mTLS 传输认证与角色隔离 · D033

> 2026-11-22，D033 为 `ControlService` 与 `SafetyService` 补齐双向 TLS（mTLS）证书指纹固定、硬编码角色方法矩阵和 schema v2 兼容的维护审计。无新增依赖、无 proto wire 变更、无数据库迁移。

## 信任模型与网络门禁

- **强制非回环保护**：`serve-control` 与 `serve-guardian` 监听任何非回环地址（非 `127.0.0.1` / `::1` / `localhost`）时，必须同时提供 `--tls-ca`、`--tls-cert`、`--tls-key` 与 `--rpc-auth-policy`，否则启动直接失败。CLI 客户端（`sim --remote`、`operation`、`task`）连接非回环目标时同样强制要求 mTLS 证书参数。
- **成对配置**：服务端只要提供 TLS 或策略之一，就必须两者齐备；启动时校验加载的 CA PEM 指纹与 `policy.ca_cert_sha256` 完全一致，避免错挂 CA。
- **回环兼容**：本机回环地址在不提供策略和证书时保留既有参考模式，便于本地开发和既有回归测试；该模式不宣称具备认证身份。
- **证书指纹固定**：每个允许对端按叶证书 DER SHA-256 指纹（`cert_sha256`）精确匹配，可选叠加精确 `spiffe_id` URI SAN 校验；完全忽略客户端自报的 gRPC metadata。

策略示例（真实私钥与证书永不进 Git）：

```json
{
  "api_version": "embodied.dev/v1alpha1",
  "ca_cert_sha256": "<64-hex-ca-der-sha256>",
  "peers": [
    {"name": "agent-core", "role": "agent", "cert_sha256": "<64-hex>", "spiffe_id": "spiffe://embodied.local/role/agent"},
    {"name": "safety-guardian", "role": "guardian", "cert_sha256": "<64-hex>", "spiffe_id": "spiffe://embodied.local/role/guardian"},
    {"name": "ops-stop", "role": "operator", "cert_sha256": "<64-hex>", "scopes": ["read_only", "reconcile_stop", "halt"]}
  ]
}
```

## 角色与方法权限矩阵

角色权限在内核写死，策略无法放宽；未列出的 RPC 方法默认拒绝：

| 服务 | 方法 | `agent` | `guardian` | `operator` 所需作用域 |
|---|---|---|---|---|
| `ControlService` | `InvokeSkill`、`RecordTaskPlan`、`RecordTaskStep`、`FinishTask` | 允许 | 拒绝 | 拒绝 |
| `ControlService` | `SupervisorLink` | 拒绝 | 允许 | 拒绝 |
| `ControlService` | `Halt` | 拒绝 | 允许 | `halt` |
| `ControlService` | `Reset` | 拒绝 | 拒绝 | `reset_guard` |
| `ControlService` | `ReconcileEffect` | 拒绝 | 拒绝 | `reconcile_effect` |
| `ControlService` | `ReconcileStop` | 拒绝 | 拒绝 | `reconcile_stop` |
| `ControlService` | `CancelOperation` | 允许 | 拒绝 | `cancel` |
| `ControlService` | `ListSkills`、`GetOperation`、`GetTask`、`ReconcileTask`、`ListTasks`、`CheckEffect`、`GetWorldState`、`StreamState` | 允许 | 拒绝 | 任意已授权作用域 |
| `SafetyService` | `Heartbeat` | 允许 | 拒绝 | 拒绝 |
| `SafetyService` | `ValidateCommand` | 允许 | 拒绝 | 任意已授权作用域 |
| `SafetyService` | `EStop` | 拒绝 | 拒绝 | `halt` |

所有校验在触及任何服务状态前执行：未授权调用 `SupervisorLink` 或 `Heartbeat` 不会篡改连接标志或触发伪造断连停机；未授权调用 `InvokeSkill` 不会触发无监督闩锁。

## 维护审计与 D028 不变式

- 在 mTLS 模式下，`ReconcileStop` 与 `ReconcileEffect` 先执行只读目标准入预检，再在现有 `operation_events` 追加 `rpc_operator_action_started`（绑定 `cert_sha256`、`subject_hash`、`policy_digest`、`scope`、`task_id`、`request_digest`、`ledger_boot_id`）。若开始审计写库失败，直接返回 `rpc_audit_unavailable`，**绝不触发物理停止观测或效果采样**；完成后追加 `rpc_operator_action_finished`（若结束审计写库失败亦明确报错，不伪装完整审计）。
- `Halt` 与 `CancelOperation` 严格遵守 D028：驱动 Guard 闩锁与调用许可撤销永远先于任何数据库写入，存储故障不得阻断急停或取消。
- `Reset` 仅允许显式拥有 `reset_guard` 作用域的操作者证书，记录结构化安全日志与内存审计，且**永不清除**账本中的未知操作隔离。
- `ReconcileTask` 经 mTLS 返回的 `RecoveryReview` 继续保持 `execution_authorized=false`、`authenticated_subject=null`、`automatic_resume_allowed=false`，不把传输认证混同为自动续跑授权。

## 边界

- 不提供在线吊销（CRL/OCSP）或自动证书轮换；吊销需更新只读策略文件并重启服务。
- 物理停止证明仍限定于仿真参考控制器；跨机传感器时钟同步、父进程原生控制隔离、多 attempt 恢复与真机急停继续保留为独立门禁。
