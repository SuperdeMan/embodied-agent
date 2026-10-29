# 平台升级执行计划

> 2026-10-29；来源：[用户指定方案](https://chatgpt.com/s/t_6ae35e1306fc8191859b25bd76e427ca)及附件《具身智能平台架构方案_2026-10-29.docx》v1.0（18 章）。正文与附件已逐章核对。外部厂商调查作为方案背景；本次不把报告中的 SDK、硬件或性能描述当作本仓已验证能力。
> 基线：`24d780a96a5ff5e48d8330b88c06e30e77390449`；保留原仓、技能 ID、任务集和历史评测，不清空重写。架构权威见 [architecture.md](architecture.md)，排期见 [roadmap.md](roadmap.md)。

## 1. 目标与边界

升级为跨机器人本体、跨算力后端的具身应用开发与运行平台。首个产品是可运行、可验证、可回放、可交付的桌面分拣闭环。按现场执行、管理发布、数据学习三个平面组织能力。

第一轮集中 P0/P1。保留 ACT、MuJoCo、脚本技能和感知链作为迁移回归资产。暂不启动厂商 NPU 空壳适配、全新 Studio、真实机械臂驱动或公共内核抽库。现有机械臂 HAL 不是已经验证的跨本体接口；Python 调度不是硬实时或功能安全认证。

## 2. 现状核对与迁移矩阵

| 模块 | 当前实现与缺口 | 迁移方向 | 优先级 |
|---|---|---|---|
| `skills/manifest.py`、`registry.py` | 仅查未知/缺失参数；未执行类型、枚举、数值校验 | 边界严格校验；manifest 为权威 | P1.1 |
| `safety/guard.py` | 有围栏、白名单、每写增量夹限；NaN 可透传；观测/配置缺校验 | 有限值、形状和配置校验；不把 source 字符串当认证 | P1.1 |
| `registry.py`、脚本/策略 handler | `wait_for(to_thread(...))` 不终止线程 | 撤销调用写许可，合作取消，停止证据独立建模 | P1.2 |
| `executor.py` | 同层 gather 没有物理互斥；UNSAT 仍 OK；UNKNOWN 可释放后继 | 资源仲裁；调用、效果和停止分别表达 | P1.3 |
| `engine.py` | 创建 done 但没有传入下一轮 executor | 先修轮内上下文；后续显式 operation/attempt 账本 | P1.3/P1.4 |
| `runtime/ledger.py` | 内存字典，非持久物理操作账本 | SQLite 操作与事件账本，崩溃后先对账 | P1.4 |
| `control/service.py`、`skills/remote.py` | 确认门与活性链已有；RPC 缺对象世界状态，取消/回执不足 | proto 先行，统一本地/RPC 效果和停止语义 | P1.5 |
| `control/hal.py`、motions、PolicyRunner | 单臂字段，依赖 timestep/solve_reach | 能力、时钟、运动规划、设备执行分别适配 | P2/P3 |
| `cognition/perception.py`、world_state | 有感知替换与独立真值裁判；缺通用时间/坐标/来源包络 | ObservationEnvelope；新鲜度、校准、epoch 失效 | P1.5/P2 |
| `data_engine/recorder.py`、CLI、console | sim 录制可选；console 未全接；NPZ 结束写出 | 原始增量证据、episode 索引、训练导出三层 | P2 |
| `providers/policy.py`、训练导出 | state + pose ACT / ONNX；不是视觉 VLA | 保留基线，新增 ModelBundle 语义兼容检查 | P2/P4 |
| console、部署 | 初版交互与场景流；无完整发行与回滚 | 证据、设备、回放、发行四类工作流 | P2/P4 |

报告三项复现必须变成回归：错误类型/枚举不得进入 handler；NaN/Inf 不得进入驱动；超时或外部取消之后不得再接受该调用的新写入。禁止将“已撤销写许可”描述为“物理运动已停止”。

## 3. 八类契约与冻结顺序

所有新契约使用 `embodied.dev/v1alpha1`，未知版本拒绝；字段变更与 wire 兼容分别评审。纯类型放 `src/embodied/contracts/`，不得依赖 ROS、MuJoCo、torch 或厂商 SDK。先冻结文档语义，再按实际消费路径增加类型与连接，避免只有 dataclass 没有执行门禁。

| 契约 | 必需语义 | 检查位置/迁移顺序 |
|---|---|---|
| RobotProfile | robot_id、能力、运动组/冲突组、单位/坐标、校准、反馈、控制模式、停止策略 | 接入预检；先 MuJoCo 参考配置，真机不虚报 stop feedback |
| SkillSpec | 保留 `skill.*` ID；类型/枚举/范围、所需能力/资源、前后置、超时、取消、恢复、版本 | Registry 准入；现有 SkillManifest 渐进兼容 |
| TaskPlan / Operation | task_id、plan_revision、operation_id、attempt_id、请求摘要、授权、资源、证据 | Mission 与控制接收端；逻辑操作跨重试稳定，物理尝试重新编号 |
| ObservationEnvelope | robot/sensor、clock_domain、采集/接收时刻、frame、校准、sequence、质量、来源 | 感知/策略入口；使用采集时刻的坐标变换 |
| ActionEnvelope | 操作/尝试、本体/资源、lease epoch、sequence、观察引用、有效期、模式、单位、关节顺序 | 唯一写出口；旧 epoch、迟到/重复、坐标/单位不符拒绝 |
| ModelBundle | 源/权重摘要、输入输出及预后处理、归一化、动作语义、profile、后端/精度、许可证、评测 | 构建/部署；相同 shape 不等于物理兼容 |
| ReleaseBundle | runtime/skills/models/profile/config/schema、驱动固件兼容、摘要、评测与恢复证据 | 维护态激活；未验证兼容组合不得标自动回滚 |
| EpisodeManifest | 任务/操作/设备/版本、时序/数据摘要、动作来源、质量和结果证据 | 记录/回放/训练；区分请求、获准、反馈及接管 |

主状态目标：CREATED → VALIDATED → QUEUED → RESERVED → RUNNING → VERIFYING → SUCCEEDED / FAILED / CANCELLED / OUTCOME_UNKNOWN。停止子状态：CANCEL_REQUESTED → CONTROLLER_ACCEPTED → MOTION_STOPPED_OBSERVED；反馈不足为 STOP_UNCONFIRMED。调用成功不代表效果 SAT；UNKNOWN 不定罪，也不释放依赖该效果的后继。

本机 deadline 使用 monotonic；跨主机不直接比较 monotonic，使用 TTL 与经验证的映射/延迟上界。设备重启、仿真回退、校准变化须使旧观察与命令失效。停止策略由本体决定，不能统一清零力矩。

## 4. 实施批次与验收

| 批次 | 交付 | 验收 | 状态 |
|---|---|---|---|
| P0.1 | 来源、固定 SHA、现状矩阵、架构/ADR/路线图 | 链接有效；历史与当前证据区分 | 文档与矩阵完成；八契约实现仍待接线 |
| P0.2 | 依赖/模型/任务/资产摘要及新基线 | 原 30 任务复测；全套和可选组结果列清 | 摘要与复测已记录；感知单次超时原因未闭环 |
| P1.1 | 严格参数与 Guard 数值门 | 类型/枚举/范围/NaN/Inf/坏形状拒绝，handler/设备零副作用 | 本批完成，47 项新增回归的一部分；见交接证据 |
| P1.2 | 调用许可撤销与线程取消 | 超时、外部取消、迟到线程、重复取消；不虚报物理停止 | 部分完成：进程内调用许可与 MuJoCo 写/步进撤销；完整停止生命周期未完成 |
| P1.3 | 资源互斥与效果门禁、跨轮上下文 | 同资源不并发；UNSAT/UNKNOWN 阻断后继；超时不重放为 OK | 部分完成：效果阻断、跨轮上下文、超时去重保留失败；资源仲裁待实施 |
| P1.4 | 操作 ID、持久账本、重启对账 | 接收去重、丢回执、崩溃后 OUTCOME_UNKNOWN；合法重复动作可区分 | schema 设计后待用户批准 |
| P1.5 | 本地/RPC 同语义、观察时效、受控拓扑 | proto 生成、RPC 故障注入、Guardian 失联、旧 epoch、停止证据 | 待实现 |
| P2 | 首台真机、记录/回放、ACT 与发行最小闭环 | 一条真实分拣任务；不少于 50 次受控任务及故障注入起步 | 等待 P1 与设备条件 |
| P3 | 第二同类本体 + 导航能力域 | 同技能跨本体与不同能力域分别验收 | 未启动 |
| P4 | 第二算力、部署代理与回滚 | 任务/性能配对评测，错误组合拒绝激活，恢复演练 | 未启动 |
| P5 | 场景深化 | 由真实需求与指标定义 | 未启动 |

故障门禁覆盖相机失联、推理超时、云断连、执行进程崩溃、回执丢失、磁盘满与不兼容更新。每项报告成功数/总数、人工介入、延迟分位数与最坏观测、过期动作率和数据缺失；不凭单一测试总数宣称全平台通过。

## 5. 数据库方案（仅设计，未获变更授权）

拟在新的本地 SQLite 文件（默认 `outputs/runtime/operations.sqlite3`）建立 `schema_version`、`tasks`、`operations`、`attempts`、`operation_events`。不改现有用户数据库，不迁移或删除历史 episode；运行时表创建也属于本次 schema 变更范围，批准前不实现/执行 DDL。

- `tasks(task_id PK, robot_id, plan_revision, state, created_at, updated_at)`。
- `operations(operation_id PK, task_id FK, request_digest, skill_id, state, evidence_json)`：同 ID 不同摘要拒绝，不能仅按参数合并合法的重复动作。
- `attempts(attempt_id PK, operation_id FK, boot_id, lease_epoch, state, stop_state, started_at, ended_at)`：先写意图再调用；重启将未收敛尝试归入未知，不自动重发。
- `operation_events(event_id PK, operation_id FK, attempt_id, event_type, payload_json, created_at)`：追加事件，在单事务更新当前状态与对应事件；事件只存非敏感结果摘要。
- schema 版本显式登记；初版只创建新库，未知 schema 拒绝打开，不隐式升级；SQLite 事务不保证现实动作恰好一次。

用户已授权本次 commit/push；数据库 schema、CI/CD、密钥/环境配置、删除和生产发布仍按项目红线单独确认。先把已授权批次做成可审查提交再请求此项。

本批实现与验证的准确范围见 [2026-10-30 交接](upgrade-status-2026-10-30.md) 和 [机器可读证据](../eval/upgrade-20261030.json)。本计划不将第一批完成等同 P0/P1 全部退出。

## 6. 不丢失的旧路线

旧 M0–M2 实现保留；M2 的学习 pick 29/30 尚未达到原“不劣于脚本”DoD。多种子训练与 temporal ensembling 属后续策略改进，不应阻塞可信执行整改。DINO 仍是实验性；控制台人工验证、远程世界状态、失败归档等缺口迁入本计划。原 M3/M4/M5 分别映射到 P2、P2/P4 学习能力、P3/P4/P5；不将旧日期当新承诺。
