# 持久操作与资源准入 · D018

> 2026-10-30；功能提交 `c6392de`。本页描述当前实现，不代表完整 P1 或真机停止能力。

> 后续 D019 已补远端仿真快照时效，见 [remote-world-state.md](remote-world-state.md)；本页 D018 验证数字仅属于原提交。

## 已接入的执行链

PlannerEngine 每轮生成 task_id；步骤获得运行时 operation_id，RPC 保留相同身份。Registry 先校验确认、参数与 manifest，再根据本体、技能、参数和 manifest 计算请求摘要。首次调用在 SQLite 事务中保存意图、attempt_id、boot_id、资源 epoch 和事件，事务成功后才启动 handler。

同 operation_id、同摘要的重发返回原调用结果或当前未知状态，不再执行；同 ID 不同任务、参数、manifest 或本体拒绝。显式不同 operation_id 可表达同参数的合法重复。模型不能通过 submit_plan 设置操作权限；旧计划保留原参数指纹去重作为过渡。

`RETURNED` 仅表示 handler 返回成功，仍须 verifier 确认任务效果。关闭 verifier 时已声明的效果变为 UNKNOWN，不放行依赖。需确认或占用物理资源的动作不自动做效果验证重试；只读旧技能的既有重试路径保留。

## 数据库与重启

默认位置 `outputs/runtime/operations.sqlite3`，新建五表：schema_version、tasks、operations、attempts、operation_events。schema 版本为 1；没有隐式迁移，未知版本拒绝打开。本次只创建新库，没有改写既有用户数据库。

同一文件仅一个执行进程持有 OS 文件锁；事务采用 SQLite FULL 同步。意图先落盘，结果和事件同事务提交。进程重启后 RUNNING 改判 OUTCOME_UNKNOWN，不自动新建 attempt；曾返回的结果保持原 attempt_id。事务只保证本地记录一致，不保证现实动作恰好一次。

任务表当前保存身份、计划修订与未知标记，不是完整持久 Mission DAG；效果证据仍由现有 verifier 提供。首版每个逻辑操作只允许一次物理尝试，尚未开放人工对账后的 retry/compensate 接口。

## 资源与取消

桌面 home/pick/place 的受信 manifest 声明 arm、gripper、workspace 组合资源，学习实现共用相同 manifest。仲裁原子占用全组，冲突立即返回 REJECTED，不部分获取、不排队死锁。写许可同时检查期限、撤销标记及资源所有权；旧 epoch 的 owner 不能释放新租约。

超时、显式取消、执行异常或无法持久化结果时保留未知；相关资源隔离并在重启后恢复隔离。`CancelOperation` 撤销许可并取消技能子任务，RPC 服务协程继续发送终态。收到取消回复只说明处理请求，停止字段仍是 STOP_UNCONFIRMED。`Halt` 同时撤销进行中的调用；`Reset` 只重置 Guard，不清除账本未知和资源隔离。

当前没有可靠停止观测与解除隔离流程，故无法自动恢复被隔离的物理资源。不能通过删除账本或换文件路径宣称问题已恢复。不同账本文件之间也不构成同一硬件的互斥保障。

## 入口与查询

交互 sim、console、serve-control 默认持久化，可用 `--ledger` 指定路径；chat、评测和采集参考路径使用内存账本。直接遥操作尚未迁入统一动作包络，不能宣称所有执行出口已收敛。远程入口仍是现有本机 RPC，可信设备身份与跨主机认证未在本批完成。

```bash
uv run --group sim --group rpc embodied serve-control --require-supervisor
uv run --group rpc embodied operation --remote 127.0.0.1:8391 --id OPERATION_ID
uv run --group rpc embodied operation --remote 127.0.0.1:8391 --id OPERATION_ID --cancel
```

操作 ID、尝试 ID、调用状态和停止状态会随 StepResult、RPC 终态及控制台步骤事件传递。查询不会重放动作；断连或没有终态的客户端返回未知，并保留 operation_id 供后续查询。

## 验证

- 本批代码工作树完整 `uv run ruff check .` 与 `uv run pytest -q -o addopts=`：493 passed、0 skipped、4 warnings；随后只有文档更新，源码作为 `c6392de` 提交。警告仍为既有 ONNX 导出弃用。
- RPC/账本/仲裁针对性测试 25 passed；另包含本体变更拒绝回放、显式确认布尔值、禁用验证器不能放行及显式操作 ID 重复意图的全套回归。
- 真正子进程非正常退出后重新打开 SQLite：未完成操作变为未知，旧 attempt 不重放。真实本机 gRPC 重启/重连验证已完成结果保留；取消和 Halt 后的查询保留 STOP_UNCONFIRMED。
- `c6392de` 干净提交，原 tabletop_pick_place@v1、脚本+真值+离线规划器：30/30。该结果不为感知、学习模型、真机或 NPU 提供新版本背书。
- 默认交互 sim 启停冒烟已创建新本地账本，并确认五表与版本；未发出机器人动作。nightly YAML 仅双语注释补齐，解析配置与前版本一致。

机器可读结果见 [operation-runtime-20261030.json](../eval/operation-runtime-20261030.json)。下一步为带时效的远端世界状态、停止证据/受信对账与解除隔离，然后统一遥操作和完整 ActionEnvelope；不将本批判为 P1 全面完成。
