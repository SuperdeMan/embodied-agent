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

当前可复用的是单臂 MuJoCo、脚本/ACT、ColorBlob 感知、Planner/DAG、RPC 活性链和训练转换代码。D018 新增本地持久调用账本与资源隔离，仍未具备完整物理停止对账。SO-101 真机、ROS/MoveIt/Nav2、视觉 VLA、NPU 及 ReleaseBundle 激活/回滚仍属待实现目标。`source` 白名单不是可信身份认证，每次写增量夹限不是速度/加速度/力约束的完整证明；`max_force` 未获设备反馈支持前不能标为已强制。

录制现状：sim 由参数开启，collect 接入录制，console 尚未完整接入；当前 NPZ 结束写出，不能承诺异常终止恢复。目标为原始证据、Episode 索引、训练交换三层，分别保存请求动作、获准动作、设备反馈和人工接管。

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

事件关联 task/operation/attempt/robot/release/trace；未知结果和缺失数据如实保留。Studio 先支持任务证据、设备控制权、episode 回放与版本状态。

## 5. 契约

八类契约字段与接线位置以[执行计划 §3](platform-upgrade.md#3-八类契约与冻结顺序)为迁移清单。纯契约不依赖适配器、ROS、GPU；跨进程 proto 先行，未知版本不静默接受。

### 5.1 D018 操作账本与资源准入

新增纯类型 `contracts/operation.py` 和 `runtime/operations.py`，区别于既有内存任务账本。操作身份包含 task_id、operation_id、plan_revision，尝试 ID、boot_id 与资源 epoch 由执行端生成。`RETURNED` 仅表示 handler 成功返回；`FAILED` 为明确调用失败；`OUTCOME_UNKNOWN` 不释放资源、不接受自动新尝试。效果仍由独立 verifier 判定。

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

## 设计来源

[用户指定完整方案](https://chatgpt.com/s/t_6ae35e1306fc8191859b25bd76e427ca) · [v1 架构历史](architecture-v1.md) · [决策记录](decisions.md) · [平台执行计划](platform-upgrade.md)。本次采用报告的工程边界与验收设计，未把外部产品调研重新验证为本仓支持矩阵。
