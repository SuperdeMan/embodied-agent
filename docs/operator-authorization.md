# 操作者身份与效果重查授权 · D030

> 2026-11-04，实施中。仅新增效果重查的受信 HTTP 路径，不授权任务执行、停止、复位或重试。

## 身份与信任边界

`console --operator-policy /path/policy.json` 显式加载策略；未提供时操作者接口返回 503。策略固定精确代理 IP、HTTPS origin 和登录主体 allowlist。身份只从匹配 socket peer 的单一 `Tailscale-User-Login` 头读取，禁止重复、下划线变体、自报管理员和 `X-Forwarded-For` 替代认证。写请求还必须携带精确 Origin，拒绝跨站与非 JSON 请求。

依据 [Tailscale Serve identity headers](https://tailscale.com/docs/features/tailscale-serve#identity-headers)（2026-11-04 核对）：Serve 覆盖客户端同名头；tagged 设备及 Funnel 不提供用户身份。后端不得被非受信来源直连。当前部署使用只有本项目容器的 internal Docker 网络、无宿主端口，可信 peer 是已核对的宿主桥接网关；策略文件以只读 bind mount 挂载。**宿主及策略发布者是信任边界**：此方案不抵抗能在宿主发请求或修改策略的攻击者，也不宣称普通参考 RPC 已有操作者认证。

策略示例（真实名单不进 Git）：

```json
{
  "api_version": "embodied.dev/v1alpha1",
  "trusted_proxy": "172.30.0.1",
  "origin": "https://example.tailnet.ts.net:8449",
  "subjects": ["operator@example.test"]
}
```

网关必须从本项目实际网络发现，不照抄示例。部署 overlay 为 [compose.operator.yaml](../deploy/compose.operator.yaml)；只有已验证的策略绝对路径通过本次命令环境传入，不修改 `.env` 或 Tailnet 访问策略。公开仓库仅记录主体/策略摘要。

## HTTP 工作流

1. `GET /api/operator/identity` 返回认证主体摘要、issuer、policy 摘要和唯一 scope `reconcile_effect`，响应禁止缓存。
2. 用既有任务评估取得 D029 `state_digest`；`POST /api/operator/authorizations` 提交 scope、task_id、operation_id、attempt_id、state_digest。服务器重新检查审查一致性、真实操作绑定、manifest、控制器/场景/Guard、资源与停止条件，返回 30 秒以内的 challenge。
3. `POST /api/operator/confirmations` 仅接受 challenge_id 和严格布尔 confirmed。相同主体、同 policy/boot、未过期且上下文未变才可消费；false 记录拒绝，不进行重查。凭据不是可转交给其他主体的 bearer token。

消费先烧毁内存 challenge，并在 SQLite 提交消费意图，再调用既有 `reconcile_effect_async`。观察资格和当前状态仍由原效果管线校验；请求不会调用技能、推进动作或把 UNKNOWN 改为成功。重启丢弃未消费 challenge；重复、过期、任何其他账本写入或 Guard 生命周期变化均拒绝。准备和确认过程不自动重试。

## 审计与失败语义

沿用 schema v2 的 `operation_events`，追加 `operator_authorization_issued/consumed/finished`，绑定已有 operation/attempt 外键、主体摘要、policy 摘要、请求摘要、账本 boot、scope、审查摘要与 TTL。旧读取器忽略这些新类别，不写 task_events，不新增表或迁移。

签发审计失败不返回 challenge；消费审计失败不重查，凭据仍失效；结束审计失败时返回错误，即使效果记录已经保存，也不能重复消费。只有 consumed 而无 finished 的记录必须人工对账，不能推断未发生或自动重做。每实例最多 128 个待确认项、每主体最多 16 个；审计按操作限制容量。无身份或非法请求不写审计，避免把未经认证的数据当操作者记录。

D029 `execution_authorized=false` 继续表示未授权任务执行。D030 的范围是一次效果重查，不能用它绕过危险技能确认、独立 Guard、停止证据或任务 UNKNOWN 隔离。真实操作者会话撤销、跨主机控制身份、物理停止授权、多 attempt 与自动续跑仍未验收。
