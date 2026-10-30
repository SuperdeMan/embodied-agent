# 远端世界状态 · D019

> 2026-10-30，功能提交 `fde3247`。本批实现仿真参考状态，不宣称真机感知或完整 P1。

## 当前链路

`serve-control` 将 MuJoCo 的受信 `read_reference_state` 接到 GetWorldState；无读取器时明确返回 unavailable。场景代次和观测在同一个驱动锁内读取，reset 改变 epoch。写入、步进、渲染与相机读取同样使用状态锁，避免状态被混合采样。

ObservationEnvelope 记录 robot_id、boot_id、epoch、sequence、simulation 时钟、捕获耗时、world 坐标、米/弧度单位、标定版本标识、sim_truth 来源，以及关节、末端、对象和区域。未知字段/版本、非有限数、错误形状、单位或来源均拒绝。`mujoco_reference_v1` 是参考配置标识，不是实机手眼标定证明。

客户端每次请求测本机单调时钟往返时间，与服务端捕获耗时相加后限定在 1 秒内；不直接比较两台机器的 monotonic。拒绝过期、同 boot 乱序、同 epoch 时间倒退、原代次被替代后又返回，以及更早请求的迟到响应。失败返回 None，对 verifier 表现为 UNKNOWN，不复用缓存充当新状态。

PlannerEngine 上下文读取和 verifier 兼容同步/异步函数。远端 CLI 绑定同一个 world_snapshot 接口；新快照能证明效果时放行依赖，无法获取时保持阻断。此前远端永远缺对象/区域导致 UNKNOWN 的缺口已补齐。

## 限定

- 当前只提供即时仿真真值；感知对象自身的最后观测时间/遮挡 TTL 尚未接入此接口，不能把状态请求成功当成所有对象都重新被看见。
- 已有本机 RPC 没有新增跨主机设备认证，本批包络描述来源但不是签名证明。
- 状态时效与停止证据是两件事。取消后的 STOP_UNCONFIRMED、资源隔离、未完成动作禁止自动重放保持不变；解除隔离仍需受信停止观测及对账。
- 世界状态读取并非高频控制 ActionEnvelope 的替代；遥操作出口统一仍待完成。

## 验证

本批完整测试 511 passed、4 条既有 ONNX 导出警告；新增覆盖非法包络、时效边界、旧代次/迟到响应、真实本机 RPC 效果放行与缺态阻断、reset 代次变化、控制台同源 WebSocket 和渲染线程清理。`fde3247` 干净提交原仿真任务为 30/30，脚本+真值+离线规划器，记录见评测账本。

本地测试与原始仿真结果保存在 [remote-world-20261030.json](../eval/remote-world-20261030.json)；完整测试运行于随后提交为 `fde3247` 的应用工作树，云端验收脚本在另一证据链中验证。

云端候选与部署边界单独见 [cloud-deployment.md](cloud-deployment.md)，不把本地结果转借为云端或真机结果。
