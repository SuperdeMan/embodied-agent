# 技术决策记录（Decision Log）

> 规则：不可逆的、有争议的、或未来会被质疑「当初为什么这么选」的决策，都记在这里。**只增不删**；推翻旧决策时新增条目并在旧条目标注 `superseded by D###`。格式：决策 / 理由 / 放弃的替代方案 / 重估触发器。

---

## D001 · 分层双系统架构（System 2 规划 + System 1 执行）
**日期**：2026-08-04 · **状态**：生效
**决策**：采用「认知层（LLM/VLM，0.2-1 Hz）+ 技能层（策略/脚本，10-50 Hz）+ 控制层（50-500 Hz）」分层，认知与执行以 SkillManifest 契约衔接。
**理由**：行业头部（Helix、GR00T、π 系列、Gemini Robotics 2）全部收敛到双系统；分层使模型换代不伤内核。
**替代方案**：端到端单模型直出动作——被否：当前延迟/可控性/安全性不满足，且个人无法参与该路线竞争。
**重估触发器**：端到端全双工机器人大模型 API 化且延迟成本可用（见架构 §8.2）。

## D002 · Python 单包起步，不用 ROS 2 全家桶
**日期**：2026-08-04 · **状态**：生效
**决策**：核心为 `src/embodied/` 单 Python 包 + uv；进程间用 gRPC（proto 先行）；ROS 2 仅在需要其硬件驱动时以 bridge adapter 引入。
**理由**：学习型技能栈（LeRobot/openpi/MuJoCo）全部原生 Python；ROS 2 带来的构建/中间件复杂度对 solo 是纯负担；car-agent 的 gRPC 工厂与 proto 纪律可直接继承。
**替代方案**：全 ROS 2（生态大但绑定深）；纯进程内单进程（实时性与安全隔离不足）。
**重估触发器**：目标硬件只有 ROS 驱动可用时。

## D003 · 数据格式对齐 LeRobotDataset
**日期**：2026-08-04 · **状态**：生效
**决策**：episode 主体用 LeRobotDataset 格式，自建仅一层版本化元数据扩展（任务语义、安全事件、planner 决策链）。
**理由**：社区最大公约数（1200+ 公开数据集、训练/可视化工具即插即用）；自研格式=自绝于生态。
**替代方案**：自研格式（灵活但孤岛）；RLDS/TFDS（生态热度已被 LeRobot 超越）。
**重估触发器**：LeRobot 格式被生态抛弃——应对是写转换器，元数据层已隔离。

## D004 · 仿真 MuJoCo 起步，Isaac Lab 作为升级项
**日期**：2026-08-04 · **状态**：生效
**决策**：M1-M2 用 MuJoCo（CPU 可跑、CI 友好、SO-101 模型现成）；规模化 RL/域随机化需求出现时升级 Isaac Lab（有 NVIDIA 官方 SO-101 sim2real 路线）。
**替代方案**：直接 Isaac Lab（重、依赖 GPU、CI 不友好）；Genesis（潜力大但稳定性观察中）。
**重估触发器**：M2 评测需要大规模并行采样时。

## D005 · 首个真机本体：LeRobot SO-101
**日期**：2026-08-04 · **状态**：生效
**决策**：真机第一站为 SO-101（leader-follower 遥操作套装），预算 ¥5-8k 含相机与备件。
**理由**：当前生态最厚的低成本平台：官方 sim2real 课程、社区数据集、开源世界模型（DreamZero-SO101）、维修便宜。
**替代方案**：宇树 R1 级人形（¥4 万起，超出场景需要）；工业协作臂（¥5 万+，杀鸡牛刀）；纯仿真（无真机数据=无护城河）。
**重估触发器**：出现碾压级低价平台或 SO-101 生态衰退。

## D006 · 复用 car-agent：复制改造，不做共享库
**日期**：2026-08-04 · **状态**：生效
**决策**：按 [reuse-from-car-agent.md](reuse-from-car-agent.md) 清单复制代码入本仓库并改造，文件头标注来源；两仓库零运行时依赖。
**理由**：两项目演进方向不同，共享库会互相掣肘；Apache-2.0 下复制合规；car-agent 约 1.7 万行可直接挪用。
**替代方案**：抽公共库（维护成本高，抽象过早）；monorepo 合仓（车与机器人耦合无意义）。
**重估触发器**：若两项目出现第三个消费者（如再开新形态 agent），重估抽库。

## D007 · 基础模型全部外采/开源微调，provider 化接入
**日期**：2026-08-04 · **状态**：生效
**决策**：LLM planner 用云 API（多供应商热切换，复用 car-agent providers）；VLA/策略用开源微调（openpi 优先、GR00T 备选）；不自研预训练。
**理由**：模型半年一换代，押接口不押模型；微调级投入个人可负担。
**替代方案**：自研小模型（数据与算力都不成立）。
**重估触发器**：SOTA VLA 膨胀到无法本地微调时转托管微调（架构 §8.2）。

## D008 · 部署形态：单机三进程，不复制微服务拓扑
**日期**：2026-08-04 · **状态**：生效
**决策**：agent-core / realtime-control / safety-guardian 三进程 + 浏览器控制台；car-agent 的服务以库形式内嵌。
**理由**：机器人是「单机-云」形态，35 容器拓扑服务于「云-边-多车」，照搬是负资产；进程边界只保留在实时性（控制环）与安全隔离（守护）处。
**替代方案**：全微服务（运维成本吞噬 solo 带宽）；单进程（LLM 卡顿会传导进控制环，守护不独立）。
**重估触发器**：多机/远程接入需求出现时，参考 car-agent gateway 设计加边界。

## D009 · 安全红线继承并加严
**日期**：2026-08-04 · **状态**：生效
**决策**：继承 car-agent 全部安全红线并做机器人化映射——LLM 不直接触达执行通道（只出计划/技能调用）；S2S 语音会话无执行工具、只能 escalate；危险动作 `require_confirm`；执行后必须验证世界状态；四级安全防线（物理/固件/软件守护/计划层）中 SG-0~2 不依赖任何 AI 输出。
**理由**：座舱的安全纪律在会动的机器人上只会更必要；这些红线在 car-agent 已被契约测试钉死，方法论成熟。
**替代方案**：无。此条不设重估触发器，只加严不放松。

## D014 · M2 数据管线：训练/部署依赖分离，转换按固定 fps 重采样，environment_state 作感知替换缝
**日期**：2026-08-07 · **状态**：生效
**决策**：
1. **依赖分组**：新增 `learn` 组（lerobot，随带 torch）只服务训练与数据集转换；`policy` 组（onnxruntime）只服务部署推理。运行时核心（main dependencies）不引入任何学习框架；CI 不装 learn/policy，相关测试在依赖缺席时自动跳过（与 `rpc` 组同规，本地必跑）。
2. **转换器 `to_lerobot()` 落地**（D012 触发器到期）：v0 episodes → LeRobotDataset v3。核心列沿用 `LEROBOT_FIELD_MAP`，另增 `observation.environment_state` = 按物体名字典序拼接的位姿向量（每物体 pos3+quat4，名单落入数据集 features.names）。这是**感知替换缝**：M2 感知 v1 上线后由感知输出填充同一向量，policy 输入契约不变（sim 真值 → 感知估计是数据源替换，不是接口变更）。
3. **重采样**：v0 录制的 hook 节奏不均匀（50 Hz 命令间隔 + settle 段一次 `step(n)` 仅触发一次采样），时间戳为 sim time。转换时按声明 fps（默认 50）均匀网格重采样：action 零阶保持（位置目标本就是阶梯信号）、观测通道线性插值——满足 LeRobotDataset 的 fps/timestamp 容差契约。
4. **技能边界入数据**：采集路径（`embodied collect`）在真实技能边界处发 `skill_start`/`skill_end` 事件并携带 `sim_t`，转换器支持按技能切分产出 per-skill 数据集，使学习策略可逐技能替换脚本实现（roadmap「manifest 不变、实现替换」的数据基础）。
**理由**：训练/部署分离兑现架构 §7「PyTorch 训练 → ONNX Runtime 部署」承诺，部署面永不背 torch；environment_state 让首个策略无需相机帧即可训练（数据轻、CPU 可训、CI 可测），相机帧通道留作视觉策略开工时的增强。
**放弃的替代方案**：v0 录制器直接录相机帧（重且慢，state-based 基线用不上）；转换时保留原始不均匀时间戳（违反 LeRobot fps 契约）；部署直接加载 torch checkpoint（部署面引入 GB 级依赖）。
**重估触发器**：图像条件策略（视觉 ACT / VLA 微调）开工时，录制器增加相机帧通道并重估存储与 fps；lerobot 大版本升级仍按依赖红线先记录再动。
**落地版本（2026-08-07）**：lerobot 0.4.4（LeRobotDataset v3.0，随带 torch 2.10.0 CPU）、onnx 1.20 系（导出期元数据写入）、onnxruntime 1.28.0。随行事实：①lerobot 装包把 protobuf 从 7.x 降至 6.x，gen/ 旧 stubs 失配，已修 `scripts/gen-proto.ps1`（绝对 `-I` 路径 + 失败即抛）并重新生成；②lerobot 的 `checkpoints/last` 符号链接在 Windows 无特权环境失败（WinError 1314），`scripts/_train_shim.py` 以目录 junction 兜底，不改 lerobot 源码。

## D013 · 三进程活性链：agent→guardian→control 单向监督，看门狗是唯一执法点
**日期**：2026-08-05 · **状态**：生效
**决策**：进程拆分的活性设计为单向链——agent-core 向 guardian 流式心跳（SafetyService.Heartbeat），guardian 向 realtime-control 持有 SupervisorLink；`--require-supervisor` 下链路缺失/过期即闩锁停机，仅显式 Reset 释放。halt 的执法权集中在**轮询看门狗循环**，绝不放在流断开的 teardown 里（gRPC 会取消 servicer 协程，`finally` 中的 await 送不出去——契约测试与真实进程冒烟各验证过一次）。附带：proto 生成物 python 根目录改为 `embodiedrpc/`（proto package 名保持 `embodied.<service>.v1` 约定），根治与真实 `embodied` 包的命名空间冲突。
**理由**：单向链让每个进程只信任下游的存在性，不依赖任何 AI 输出；kill 任一上游进程，机械臂停且保持停。
**替代方案**：网状互相心跳（复杂度高、成环推理难）；teardown 内发 halt（已被证伪）。
**重估触发器**：真机阶段（M3）若引入独立急停硬件通道，软件链降级为第二道防线，重审超时参数。

## D012 · Episode 录制 v0 用轻量中间格式，lerobot/torch 依赖推迟到 M2
**日期**：2026-08-05 · **状态**：生效
**决策**：M1 录制器落 `meta.json + steps.npz + events.jsonl`（字段名 1:1 映射 LeRobotDataset v3，映射表随 meta 落盘），`to_lerobot()` 转换器以 stub 存在；`lerobot`/`torch` 到 M2 训练管线搭建时才进依赖。
**理由**：lerobot 会拉 torch（>2GB），M1 无训练需求；字段级对齐保证 M2 转换是机械工作，不违背 D003 的格式对齐承诺。
**替代方案**：直接依赖 lerobot（安装重、当前零收益）；自研格式不对齐（违背 D003）。
**重估触发器**：M2 训练管线开工时实现转换器，并用社区可视化工具验证互操作。

## D011 · proto 工具链：grpcio-tools 替代 buf
**日期**：2026-08-04 · **状态**：生效
**决策**：proto 生成用 uv `rpc` 依赖组内的 grpcio-tools（`scripts/gen-proto.ps1|.sh`），不引入 buf。
**理由**：buf 是全局二进制安装（触碰全局依赖红线），M0 仅 3 个 proto，grpcio-tools 在项目内闭环、跨平台一致。
**替代方案**：buf（生态标准、lint/breaking 检测与多语言生成更强）。
**重估触发器**：proto 数量明显增长，或需要为 console（TypeScript）生成 stubs 时，改用 buf 并记录新决策。

## D010 · 许可证：Apache-2.0
**日期**：2026-08-04 · **状态**：生效
**决策**：沿用仓库初始化时的 Apache-2.0。
**理由**：与 car-agent 一致，复制代码零摩擦；对未来商业化（闭源增值、专利授权）友好。
**替代方案**：MIT（无专利条款）；AGPL（吓退潜在使用者，与商业化路径冲突）。
