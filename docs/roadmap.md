# embodied-agent 路线图

> 状态：v1.0（2026-08-04）。进度以各阶段退出标准（DoD）为准，不以日历日期问责——本项目按业余带宽推进，日期是锚点不是承诺。
> 关联：[架构设计](architecture.md) · [决策记录](decisions.md) · [car-agent 复用评估](reuse-from-car-agent.md)

## 阶段总览

| 阶段 | 主题 | 时间锚点 | 一句话退出标准 |
|---|---|---|---|
| M0 | 规范与骨架 | 2026-08 | 文档规范齐备，代码骨架 + 复用移植完成，CI 绿 |
| M1 | 仿真闭环 | 2026-08 ~ 09 | 语音指令在仿真中完成 pick/place 全链路，每次运行自动产出数据 |
| M2 | 学习闭环 | 2026-10 ~ 11 | 第一个学习策略在评测集上不劣于脚本技能，训练→部署一条命令 |
| M3 | 真机落地 | 2026-12 ~ 2027-02 | SO-101 真机复现仿真闭环，安全防线全部生效 |
| M4 | VLA 与泛化 | 2027-H1 | 微调 VLA 接管 ≥2 个技能，未见任务组合有可测泛化 |
| M5 | 数据飞轮与扩展 | 2027-H2 起 | 「数据→训练→评测→部署」周级自动循环，第二本体接入 ≤2 周 |

依赖关系：M0→M1→M2→M3 严格串行；M4 依赖 M2（数据管线）与 M3（真机数据更佳但非必需，可先在 sim 做）；M5 持续演进。

---

## M0 规范与骨架（2026-08，约 1-2 周）

**目标**：把「约束先行」落满，把 car-agent 可直接复用资产搬进来，让后续每个会话都有据可依。

**任务**
- [x] 项目规则（CLAUDE.md）、架构设计、路线图、决策记录、复用评估（2026-08-04）
- [x] uv 初始化 `src/embodied/` 单包骨架 + 目录结构（按架构 §10），ruff + pytest 配置
- [x] GitHub Actions CI：lint + 单测门禁
- [x] P0 移植（执行记录见[复用评估](reuse-from-car-agent.md) §3）：providers LLM 体系、obs（JSONL sink 改造）、ledger/testing、permission + 机器人域 scopes（memory 服务按复用评估属 P2，原文误列于此，已修正）
- [x] proto 首版：三进程契约 + grpcio-tools 生成链路（D011）
- [x] `.env.example` 建立（参数名从 car-agent 精简，值全空）
- [x] 附加完成：最小认知环（bounded tool loop）+ `embodied chat` 文本模式 + SO-ARM100 仿真资产与渲染验证（M1 探路）

**DoD 达成（2026-08-04）**：219 个测试全绿（其中约 189 个为移植随行/新增）；`embodied chat` 离线全链路通过（自然语言 → 技能调用 → 结果汇报，危险技能确认拒绝）；术语纪律契约测试在集成中拦下一处泄漏后全清。

**风险**（保留供回顾）：移植贪多嚼不烂 → 严格按 P0 清单执行，P1/P2 留给用到时再搬。

## M1 仿真闭环：see-think-act（2026-08 ~ 09，约 4-6 周）

**目标**：在 MuJoCo 中打通「语音→规划→技能→执行→验证→汇报」全链路，数据录制默认开启。

**任务**
- [x] MuJoCo 桌面场景 + SO-ARM100 模型；`Embodiment` 接口 + sim driver（2026-08-05，Menagerie 尚无 101，fetch 脚本按设计回退 100）
- [x] 脚本技能三件套：`skill.manip.pick` / `skill.manip.place` / `skill.arm.home`（DLS IK + 插值轨迹 + 执行后世界状态验证；抓取参数为网格扫描标定，见 manip.py 注释）
- [x] SkillRegistry + manifest 加载 + 契约测试（M0 完成）
- [x] WorldState v0（本体状态 + 仿真真值物体表 + 区域谓词；感知 M2 再上）
- [x] 移植改造 planner 引擎替换 M0 最小环（2026-08-05）：Plan/Step 模型、PlanBuilder（submit_plan 工具通道、原子校验、manifest 权威链）、DagExecutor（拓扑分层并行、param_refs 三形态、副作用防抖、确认闸 fail-closed）、verify 三态对账（SAT/UNSAT/UNKNOWN、声明式谓词注册表、`$param:` 动态期望、须确认步永不重试）、有界 replan 循环（分档预算）。M0 最小环保留为降级路径
- [x] Safety Guard v0：工作区围栏（越界闩锁）、速率限幅、命令来源白名单，接入 driver 写路径（2026-08-05）；心跳看门狗随三进程拆分实现（proto 已备）
- [x] 控制台 v0（2026-08-05）：`embodied console` — 浏览器按住说话（AudioWorklet 16k PCM 上行）→ ASR → 引擎 → step 时间线 + 流式 TTS 回放 + 场景视图 5fps + 危险技能确认弹窗；单 WS 会话协议（hri.v0），无 key 时 mock 音频离线可用。语音前端为精简重写而非整栈移植（VAD/KWS 唤醒与打断随 S2S 升级引入，见 reuse 文档执行记录）
- [x] Episode 录制落盘（LeRobot 字段对齐的中间格式，D012；转换器 M2 实现）
- [x] 接线 M0 移植库（2026-08-05）：GuardedProvider 把 ratelimit 挂到 complete/complete_tools、cache 挂到 complete（ported cache 只存文本形态，规划调用刻意不缓存以免丢 tool_calls）；ledger 持久化后端选型仍留 M2

**阶段进展（2026-08-05 第二批）**：planner 引擎移植完成，`embodied sim --eval 10` 改为全链路引擎评测（文本指令→submit_plan→DAG→技能→独立世界状态裁判）仍 **9/10**（失败局为已知扇区边缘位，非引擎回退）；技能 manifest 新增声明式 `verification` 字段（pick 用 `gripper_holding` 谓词、place 用 schema 模式）。

**阶段进展（2026-08-05 第三批）**：控制台 v0 + 语音链路落地（headless 协议冒烟通过：文本/语音指令→执行→TTS 回流、确认弹窗桥接、场景流）；ASR/TTS provider 族移植完成（21 类，与源 diff 验证零逻辑改动）；**扇区边缘抓取失败修复**（根因：斜接近角下固定指下降途中推移方块 9mm；两段式下降 + 中途重瞄）——评测三种子 **30/30**。

**M1 完成（2026-08-05 第四批）**：①评测任务集版本化——`eval/tasks/tabletop_pick_place_v1.yaml`（种子/随机化维度/裁判/阈值声明式）+ `--task` 运行器 + 结果 JSON 落盘 + `eval/BASELINES.md` 只增账本（首条基线 30/30）；②三进程拆分——`serve-control`/`serve-guardian`/`sim --remote` 三进程拓扑，活性链 agent→guardian→control（D013），真实进程冒烟验证「kill guardian → control 闩锁拒绝执行」；RemoteSkillRegistry 使确认门禁跨进程成立。全量 384 测试绿。**遗留到 M2 前**：控制台真机浏览器人工验证（用户执行 `embodied console`）；远程模式的世界状态流（当前远程验证退化为 UNKNOWN 不定罪，需扩展 StreamState 或增 WorldService）。

**DoD**：「把红色方块放到盒子里」类自然语言指令，10 次运行 ≥8 次成功（脚本技能 + 真值感知条件下）；每次运行自动产出可回放的 episode；断网时已注册技能仍可通过控制台指令执行。

**风险**：IK/抓取姿态调参耗时 → 用 LeRobot/Menagerie 社区现成配置起步；语音链路细节多 → 三段式先行，S2S 后置。

## M2 学习闭环（2026-10 ~ 11，约 6-8 周）

**目标**：第一次「用自己采的数据训出策略并部署回系统」，评测体系立起来。

**任务**
- [x] 仿真遥操作（键鼠/手柄 → 后续真机 leader 臂复用同一路径），采集 pick/place 50-100 episodes（2026-08-07：输入无关 `TeleopSession` + `embodied teleop` 键盘前端，围栏预检 fail-closed；专家数据由 `embodied collect` 以脚本技能直采 60/60，携带 sim_t 技能边界；人类示教与脚本示教走同一录制/转换管线）
- [x] LeRobot ACT 基线训练管线：`scripts/train.py` 一条命令从数据集到 checkpoint（2026-08-07：`embodied convert` 实现 D012 转换器并经 LeRobotDataset v3 loader 回读验证互操作（D014：fps 重采样、environment_state、按技能切分）；训练走 lerobot 0.4.4，Windows checkpoint 符号链接以 junction 兜底）
- [x] PolicyProvider：checkpoint → ONNX → 学习技能接入 SkillRegistry（manifest 不变，实现替换）（2026-08-07：`scripts/export_onnx.py` 把归一化烘进图并做 torch/ORT 数值校验门；OnnxPolicy 部署侧零 torch；学习技能与脚本技能共用 manifest 对象与真值裁判，`embodied sim --pick-policy/--place-policy` 在同一版本化任务上对比）
- [ ] 感知 v1：Grounding DINO + SAM2 + 深度 → 物体注册表（替换仿真真值，sim 内先验证感知链路）
- [ ] 评测 harness：版本化任务集（位置随机化）、nightly CI 出成功率报告、golden-gate 纪律生效（版本化任务集与 append-only 账本 M1 已生效并用于本阶段脚本/策略对比；余 nightly CI 报告）
- [ ] 失败案例自动归档与标记（v0 已有：采集失败 episode 保留并标 success=false，转换默认过滤；自动归档规则与失败挖掘未做）

**阶段进展（2026-08-07 第一批）**：学习闭环管线全线打通并首次出数——`embodied collect`（专家 60/60）→ `embodied convert`（pick/place 按技能切分 16.7k/10.4k 帧 @50fps）→ `scripts/train.py`（ACT state+environment_state，CPU 小时级，pick loss 0.077 / place 0.060）→ `scripts/export_onnx.py`（归一化入图，torch/ORT 数值差 <1e-6）→ OnnxPolicy 同 manifest 替换。同任务同裁判：脚本 30/30；**学习 pick + 脚本 place 29/30；双学习 29/30**（两配置失败为同一深位 pick 超时局；学习 place 29/29）。「训练→评测→部署一条命令」成立；「不劣于脚本」差 1 局，路径明确（失败位补采/加练）。账本见 `eval/BASELINES.md`。

**DoD**：学习策略在评测集成功率 ≥ 同任务脚本技能；训练→评测→部署全程一条命令；感知驱动（非真值）的闭环在 sim 中跑通。

**风险**：首个策略成功率低 → 数据量与随机化范围逐步加，脚本技能保底不下线；评测集设计偏简单 → 参照 LIBERO 类基准的随机化维度设计。

## M3 真机落地（2026-12 ~ 2027-02）

**目标**：一切在真实世界重演一遍。这是项目从「框架」变成「机器人」的阶段。

**任务**
- [ ] 硬件采购与组装：SO-101 leader-follower 套装 + RGB-D 相机 + 工位（预算约 ¥5-8k，含备件）
- [ ] Feetech 串口 driver 实现 `Embodiment` 接口；标定（手眼、关节零位）
- [ ] 真机遥操作采数（leader 臂），真机数据微调/重训策略
- [ ] Safety Guardian 真机化：SG-0/1/2 全部生效，首跑降速模式
- [ ] sim2real 差距报告：同任务 sim/real 成功率对比与原因分析（沉淀为文档）
- [ ] 真机操作纪律写入 CLAUDE.md（急停在手边、无人不运行等）

**DoD**：真机复现 M1 语音闭环 + M2 学习技能，评测任务真机成功率 ≥60%（首版基线，重在建立测量而非数字本身）；任何 AI 层进程被 kill 时机械臂安全停止。

**风险**：sim2real gap → M2 已用真实感知链路预演，剩余 gap 集中在动力学与标定，用真机数据微调收敛；硬件到货/组装周期 → 提前至 M2 末采购。

## M4 VLA 与泛化（2027-H1）

**目标**：从「一任务一策略」进入「语言条件多任务」，验证本架构吃 VLA 红利的能力。

**任务**
- [ ] openpi（π0/π0.5 系）用自有数据微调，SkillRegistry 以语言条件技能方式接入；GR00T N1.7 路线做对照评估后二选一
- [ ] 泛化评测集：未见物体 × 未见指令组合 × 位置随机，成功率单独跟踪
- [ ] VLA 技能替换 ≥2 个专用策略；失败案例回流数据集
- [ ] S2 增强：VLM 场景描述、失败反思入记忆、计划前世界模型预演试点（DreamZero-SO101 类，作为验证者角色）
- [ ] 评估经验强化（RECAP 类失败数据利用）在自有数据上的可行性

**DoD**:VLA 技能在既有任务上不劣于专用策略，且在泛化集上显著优于脚本兜底；「换 policy 模型」全程未改动内核代码（架构承诺的实证）。

**风险**：个人算力微调受限 → LoRA/量化微调优先，不行则转托管微调；VLA 推理延迟 → 动作分块 + ONNX/TensorRT，控制环降频兜底。

## M5 数据飞轮与扩展（2027-H2 起，持续）

**目标**：让系统随使用变强，而不是随维护变旧。

**任务**
- [ ] 部署日志→数据集自动回流：curation 规则（去重、质量分、失败标记）、周级再训练与评测循环
- [ ] 第二本体接入（候选：更大工作空间的臂 / 移动底盘 + 臂，按当时价格与场景定），验证「2 周接入」承诺
- [ ] 世界模型角色升级评估：从验证者到规划参与者（视当时推理成本）
- [ ] 场景扩展与对外形态决策（见下方商业化假设）

**DoD**：无人工干预的周级「数据→训练→评测→部署」循环稳定运行一个月；第二本体从开箱到跑通评测 ≤2 周。

---

## 节奏与机制

- **季度技术雷达**（1/4/7/10 月）：复查架构 §8.2 失效触发器，扫描模型/硬件/生态变化，产出一页纪要入 `docs/`，必要时修订架构与本路线图。技术判断过时是计划内事件，不是事故。
- **评测驱动**：M2 起任何策略/模型变更以评测集数字说话；基线只能被数据推翻，不能被感觉推翻（car-agent golden-gate 纪律平移）。
- **文档同步**：架构级变更先改 architecture.md + decisions.md 增条目，再动代码；阶段状态在本文档打勾维护。

## 商业化假设（开放问题，M2 后再定）

三条候选路径，当前均不承诺，靠 M1/M2 的 demo 反馈与个人意愿决策：

1. **开发者框架/开源影响力**：面向 LeRobot 生态的「带安全与语音交互的具身 agent 运行时」，吃社区红利，反哺数据。
2. **垂直场景产品**：桌面陪伴/效率/教育机器人（语音交互是现成强项），走「智能硬件 + 订阅」。
3. **行业轻方案**：小商户/轻工业桌面级分拣整理 PoC，按项目收费验证付费意愿。

## 近期行动清单（M2 中程，2026-08-07 更新）

采集→转换→训练→ONNX 部署→对比评测一条链已通（首个学习策略 29/30，账本见 `eval/BASELINES.md`）。接下来：

1. 学习 pick 补强至不劣于脚本（30/30）：对失败深位（seed=1 附近扇区深处）补采示教 / 扩数据随机化 / 加练步数，数字说话
2. 感知 v1（Grounding DINO + SAM2 + 深度）：填充与真值同构的 `environment_state` 向量（D014 替换缝），sim 内跑通感知驱动闭环——这是 M2 DoD 的最后一块
3. nightly CI 评测报告 workflow（草案已交用户，CI 配置属红线，批准后落地）
4. （视带宽）`embodied teleop` 实采一批人工示教，验证人类数据走同一转换/训练管线；失败案例自动归档规则化
