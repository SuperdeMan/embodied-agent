# 物理控制进程隔离与受控停机回收 · D034

> 2026-11-22，D034 将 MuJoCo 物理仿真、逆解、驱动侧 `Guard` 与 `SimStopController` 迁入独立受监管子进程（`IsolatedTabletopSim` + `ControlProcess`），与父进程控制面及 D026 离屏渲染子进程分离。无新增运行依赖、无 SQLite schema 迁移、不改变既有外部 gRPC wire。

## 进程拓扑与职责边界

| 进程 | 持有对象 | 边界 |
|---|---|---|
| 父控制面进程（`console` / `serve-control` / `sim`） | `SkillRegistry`、SQLite v2 账本、`ResourceArbiter`、`ActionAuthority`、镜像 `Guard`、HTTP/gRPC 服务 | 不执行 MuJoCo 物理步进或逆解；物理子进程故障时保持 API、查询、取消与 `Halt` 响应 |
| 物理控制子进程（`control_worker`） | 无头 `TabletopSim(isolated_render=False)`、驱动侧 `Guard`、`kinematics.solve_ik`、`SimStopController` | 无 OpenGL/Mesa 上下文，无 API key/token 环境变量，无 SQLite 句柄；仅受理白名单控制操作 |
| 纯计算渲染子进程（D026 `render_worker`） | 独立 `MjData` 副本与 Mesa 离屏渲染 / JPEG 编码 | 与物理控制进程完全分离；渲染崩溃或卡顿不牵连物理状态 |

## 本地管道协议与生命周期

- 协议源为 `proto/embodiedrpc/control_worker/v1/control_worker.proto`（`embodied.control_worker.v1.ControlWorkerEnvelope`），本地管道采用 4 字节网络序长度前缀 + 严格 JSON 映射（上限 16 MiB，拒绝重复键、NaN/Inf、缺省/多余字段与非白名单操作）。
- 每个实例单在途请求、强制校验 `boot_id` 与严格递增 `request_id`；Windows 使用无窗口子进程 + kill-on-close `WindowsJob`，Linux 使用独立进程组（`start_new_session=True` + `killpg(SIGKILL)`）与 `prctl(PR_SET_PDEATHSIG, SIGKILL)`。
- 状态机：`NEW` → `READY` → `FAILED`（仅在空闲、已回收且资产摘要未变时经 `restart_physical_control()` 显式拉起新代次）；`CLOSED` 为终态，不可重新激活。

## 故障遏制、急停与显式恢复

- **原生挂起 / 崩溃 / 坏协议**：超时、`os._exit`、`abort()`、错代次或超长帧均立即终止并实际回收物理子进程树，置 `FAILED`、同步闩锁父进程 `Guard`（`physical_control_fault:*`），不自动重启、不重放请求；在途技能解除阻塞退出（`permit.drained=True`）并记为 `OUTCOME_UNKNOWN / STOP_UNCONFIRMED`，保持资源隔离。
- **正常取消 vs 挂起强杀**：
  - 健康子进程遇到 `CancelOperation` 或 `Halt` 时，短宽限窗口（默认 `0.15s`）内完成极短在途步进并同步子进程 `Guard` 闩锁，保留活体供 `reconcile_stop` 在同一物理场景中观测停止。
  - 若物理子进程原生挂起超过宽限窗口，`urgent_estop` 或取消超时立即强杀并回收子进程树，避免父进程、`ControlService.Halt` 或 `AppContext.close()` 被锁死。
- **恢复审查与健康探针**：子进程处于非 `READY` 状态时，`/healthz` 返回 `503 physical_control_unavailable`，`RecoveryReview` 将 `control_process_state` 纳入 `state_digest` 并在 `blockers` 加入 `physical_control_unavailable`；`reconcile_stop` 与 `Guard.reset()` 均 fail-closed。
- **显式重启**：`registry.restart_physical_control()` / `sim.restart_physical_control()` 仅允许在无活跃调用/观察/在途停止、全部停止上下文已排空且旧进程已实际回收时启动新代次；新代次轮换 `controller_boot_id` 与 `scene_epoch`（令旧动作包络与旧效果上下文全部失效），**保留父 `Guard` 闩锁与账本资源隔离**，永不静默恢复执行。

## 边界

- 终止仿真物理子进程证明的是**仿真控制进程已终止并回收**，不等于真实硬件独立急停（SG-0）已验收。
- 环境变量白名单与自有进程树回收提供故障和生命周期隔离，不构成操作系统级文件/网络权限沙箱。
- 真机硬件急停、跨机传感器时钟同步、在线证书吊销（CRL/OCSP）与 schema v3 多 attempt 恢复仍为独立未关闭门禁。

## 本批验收

固定提交 `cb05aee`（干净工作树、无新增运行依赖、无 SQLite schema 变更、无外部 gRPC wire 变更；新增本地协议 `proto/embodiedrpc/control_worker/v1/control_worker.proto`）：

- **本机验证**：`ruff check .` 通过；完整 5 组测试 **897 passed / 0 skipped**（含 19 项新增物理控制进程与 mTLS 集成测试）；RPC 套件 **46 passed / 0 skipped**；[P1 参考故障矩阵](p1-fault-matrix.md) 扩展至 **12 组 260 项全部通过**（`p1_complete: false`）；在默认启用物理控制隔离的 `embodied sim` 下，`tabletop_pick_place@v1` 3×30 回归保持 `scripted_truth` 30/30、`isolated_pick_v1_truth` 29/30（既有第 14 局 `horizon reached`）、`isolated_color` 30/30。
- **Linux Docker 候选与 19 项真栈实测**：候选镜像 `embodied-agent:cb05aee...`（`sha256:f4c5a494...`）通过无网络隔离控制台验收（`control_isolation: "process"`、抓放、并行预览、6 项计算故障回收）及 `scripts/control_smoke.py`（三进程 PID 互异、数值一致、`urgent_estop` 0.130s 强杀 Linux 持 GIL 挂起子进程、6 项物理控制故障全部回收且零重放）；独立测试镜像 `embodied-phys-rpc-test:cb05aeeb3ada`（`sha256:cf445026...`）在 Linux 容器内全量通过全部 19 项 D034 + BoringSSL mTLS 集成测试。
- **云端私网控制台升级**：已授权由 `90135f0` 升级至 `cb05aee`（回执 `20261122T042559Z`，HTTPS 8449），原七表历史行（19 tasks / 34 operations）逐行保留，实测容器内 PID 1（`console`）/ PID 8（`control_worker`）/ PID 25（`render_worker`）三进程分离、抓放、重启持久性与真实 Serve HTTPS 身份/双作用域校验全部通过。完整证据见 [physical-control-isolation-20261122.json](../eval/physical-control-isolation-20261122.json) 与 [cloud-physical-isolation-upgrade-20261122.json](../eval/cloud-physical-isolation-upgrade-20261122.json)。
