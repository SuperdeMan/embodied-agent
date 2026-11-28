# embodied-agent 架构设计

> 状态：v2 平台化目标已确立（2026-10-29），P0/P1 迁移中。本文档为当前架构权威；目标能力不等于已实现能力。v1 历史快照见 [architecture-v1.md](architecture-v1.md)。本轮范围与逐模块落差见 [platform-upgrade.md](platform-upgrade.md)。
> 关联文档：[路线图](roadmap.md) · [技术决策记录](decisions.md) · [car-agent 复用评估](reuse-from-car-agent.md)

## 1. 结论（TL;DR）

**v2 当前决策**：原仓演进为「跨机器人本体、跨算力后端的具身应用开发与运行平台」。采用现场执行、管理发布、数据学习三个平面；保留现有五层代码作为首个桌面参考实现。先补可信执行内核，再证明本体、策略、算力与交付的可替换性。v1 设计保存在历史快照，涉及已完成程度的判断以本文和执行计划为准。

### 1.1 三平面与唯一动作链

| 平面 | 职责 | 边界 |
|---|---|---|
| 现场执行 | Mission、Skill、Policy/Motion、Arbiter、Guard、Robot Adapter、Guardian | 动作许可由现场决定；云服务不持有设备直接写权限 |
| 管理发布 | Studio/API、设备能力与健康、任务提交、ReleaseBundle、维护态激活/回滚 | 首期模块化单体；不提前建设集群与技能市场 |
| 数据学习 | 原始增量记录、Episode 索引、训练导出、模型构建和独立评测 | 保留 ACT/LeRobot；候选模型先过评测，影子运行不持有控制权 |

受控链：任务 → 技能生命周期 → 动作候选 → 控制权/epoch/序号/时效检查 → Guard → 设备适配 → 观测与效果验证。遥操作和恢复逻辑必须走同一出口。Guardian 可撤销许可，但软件许可撤销不等于本体停止已观测。

### 1.2 执行语义与迁移次序

1. 参数在实际调用前执行类型、枚举、范围、有限值与未知字段校验；manifest/受信配置决定上限，LLM 不能放宽。
2. 超时或外部取消先撤销该调用的写许可；线程可能继续计算，但不应继续产生获准命令。停止请求、控制器接受和真实停止证据分开表达，缺反馈为 STOP_UNCONFIRMED。
3. 物理资源采用单写者与组合冲突组；锁定顺序与超时明确。仅 DAG 数据依赖不能证明物理互斥。
4. 调用结果、任务效果与停止结果独立。UNSAT 不释放依赖；UNKNOWN 不能当失败事实，也不能当成功。按 D018 接入持久 Operation/Attempt：先事务记录意图再调用，重启把未收敛操作改判未知，不自动重发。持久调用结果不等于任务效果证据。
5. 本地与 RPC 最终使用同一效果/取消契约；接口先改 proto。未知远端世界状态不得伪装为已验证成功。

八类契约的字段、验证位置与时钟/坐标规则见 [platform-upgrade.md §3](platform-upgrade.md#3-八类契约与冻结顺序)。新类型放 `src/embodied/contracts/`；依赖方向为适配器 → 核心契约，禁止反向引入厂商或重型学习依赖。

### 1.3 当前能力与目标的区别

当前参考实现覆盖单臂 MuJoCo、脚本/ACT、ColorBlob、Planner/DAG、RPC 活性链和训练转换。D018–D037 已覆盖仿真停止与效果对账、对象时效、受控维护、进程隔离及 v2 发行兼容。D041 进一步接入 v3 八表账本、持久一次性授权和经过 HTTP/mTLS 验证的单步恢复。SO-101 真机、可选 ROS/MoveIt/Nav2、视觉 VLA、第二算力与设备发行代理需按阶段验收。动态速度、加速度与力约束需要设备反馈支持，`max_force` 尚无真机验证。

录制已用于评测、collect 和 teleop。D035 为 `EpisodeWriter` 接入增量日志与硬杀恢复，D040 将本地 sim 和控制台交互接入录制和逐帧回读。后续现场录制仍需区分请求动作、获准动作、设备反馈和人工接管，实测设备断电时的数据损失。

三个平面在逻辑上拆分，不要求立即扩成微服务。先保留 agent/control/guardian 的已有边界；模型 worker 与 Recorder 隔离按实际预算引入。ROS 与厂商 SDK 使用各自锁定配置；真实伺服与设备停止由控制器/固件承担。

## 2. 产品边界

首发桌面分拣完整闭环；随后移动巡检验证能力域扩展，第二机械臂验证同技能跨本体。状态分接口预留、仿真验证、实机验证、持续回归。平台自持任务与操作语义、资源与证据、数据血缘、评测和发行兼容；集成已有仿真、ROS、控制器、学习框架与模型运行时，不自研芯片、OS、通用预训练或机器人硬件。

## 3. 当前参考实现

`cognition` 保留 PlanBuilder/DAG/有界重规划；`skills` 保留 Registry 与脚本/学习同 manifest；`control` 保留 MuJoCo 桌面单臂；`safety` 保留围栏、闩锁和活性链；`data_engine` 保留中间记录与 LeRobot 转换；`providers` 保留模型接入。未来能力通过兼容适配逐项接线，避免一次搬空目录。

## 4. 执行边界

### 4.1 交互

HRI 只提交任务、接收结果和明确确认。S2S 唯一工具仍为 escalate，模型不持有设备执行通道。

### 4.2 任务

调用返回不代表物理目标完成。任务、计划修订、逻辑操作和物理尝试分别标识。重规划必须保留执行上下文；重启须账本与设备对账，不自动从头重放。

### 4.3 技能

保留 `skill.<domain>.<action>` 名称；参数、资源、能力、前置/效果验证、超时/取消/恢复由受信 manifest 声明。目标生命周期 start/poll/request_cancel/observe_stop/collect_evidence。脚本与策略共用效果门，不能靠模型自评成功。

### 4.4 本体与时间

按 Manipulation/Navigation/Gripper/Inspection/Locomotion 分能力。RobotProfile 声明坐标、校准、控制模式、反馈和停止能力。时钟、仿真步进、IK/规划与设备写入分别适配；单臂字段不可强塞给底盘。

### 4.5 安全

SG-0 硬件、SG-1 固件/驱动、SG-2 确定性守护、SG-3 任务确认分别设防。SG-0–2 不依赖 AI 输出正确性。停止方法按本体确定；拒绝命令、接受停止请求和停止观测不得混同。现有白名单、每写夹限和软件 Guardian 不足以证明完整动态安全或可信身份。

### 4.6 数据

原始证据增量写出，经质量检查形成 Episode 索引，再导出训练格式。分别记录请求、获准、反馈和接管；录制/压缩/上传有界缓冲，不阻塞控制。训练和评测按场景/批次等分组隔离，裁判真值不能泄漏到策略输入。

### 4.7 可观测

D040 为本机交互 sim 与控制台提供显式开启的任务录制。每个会话轮次使用独立上下文，驱动反馈和获准动作进入有界后台写队列。过载或写盘故障单独报告，不重试任务。硬杀时内存队列里尚未送达写入器的样本也可能丢失，D035 的检查点界限仅适用于写入器已收到的采样。记录任务、操作和尝试标识即可从时间线定位证据；无独立任务裁判的录制保留未知成功状态，默认训练导出只接受有肯定标签的数据。

失败案例以现有评测 JSON 或 episode 目录为原始证据。D039 在结束时保存可检索的失败类别，历史文件可在查询时按相同规则分类。索引只指向原始证据；未录制的评测只有任务、随机种子、局号及步骤结果。无足够证据时保留未分类。

事件关联 task/operation/attempt/robot/release/trace；未知结果和缺失数据如实保留。Studio 先支持任务证据、设备控制权、episode 回放与版本状态。

## 5. 契约

八类契约字段与接线位置以[执行计划 §3](platform-upgrade.md#3-八类契约与冻结顺序)为迁移清单。纯契约不依赖适配器、ROS、GPU；跨进程 proto 先行，未知版本不静默接受。

### 5.21 D041 受控单步恢复与账本 v3

用户于 2026-11-28 批准[账本 v3 方案](controlled-recovery-v3.md)、隔离演练与演练通过后的常驻库迁移。v2 继续受支持，现有库显式迁移。操作保留稳定身份，每次设备尝试拥有独立编号、许可代次、结果、停止和效果证据。维护态按同一事务消费一次性授权并写入新尝试，随后沿用同一 Registry、Arbiter 与设备 Guard。

仅经已认证且显式持有 `retry_step` 的操作者可以申请单步重试。需要已结束或中断的任务、正确版本和请求、旧尝试已有停止实证、当前独立效果为 UNSAT、依赖满足及资源空闲。消费时重验，历史 UNKNOWN 保留。读报告、普通技能重发、启动进程和未列明作用域均不授权重试。回滚时必须证明 v3 没有迁移后的业务写入，否则保留 v3 历史。

### 5.20 D038 发行预检的证据要求

`release-check` 的激活和回滚结果用于维护人员的发行决策。两种操作都必须独占实际账本，核对 schema、运行中的操作与未确认停止；包中声明配置或模型文件时还须从实际发行目录核对文件摘要。对于声明的 ONNX 文件，预检须能读取其策略元数据。证据缺失或无法读取时，报告原因并令 `allowed` 与 `rollback_safe` 均为假。独立的模型语义比较仍可用于离线设计。预检只描述检查当时的状态，执行激活的设备代理须维护自己的独占窗口。

### 5.19 D037 发行兼容契约与 v2 账本降级恢复

为收口 P1 故障矩阵中 v2 账本新写入后的发行兼容与降级/恢复演练门禁，在零新增依赖、零 SQLite schema 变更、零 proto 变更前提下，新增纯契约 `src/embodied/contracts/release.py`（`ModelBundle`、`TargetEnvironment`、`ReleaseBundle`、`LedgerBackupManifest`、`ReleasePreflightReport`，版本 `embodied.dev/v1alpha1`）与运行时模块 `src/embodied/runtime/release.py`（CLI `embodied release-check`）：

- **模型物理语义兼容（不止校验张量形状）**：`verify_model_compatibility` 同时核对 `robot_id`、关节顺序 `joint_order`、单位、`action_semantics`、`normalization_mode`、推理后端支持集与模型文件 SHA-256（拒绝路径穿越与篡改），防止同维度模型在错误本体或错误关节序上激活。
- **维护态激活与回滚门禁**：`preflight_release` 在激活或回滚前强制校验独占账本 `.lock`、零 `RUNNING` 操作、零 `STOP_UNCONFIRMED` 尝试、资产/停止 profile 与配置摘要、驱动固件兼容列表、账本 schema 版本区间 `[min_ledger_schema, max_ledger_schema]` 以及显式 `verified_rollback_targets`。凡未经验证的组合或版本跨界降级（如 v1-only 运行时读取 v2 账本）一律 fail-closed 并固定 `rollback_safe=false`。
- **全七表认证备份与受损恢复**：`backup_ledger` 与 `restore_ledger_backup` 在独占锁下通过 SQLite 在线备份与全七表确定性 SHA-256 校验，确保受损账本仅在备份哈希、完整性与外键检查全通过后原子恢复，且支持在真实双镜像 Linux 容器间完成“新版本写入 v2 → 备份 → 受控回滚至前序兼容镜像 → 历史行逐行一致”演练。
- **诚实边界**：`hardware_verified` 固定为 `false`，不宣称真机固件 OTA 刷写或跨大版本 schema（v3→v2）降级兼容。详见 [发行兼容与降级恢复](release-compatibility.md)。

### 5.18 D036 跨机视觉与观察时钟契约

D019 远端世界状态仅发布即时 `sim_truth` 且未传递对象级感知年龄，D023 对象级时效仅覆盖本机同进程 `monotonic`。D036 在不破坏既有 gRPC proto wire、不改 SQLite schema v2、不增运行依赖的前提下，补齐跨机视觉/观察时钟契约与 RPC 对象级时效传播：

- **显式时钟域与跨机单调时钟禁令**：`src/embodied/contracts/observation.py` 新增纯契约 `ClockMetadata`、`RemoteObjectEvidence` 与 `ClockAdmissionPolicy`（严格校验、拒绝非有限数与未知字段）。`local_monotonic` 仅允许同 `host_id` 消费，跨主机一律拒收（`cross_host_monotonic_forbidden`）；非 `simulation` 时钟域必须显式附带 `ClockMetadata`。
- **偏斜/漂移/RTT 数学鲜度上界**：对 `wall_ntp` / `wall_ptp` 强制检查 `sync_status == "synchronized"`、最大偏斜与漂移预算、未来时间戳拒止、接收端本地墙钟阶跃跳变检测，并在存在 `publish_unix_s` 时执行 Cristian 中点往返偏斜反查（`|publish_unix_s - (rx_unix - rtt/2)| <= skew + rtt/2`），即使远端误报“已同步”也能检出超预算偏斜；对无共享墙钟的 `simulation` / `rpc_rtt_bounded` 则完全以生产端本地测得的对象年龄加接收端单调 RTT 与不确定度构造保守上界。
- **RPC 对象级感知传播与本地重基准**：`PerceivedSim` 显式实现 `read_reference_state()`，阻断此前穿透到底层仿真泄漏真值的缺口；`ControlService.GetWorldState` 在感知包装下输出 `source="perceived_sim"` 与逐对象 `RemoteObjectEvidence`（不传输生产端原始 `monotonic` 供对端比较）。接收端 `SnapshotAdmission` 在入站即淘汰已过期对象，并将新鲜对象的剩余 TTL 重基准到接收端自身单调时钟，使跨机效果验证器在遮挡、传输延迟或缓存过期时一致返回 `UNKNOWN` 并阻断依赖步骤。
- **诚实边界**：本批关闭软件层跨机视觉时钟契约与对象级 RPC 鲜度缺口，支持 `embodied serve-control --perception color` 跨进程实测；真实物理相机掉线与硬件 PTP/NTP 同步质量仍保留为实机门禁。详见 [跨机视觉与观察时钟契约](cross-host-observation-clock.md)。

### 5.17 D035 P1 可观测延迟、过期动作率与 Episode 崩溃完整性

为收口 P1 故障矩阵中的仿真延迟与数据完整性测量缺口，引入纯契约 `src/embodied/contracts/metrics.py`（`embodied.p1-metrics/v1`）与轻量运行时采样 `src/embodied/runtime/metrics.py`，无新增运行依赖、无 SQLite schema 变更、无外部 proto 变更：

- **停止延迟分项计量（绝不混同）**：分别记录 `guard_estop_latch_ms`（父进程 Guard 闩锁与调用撤权墙钟）、`urgent_estop_kill_ms`（对持 GIL 原生挂起物理子进程的强杀回收墙钟）、`stop_reconciliation_wall_ms` 与 `stop_reconciliation_sim_ms`（真实仿真保持与双侧接触观测墙钟/仿真毫秒时长），报告固定标注 `hardware_stop_measured=false`，严禁将仿真进程回收或测试执行时间冒充真机 SG-0 硬件急停延迟。
- **动作准入与过期拦截计数**：在 `ActionAuthority` 维护只增内存计数器（`actions_submitted`、`actions_accepted`、`actions_rejected`、`stale_actions_rejected`、`replay_rejected`、`epoch_mismatch_rejected`、`guard_denied`、`guard_clamped`），区分正常执行过期率与故障注入过期拦截率（要求过期动作 0 放行）。
- **操作与人工介入计数**：在 `SkillRegistry` 维护只读指标快照（总操作数、返回/失败/未知数、危险确认拒绝、取消、停止/效果对账成功与拒绝、物理控制重启次数及人工介入占比）。
- **Episode 增量日志与崩溃恢复**：`EpisodeWriter` 在保持 `v0` 布局（`steps.npz` + `meta.json` + `events.jsonl`）及 LeRobot 转换完全兼容的前提下，按 `checkpoint_every`（默认 10 步）将步骤增量追加并刷新至 `steps.journal.jsonl`，正常收尾时写入 `steps_sha256` 并在原子替换 `steps.npz` 与 `meta.json` 后清理临时日志。遭遇进程硬杀（`os._exit` / `SIGKILL`）时，提供 `audit_episode()` 与 `recover_episode()` 容忍截断尾行并重建完整 `steps.npz`（标 `aborted=true`、`recovered_from_crash=true`），将单局骤停损失严格收敛到 `< checkpoint_every` 步而非 100% 丢失。可重复基准脚本见 `scripts/p1_metrics_baseline.py`，详情见 [P1 延迟与数据完整性基线](p1-metrics-baseline.md)。

### 5.16 D034 物理控制进程隔离与受控停机回收

D026 将图像检测、ONNX 与状态副本渲染迁入独立计算子进程，但 MuJoCo 物理仿真、逆解与停止控制器仍运行在父进程内。D034 将仿真物理控制本体（`TabletopSim` 物理步进、逆解、驱动侧 Guard 与 `SimStopController`）迁入父进程监管的独立物理控制子进程（`IsolatedTabletopSim` + `ControlProcess` + `control_worker`），与 D026 渲染子进程保持分离：父进程只持有任务/操作账本、资源仲裁、`ActionAuthority`、HTTP/gRPC 控制面与镜像 `Guard`，不再在父进程内执行 MuJoCo 原生步进或逆解；渲染继续通过独立 `RenderBackend` 子进程消费状态副本。

本地控制管道契约先定义于 `proto/embodiedrpc/control_worker/v1/control_worker.proto`（无新增运行依赖，不修改现有 `control.proto`/`safety.proto`/`compute.proto` wire，不修改 SQLite schema v2）。管道采用网络序 4 字节长度前缀与严格 JSON 映射（拒绝重复字段、非有限数、超长帧与未知操作），单实例单在途请求、代次 `boot_id` 与递增 `request_id` 强绑定。子进程环境仅透传白名单运行变量，使用 `WindowsJob`（kill-on-close）或 Linux 独立进程组（`killpg(SIGKILL)` + `prctl(PR_SET_PDEATHSIG)`）限制自有进程树生命周期。

当物理控制子进程发生原生挂起、持有 GIL 阻塞、异常退出、`abort()`、错代次或超长帧时，监督器在有界时间内终止并实际回收子进程树与 I/O 线程，将实例置为 `FAILED`，**同步闩锁父进程 `Guard`**（D028 不变式优先于子进程与数据库），不自动重启、不重放旧请求；在途技能立即解除阻塞并落账 `OUTCOME_UNKNOWN / STOP_UNCONFIRMED` 且保持资源隔离，父进程 HTTP/gRPC 查询、取消与 `Halt` 保持响应。当 `Halt` 或 `estop` 到达而子进程健康时，仅同步闩锁子进程 `Guard` 并保留活体供 `reconcile_stop`；若子进程在短暂宽限窗口内无响应（原生挂起），`urgent_estop` 立即强杀并回收子进程树。仅在无活跃调用/观察/在途停止且确认子进程已回收、资产摘要未变时，受信维护入口 `restart_physical_control()` 才可拉起新代次（轮换 `controller_boot_id` 与 `scene_epoch`），同时保留父 `Guard` 闩锁与账本资源隔离。本批关闭仿真物理控制原生挂起/崩溃对父进程的连带冻结与杀进程风险，不冒充真实硬件急停（SG-0）或 OS 权限沙箱。详见 [物理控制进程隔离](physical-control-isolation.md)。

### 5.15 D033 控制与监督 RPC 的 mTLS 传输认证与角色隔离

`ControlService` 与 `SafetyService` 支持显式双向 TLS（mTLS）与只读 `RpcAccessPolicy` 角色策略。策略固定根 CA 证书指纹 `ca_cert_sha256` 及每个允许对端的叶证书 SHA-256 指纹、唯一名称、角色（`agent` / `guardian` / `operator`）、可选 `spiffe_id` 及操作者显式作用域（`read_only`、`cancel`、`reconcile_effect`、`reconcile_stop`、`halt`、`reset_guard`）。

监听任何非回环地址或 CLI 连接非回环远端时，必须成对提供 mTLS 证书与策略，缺任一项均在启动阶段拒绝（fail-closed）；回环地址保留既有未认证本地开发参考模式，不宣称跨机身份认证。服务端直接从 BoringSSL `auth_context()` 提取传输类型、叶证书指纹与 URI SAN，不信任客户端自报 gRPC metadata 或仅靠 `confirmed=true` 标志。

方法级角色矩阵在内核写死，策略不能越权放大：`InvokeSkill` 与任务日志写接口仅限 `agent`；`SupervisorLink` 仅限 `guardian`；`Heartbeat` 仅限 `agent`；`ReconcileEffect`、`ReconcileStop` 与 `Reset` 仅限显式拥有对应作用域的 `operator`，`agent` 与 `guardian` 均不能清除急停闩锁。未认证或越权调用在触达任何状态或账本之前以 `UNAUTHENTICATED` / `PERMISSION_DENIED` 中止。已认证 RPC 维护操作（`ReconcileStop`、`ReconcileEffect`）在执行前写入 `rpc_operator_action_started` 至既有 schema v2 `operation_events`（写失败拒绝执行），结束后写 `rpc_operator_action_finished`；`Halt` 与 `CancelOperation` 继续遵守 D028，撤销与驱动闩锁绝不因数据库写失败被阻断。详见 [RPC mTLS 与角色授权](rpc-mtls-authorization.md)。

### 5.14 D032 受控停止授权与作用域隔离

`OperatorPolicy` 显式声明允许的操作作用域集合 `scopes`；未填写时默认仅为 `["reconcile_effect"]`，保证已有策略文件在应用升级后不会隐式获得物理停止对账权限。只有策略显式包含 `reconcile_stop` 时，受控 HTTP 入口才受理该作用域；未授权作用域直接返回拒绝且不写审计。

`reconcile_stop` 与 `reconcile_effect` 共享同一代理身份、精确 Origin/Host 校验、30 秒一次性内存 challenge 与 `operation_events` 审计链路，不改 schema，不进入模型技能目录，不授权任务续跑、技能重试或 Guard 复位。准备与确认阶段复用 `SkillRegistry` 的停止准入校验：仅允许带有效任务计划和受跟踪工作线程的 `OUTCOME_UNKNOWN / STOP_UNCONFIRMED` 操作，且须满足无活跃调用、工作线程已退出、`StopProfile` 摘要匹配、无在途观测或冲突围栏且隔离归属一致；已完成停止或普通操作拒绝签发。

与效果重查不同，停止对账允许跨重启（`context_matches=false` 且任务为 `INTERRUPTED`）以及在 Guard 已急停闩锁（`guard.halted=true`）下执行，但要求 Guard 限位绑定未篡改，且从签发到消费期间 `review_state`（含停止配置摘要、围栏、在途状态、场景代次、Guard 代次和账本写入计数）无任何变化。确认后先烧毁凭据并持久化 `operator_authorization_consumed`，再调用现有 `SimStopController` 推进仿真、连续采样并先写 `stop_observed` 再解除隔离，最后追加 `operator_authorization_finished`。原调用保持 `OUTCOME_UNKNOWN`，Guard 闩锁永不被清除。详见 [受控停止授权](stop-authorization.md)。

### 5.13 D031 本体反馈不触发语义采样

关节插值、夹爪插值及夹取框架方向仅消费现有 `read_control_feedback` 的关节/夹爪/末端反馈；旧参考夹具没有该接口时保留 read 回退。不得为读取这些字段重复触发视觉渲染/检测，消耗已求解物体目标的 TTL。物体目标的初始、分段与最终观测继续显式读取感知；夹爪闭合后、抬升前显式更新附着信念，保留原生命周期行为。

不修改抓取坐标、运动时长、关节安全约束、观察 TTL 或动作有效期，不刷新旧观察时间戳。真实迟到的语义目标仍拒绝。D030 验收发现的 color 29/30（semantic_target_stale）及更早的无详情失败均保留，不能把消除冗余采样当成所有历史失败已解释。

### 5.12 D030 代理身份与一次性效果重查授权

控制台可显式加载只读 OperatorPolicy：精确的代理 peer IP、HTTPS origin 和主体 allowlist。只接受该 socket peer 上由 Tailscale Serve 覆盖的单一身份头，拒绝直连、重复/变体头、未列入主体及 tagged/Funnel 无用户身份请求；不信任 X-Forwarded-For 或客户端自报主体。宿主及策略发布者属于信任边界，不声称抵抗已攻陷宿主。普通本机/RPC 参考入口不因此获得跨机认证。

新增 HTTP 操作者接口仅授权 `reconcile_effect`，不在模型技能目录，不提供停止、复位、动作重试或续跑。准备阶段核对 D029 审查摘要、目标 task/operation/attempt/请求、manifest、控制上下文和停止/资源条件，生成 30 秒、同主体同 boot 的有界内存 challenge，并在既有 operation_events 追加签发审计。消费需相同受信身份、精确 challenge 和严格布尔确认；先烧毁内存凭据并提交消费意图，再调用已有只读效果重查。结果单独审计；写失败、过期、变更、重复或重启均拒绝，不补发、不重放。

审计使用现有操作/尝试外键与新事件类别，不改 schema，不写 task_events；旧读取器忽略这些类别。主体用稳定摘要及策略摘要关联私有 allowlist，审计不记录密钥。D029 仍不签发任务执行权限；本批仅打通受信 HTTP 操作者到有审计的效果重查。部署须证明代理头覆盖、网络唯一后端与精确 peer 绑定，见 [操作者授权](operator-authorization.md)。

### 5.11 D029 恢复审查快照与权限拒绝契约

恢复评估仅返回审查材料，不签发执行权限。快照绑定任务/计划版本、operation/attempt/请求摘要、账本 boot、控制器/场景/profile 和变更指纹；摘要用于比对，不是签名或 bearer token。当前没有受信操作者身份适配，明确返回 `execution_authorized=false`、`authenticated_subject=null`。未来授权必须绑定明确动作范围、期限、一次性消费和持久审计，不能把 `confirmed=true` 当身份认证。

只读评估在每次异步证据检查后及返回前复核账本变更、控制器上下文、manifest 与运行中状态。任意变化令整份评估失效，历史记录仍可展示，当前效果统一 UNKNOWN；不自动重试以拼接新旧结果。较早检查的证据在返回前再次计算年龄，RPC 再加往返时间，过期不保留 SAT 资格。此一致性只覆盖本接收端可观察的身份/日志与资格，不声称原子物理世界快照。

沿用 TaskReply JSON，不增加 proto 字段、schema、任务事件类型或执行接口。HTTP/RPC 查询都不写账本、不停止、不重放，详见 [恢复审查与权限边界](recovery-review.md)。

### 5.10 D028 存储故障下的取消与监督闩锁

取消权限必须独立于持久化可用性。批量取消先同步撤销所有活跃调用许可并发出协程取消，再逐项尝试持久化；一项读写失败不得跳过后续调用。单项取消同样先撤权，账本读写失败时保留资源隔离并向调用者报告错误。账本中的 RUNNING 不因写失败被伪造为已取消；重启按原规则恢复为 UNKNOWN。

控制服务先置自身闩锁并调用驱动 Guard 急停，再执行批量取消。批量持久化错误按操作 ID/异常类型记录，不让监督 watchdog 因数据库故障退出；Halt 的成功仍只代表闩锁与撤权请求，不代表物理停止已观测。无新 schema、无自动解除隔离或续跑。组合故障验收见 [P1 故障矩阵](p1-fault-matrix.md)。

### 5.9 D027 任务日志与只读恢复评估

任务恢复先保存计划版本、步骤意图与执行器回执，再关联既有 operation/attempt、效果和停止证据。新增 `task_plans` 与 `task_events`，schema v2 只追加两表，不重写原四张业务表。v1 不隐式迁移；升级须停止写者、备份、单事务 DDL，并验证历史行不变。迁移方案见 [task-recovery.md](task-recovery.md)。

记录计划的验证/确认权威由接收端 manifest 重建，不接受客户端自行放宽；步骤意图必须在调用前落账。执行器回执属于历史汇报，不替代执行端操作与效果证据。重启后未结束任务标记为中断，旧任务的新操作拒绝；旧 operation_id 仍只回放历史，不执行。

任务查询与恢复评估均只读：展示完整计划、未派发/只有意图/已接收/已返回/未知步骤，区分历史效果与当前证据资格。缺少计划的旧任务明确标为 legacy；无效或损坏记录不能推出可恢复。当前 `automatic_resume_allowed` 固定为 false，不提供自动续跑、多 attempt 或自动确认停止。

### 5.8 D026 纯计算子进程与受控退出

图像检测、ONNX 推理和状态副本渲染使用独立 Python 子进程。父进程保留真实 MuJoCo 状态、Guard、调用许可、资源仲裁与 SQLite；IPC 只传有大小/形状约束的数值数据，不传本体对象、执行句柄或 Python callable。渲染在父进程锁内复制状态，然后释放锁等待子进程，不能把子进程卡顿变成原生状态锁长期占用。

协议先定义于 `proto/embodiedrpc/compute/v1/compute.proto`，本地管道采用其 JSON 字段映射与长度前缀帧，不依赖远端 gRPC 或新增运行依赖。每个实例最多一个请求，响应核对版本、进程代次、请求 ID、数值和形状。超时、撤销、崩溃或坏协议使实例进入 FAILED：回收所属进程及 I/O 工作，不重发请求，不自动重启。只有确认回收后，受信调用者才可显式开启新代次。

Windows 使用无窗口子进程和 kill-on-close Job Object；Linux 使用独立进程组及直接 worker 的父线程死亡信号。启动从专用持久 I/O 线程发起，避免父线程死亡信号绑定到短命观察线程。优雅退出、强制结束和实际回收分别检查；无法回收时不创建替代进程。D025 的等待/迟到结果保护仍保留。这是故障与生命周期隔离，不是文件/网络权限沙箱，详见 [isolated-compute.md](isolated-compute.md)。

### 5.7 D025 有界只读观察

每个 Registry 使用一个无排队的只读观察通道，绑定、效果采样/资格检查、效果重查和应用世界快照经该通道执行。默认等待预算 1 秒，独立于原有对象/效果 2 秒有效期；超时或取消只结束等待，不把仍运行的线程当成已退出。迟到返回不提交绑定或证据，SQLite 始终由运行时所属线程访问。

观察线程不继承调用许可，并带显式只读上下文，驱动拒绝其中的 write/step/reset。旧观察未结束时拒绝新观察、新动作、场景重置、停止对账及运行时关闭；操作查询、取消与监督 RPC 保持响应。已完成调用的效果超时只记录 UNKNOWN，不改写已知调用结果，也不自动重放。绑定阶段的意图先入账，再异步绑定并追加受信绑定事件，随后才允许 handler 执行。

控制台帧请求同样限制为一个在途工作，超时/并发请求返回不可用，不排无限队列。保留同步 Python 兼容接口，但异步应用与 RPC 使用异步路径。该机制约束等待和并发数量，不强杀 Python/native 线程，不声称进程崩溃隔离或硬实时保证；永久阻塞需受控进程重启。细节见 [bounded-observation.md](bounded-observation.md)。

### 5.6 D024 持久效果证据与受信重查

资源持有期间，绑定本体的 Registry 对声明式空间效果采样；调用结果与效果证据分别落账。效果事件绑定操作/尝试、请求摘要、manifest 验证声明、观察 boot、场景、控制 profile、采样时刻与来源；证据不修改 RETURNED/FAILED/OUTCOME_UNKNOWN 或停止状态。沿用五表的追加事件，无 schema 迁移。

后继调度消费接收端已持久化且仍新鲜的证据，不允许客户端提交 SAT。旧操作重放只给历史调用结果，不能重新观察后来场景为旧动作补成功。显式效果重查不执行动作，要求本体空闲、旧工作已退出、未知操作已完成停止对账、原场景/profile/控制器仍匹配且无更新的冲突操作。重启或 reset 后只保留历史证据，不能认领为当前效果。

RPC 提供效果资格检查与只读重查，客户端用 RTT 加服务端证据年龄作保守时效界限，不比较跨机 monotonic。落账失败或证据过期/不兼容返回 UNKNOWN，不能释放依赖；无 manifest 效果元数据的历史记录不猜测补齐。细节见 [effect-reconciliation.md](effect-reconciliation.md)。

### 5.5 D023 对象级感知时效

感知包装器为每个对象保留真实检测的采集时刻、相机、序号、场景、标定摘要与本进程时钟身份；重读本体状态或运动学附着推算不得刷新该检测时间。视觉位姿与短期运动学推算分别标注来源，均受仿真时间和墙钟双重 TTL 约束。相机错误、持续缺测、时间倒退、场景或标定变化不能让旧位姿无限存活。

失效对象从新观察的可用对象表移除；世界状态空间谓词将其视为 UNKNOWN，规划上下文不发布陈旧坐标，策略在输入和每次输出前检查原输入证据。脚本在各个对象目标求解点检查新鲜状态；已获准的短段关节插值仍受 D022 控制反馈和命令期限约束，不把对象 TTL 宣称为动态避障或全轨迹再规划。

本批针对本地感知链，不修改仅发布即时 sim_truth 的 D019 RPC 契约。裁判仍独立读取仿真真值；不因感知缺失回退到真值。详细预算与验收见 [object-freshness.md](object-freshness.md)。

### 5.4 D022 统一动作准入与遥操作

平台运行时绑定仿真实例后，脚本、策略和遥操作的目标都在驱动前转换成 ActionEnvelope。包络包含本体、控制器 boot、场景 epoch、profile/资产标识、运行时 boot、操作/尝试、资源租约、序号、来源、关节顺序、模式、单位、控制反馈引用和本机单调时钟期限。字段本身不授予控制权；接收端还须持有本实例绑定运行时发出的活跃调用许可。

同一仿真实例只能绑定一个运行时，遥操作复用它的账本与仲裁，每条手动命令独立记录操作，不注册为模型技能。命令与来源声明不可信，未知版本、错本体/代次/顺序/单位、重复序号、伪造反馈引用和过期命令在 Guard 前拒绝；Guard 继续独立检查当前本体状态和数值约束。

运动原语使用新鲜的本体反馈；策略动作块保留推理输入的观察引用，不能在推理结束后重新打时间戳。D022 只证明控制反馈/输入快照的时效，不证明每个感知对象最后检测时间；本地对象级 TTL 由 D023 接入，见 §5.5。

未绑定运行时的独立物理夹具保留 reference 模式，不代表平台执行入口。绑定后裸 write 不再可用；无调用上下文的 step 仅可在无活跃/隔离资源时推进空闲模拟时钟。reset 是空闲时的仿真维护操作，不能绕过资源隔离或改变活跃动作的场景。D021 专用停止路径继续在维护围栏内执行。

本批不新增跨机原始关节 RPC，不把本机 monotonic 直接传到其他时钟域；现有高层 RPC 在控制进程内生成并验证包络。无数据库 DDL 或迁移。细节见 [action-admission.md](action-admission.md)。

### 5.3 D021 停止观测与受信解除隔离

停止对账是独立维护操作，不是 LLM 技能。调用者必须明确确认 operation_id 与 attempt_id；服务端使用已绑定的停止控制器，拒收客户端提供的“已停止”布尔值或证据。当前仅实现 MuJoCo 参考控制器，不为真机出具证明。

脚本和学习技能的阻塞工作改用可跟踪执行器：取消后保留实际线程完成信息，线程未退出不进入停止对账；未声明受跟踪执行的旧操作保持隔离。账本准入事件保存停止 profile 摘要和跟踪能力；不新增表、不改变 schema 版本、不迁移云端数据。

仿真维护操作先阻断普通写入/步进/reset，再在关节限位内保持机械臂当前位置；夹爪检测到双侧接触时保持已接受的闭合目标，否则保持当前位置，实际推进物理仿真并连续观测所有受控关节（含夹爪）速度。不能直接清零 qvel 伪造停止。证据绑定本体、资产/profile、操作/尝试、原 boot 与当前观察 boot、资源 epoch、场景代次、一次性凭据、样本与时间窗；非有限值、过期、错代次、错身份或后台工作未结束均不释放。

提交时在控制器维护锁内先事务记录停止证据和 `MOTION_STOPPED_OBSERVED`，再释放对应资源并撤销维护围栏。失败保持隔离。原调用与任务效果仍是 OUTCOME_UNKNOWN，旧 operation_id 不重放，只有新的显式操作可占用已释放资源。停止对账不清除 Guard 闩锁。重复对账只返回历史结果，不再次停止可能已被新任务使用的设备。

重启仅恢复合法、完整且身份匹配的已提交停止证据；没有准入元数据的历史未知操作不被自动放行。模型、stop profile 或本体变化时拒绝套用旧证据。具体实现与边界见 [stop-reconciliation.md](stop-reconciliation.md)。

### 5.2 D019 远端世界状态的新鲜度

远端规划与效果验证通过 `GetWorldState` 请求有版本的 ObservationEnvelope，包含本体、服务 boot_id、场景 epoch、序号、仿真时钟、坐标/单位、标定版本、来源、关节和对象/区域。服务端当前仅提供仿真观测，不能把 sim_truth 声称为真实感知。客户端用本机单调时钟测量往返耗时，并加上服务端读快照耗时作为保守新鲜度界限，不比较跨机 monotonic 值。

未知版本、非有限值、时钟倒退而 epoch 未变、乱序、过期、断连或缺快照都返回 UNKNOWN。场景 reset 改变 epoch；服务重启改变 boot_id；已被替代的 epoch 不得被迟到回复恢复。规划上下文与 verifier 支持异步读取，远端验证不再固定退化 UNKNOWN。状态请求的新鲜度不等于所有对象检测的新鲜度；当前控制服务的 reference profile 只发布实时仿真真值。

### 5.1 D018 操作账本与资源准入

新增纯类型 `contracts/operation.py` 和 `runtime/operations.py`，区别于既有内存任务账本。操作身份包含 task_id、operation_id、plan_revision，尝试 ID、boot_id 与资源 epoch 由执行端生成。`RETURNED` 仅表示 handler 成功返回；`FAILED` 为明确调用失败；`OUTCOME_UNKNOWN` 默认隔离资源；只有 D021 停止证据提交后才可释放，仍不接受自动新尝试。效果仍由独立 verifier 判定。

SQLite 五表保存任务、操作、尝试、事件和 schema 版本；结果与事件在一个事务写入，未知 schema 拒绝打开。一个数据库只允许一个执行进程持有（OS 文件锁），避免第二进程把仍活着的运行误判为重启遗留。部署必须让同一本体使用同一账本；不同文件不能提供跨进程硬件互斥保证。

manifest 声明组合资源；当前桌面 home/pick/place 共占 arm/gripper/workspace。进程内仲裁原子获取全组，冲突立即拒绝，无部分获取与死锁。许可在真实写路径同时检查期限和当前资源所有权；未确认停止的资源隔离状态从持久未知尝试恢复。当前不提供盲目清除隔离或自动恢复接口，Guard Reset 不清除账本未知结果。

控制服务、交互 sim 与 console 默认使用 `outputs/runtime/operations.sqlite3`；chat/评测/采集参考路径使用内存账本，不据此宣称跨重启恢复。RPC 增加操作身份、查询和取消请求，取消受理与停止确认分离。旧 command_id 可作为操作 ID；无身份的旧远端请求拒绝。新的明确操作 ID 可以表达同参数的合法重复动作；旧规划仍保留保守指纹去重过渡策略。

## 6. 部署与交付

现有本地参考与 agent/control/guardian 进程拓扑保留；逐步统一验证和取消语义。管理服务提交任务和版本，现场保有最终执行权。模型/Recorder 按预算隔离；伺服环交由控制器。ModelBundle 校验动作语义与目标后端；ReleaseBundle 绑定版本和评测，维护态激活，兼容检查与演练后才允许回滚。

## 7. 选型

Python/uv 与 MuJoCo/ACT/ONNX 参考链保留。ROS、MoveIt、Nav2 走可选适配，发行与厂商 SDK 分别锁版本，不默认混装。SQLite 操作账本已获授权并按 D018 接入。MCAP 等流容器、第二算力与模型选择在对应阶段以可获取资源及实际任务证据决定，不把报告调研当依赖安装授权。

## 8. 演进与重估

### 8.1 不变约束

模型可替换、安全独立、仿真先行、数据和评测可追溯。原仓渐进迁移；共享内核仍须两个实际运行场景共同验证。

### 8.2 失效触发器

第二本体暴露动作/停止差异、第二后端暴露算子/资源限制、模型或数据格式不兼容、现场时延或数据损失超预算时重审契约与配置。季度复查生态，厂商公告不能直接改变已锁定部署配置；重大依赖升级先 ADR 再实现。

## 9. 验收

先保留原 30 任务对照；再扩大分工况仿真和真机故障注入。契约、并发、取消、任务效果、数据恢复、模型和发行分别验收。纯单元测试绿不等于真机或跨算力通过；各项证据带 SHA、任务/模型/依赖摘要与运行条件。

## 10. 目录与迁移

当前保留 `src/embodied/{hri,cognition,skills,control,safety,data_engine,providers,runtime}`，以及 console/proto/assets/sim/eval/scripts。按消费路径增加 `contracts`、`perception`、`deployment` 子模块；adapters/profiles/deploy 顶层目录在相应实施批次建立并记录 ADR。权重/数据/录制仅存指针与摘要；生成的 RPC stubs 在 gen，不进 Git。

D020 增加 `deploy/` 存放云端仿真参考镜像与受限 Compose 配置；部署规划与资源边界见 [cloud-deployment.md](cloud-deployment.md)。首版只承载仿真控制台，真实控制和停止监督仍属现场职责。

## 设计来源

[用户指定完整方案](https://chatgpt.com/s/t_6ae35e1306fc8191859b25bd76e427ca) · [v1 架构历史](architecture-v1.md) · [决策记录](decisions.md) · [平台执行计划](platform-upgrade.md)。本次采用报告的工程边界与验收设计，未把外部产品调研重新验证为本仓支持矩阵。
