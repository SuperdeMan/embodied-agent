# 技术决策记录（Decision Log）

> 规则：不可逆的、有争议的、或未来会被质疑「当初为什么这么选」的决策，都记在这里。**只增不删**；推翻旧决策时新增条目并在旧条目标注 `superseded by D###`。格式：决策 / 理由 / 放弃的替代方案 / 重估触发器。

## D027 · 先持久任务计划，再做只读恢复评估

**日期**：2026-11-04 · **状态**：实施中；用户已确认 schema v2 升级与部署，验收通过后执行。

**决策**：增加任务计划版本和追加事件日志，接收端固定 manifest 权威；操作意图在调用前保存。重启只恢复历史和中断状态，禁止旧任务直接接收新的物理步骤；只读评估列出证据、缺口与需人工处理事项，始终不自动续跑。

**理由**：现有任务行只记录 ID/修订号，无法还原未执行步骤或进程崩溃前的计划；把内存 `done` 字典当作恢复来源会丢失证据，也可能重放旧动作。需要真实任务日志，不能用伪造“日志专用操作”挤占物理尝试，或用不具备同事务能力的旁路文件代替。

**替代方案**：重启后重新跑原话、把历史 SAT 当当前 SAT、客户端上传成功状态以解锁动作、为避免迁移而制造虚假 operation（均拒绝）。本批只追加两表，无旧列更改、数据删除或自动物理恢复。

**边界**：完整执行恢复与多 attempt 仍未开放；schema 迁移须在具体方案验证后单独确认。原生控制与真实设备停止证据不由任务日志代替。

## D026 · 子进程只承载可复制输入的纯计算

**日期**：2026-11-04 · **状态**：生效（纯计算参考边界，验收见 [isolated-compute.md](isolated-compute.md)）。

**决策**：在本地管道隔离颜色/DINO 检测、ONNX 推理和状态副本渲染。父进程独占执行通道和账本，子进程不接收本体句柄；严格数据帧绑定 boot/request，故障不重放，显式新代次只在旧进程/I/O 已回收后建立。

**理由**：线程超时无法终止持有 GIL 的原生调用。直接把整个活跃本体送入子进程会复制控制状态、锁和权限，难以证明停止与数据归属。纯计算边界可真实验证崩溃、挂起、回收和恢复，输出仍经 D022/D023 与独立效果判断。

**替代方案**：fork 活跃仿真、pickle 本体/任意 callable、超时后悄悄重放推理或动作、旧进程未退出就启动替代进程（均拒绝）。保留直接 provider 类作为参考实现，应用工厂明确启用进程适配；无数据库迁移或全局依赖安装。

**依据与边界**：[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 与 [Linux PR_SET_PDEATHSIG](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)。Linux 的死亡信号关联创建子进程的线程，因此创建与 I/O 生命周期合并到专用长寿命线程。进程隔离不等于恶意代码沙箱，也不保证内核/硬件故障下的硬实时终止。

## D025 · 观察有界等待，迟到结果无提交权

**日期**：2026-11-04 · **状态**：生效（进程内有界观察，验收见 [bounded-observation.md](bounded-observation.md)）。

**决策**：单个运行时使用单在途、无队列的只读观察线程通道。绑定及效果观察异步等待，默认 1 秒超时；超时或取消后仍追踪实际工作完成，拒绝叠加工作与新动作。读线程不继承动作许可，账本提交只能在拥有者线程通过当前请求完成。

**理由**：D024 同步采样会阻塞事件循环；仅包 `wait_for(to_thread(...))` 无法停止线程，还可能在重复超时后堆积工作和发布迟到结果。应明确区分“调用者不再等待”与“观察线程已退出”。

**替代方案**：超时后立即替换线程、在工作线程写 SQLite、取消后仍接受成功回调、默认执行器无限排队（均拒绝）。不更改数据库 schema 或 RPC wire。

**边界**：这是进程内等待/并发约束，不是强制终止或原生崩溃隔离。永久卡住的提供方保留故障状态，需受控重启；模型独立进程和完整任务恢复另行实现。

## D024 · 效果证据独立落账，旧调用不借新场景成功

**日期**：2026-11-04 · **状态**：生效（参考本体与私网仿真，验收见 [effect-reconciliation.md](effect-reconciliation.md)）。

**决策**：绑定本体的 Registry 在释放资源前采集声明式空间效果并追加到现有事件表；效果、调用和停止保持独立。后继只接受持久且当前有资格的证据。接收端提供证据检查和显式只读重查；重查不改调用结果、不释放隔离、不自动重放动作。

**理由**：执行器内存中的 `_verify` 会在重启后丢失；旧操作回执重放后再读取当前世界，可能把其他操作的结果误记为旧动作成功。需要同时绑定请求、尝试、场景和资源序列，历史 SAT 也不能永久当作当前事实。

**替代方案**：客户端上传 SAT、直接把 RETURNED 改成 SUCCEEDED、确认停止后自动重试、重启后把新场景观测归给旧操作（均拒绝）。沿用 schema v1 五表，不添加 DDL。

**边界**：当前支持参考本体的已注册空间谓词；记录状态证据，不证明因果归属。跨控制器重启、reset 或后续冲突操作后拒绝复用；真实传感器与多 attempt 自动恢复仍待独立验证。

## D023 · 对象缓存保留检测年龄，推算不续期

**日期**：2026-11-04 · **状态**：生效（本地仿真感知，验收见 [object-freshness.md](object-freshness.md)）。

**决策**：本地感知对象增加来源、采集双时钟、时钟/场景/标定身份和期限；短暂遮挡可复用或推算位置，但不得更新检测时刻。过期、相机错误持续超预算、场景/标定变化或时间倒退时，旧对象退出可用表；空间效果为 UNKNOWN，不构造反事实失败或成功。策略输出沿用输入对象证据，再新鲜的本体反馈也不能给旧对象续期。

**理由**：现有感知注册表永久保留最后位姿，附着信念没有期限。整体快照时间新鲜不证明对象最近被看见；对旧坐标继续求解会掩盖失联与任务结果不确定性。

**替代方案**：每次读取给所有对象重新盖时间戳、遮挡时切回裁判真值、用夹爪编码器永久证明持物（均拒绝）。本批不增加跨机视觉发布接口或数据库迁移。

**边界**：短期运动学位姿仍是有期限的信念，不是接触传感器证据。参考双时钟预算须经原任务回归，不能为通过失败用例无限放宽；完整任务效果持久对账与真机传感器时钟另行实现。

## D022 · 驱动前消费动作包络，遥操作共享执行权威

**日期**：2026-11-03 · **状态**：生效（仿真参考范围，验收见 [action-admission.md](action-admission.md)）。
**决策**：新增纯类型 ActionProfile/ObservationRef/ActionEnvelope，在已绑定的 MuJoCo 写入口同时核对运行时许可、资源、身份、代次、序号、控制语义与期限。遥操作同步入口和异步技能复用准入/落账逻辑，不能创建第二套资源锁。手动命令不进入模型工具目录；发生不确定执行时仍交 D021 对账。
**理由**：旧遥操作绕过 Registry；裸 source 标签不足以证明权限；只在取消时检查许可，不能拒绝重复、迟到或来自另一控制器的动作。策略推理耗时必须计入输入观察年龄。
**替代方案**：仅给现有命令补时间字段、不核对真正调用许可；遥操作单独加一把锁；推理结束刷新观察时间；向云端开放任意关节写 API（均拒绝）。
**边界**：当前为单进程控制侧包络，本体为仿真参考。独立 reference 夹具与空闲物理时钟不是平台控制权限。跨主机身份、时钟映射、对象级感知时效和真实驱动仍须单独验证。高层 RPC 保持技能调用，不新增 raw-action 通道；现有五表记录操作与来源，不做 schema 迁移。

## D021 · 停止观测只由受信本体控制器出具，先落账再释放

**日期**：2026-10-30 · **状态**：实施中。

**决策**：对未知操作增加独立的、需明确确认具体 attempt 的停止对账入口，不放入模型工具目录。阻塞线程与协程分别追踪；旧线程仍活跃、历史操作无跟踪元数据、控制器/profile 缺失或身份不符时拒绝恢复。当前仅绑定 MuJoCo 参考适配器。

**参考停止策略**：维护围栏阻断普通 write/step/reset；保持机械臂当前位置，夹爪按双侧接触力保持夹持或当前位置（只允许可信 profile 定义的微小软限位修正），推进仿真并以连续关节速度样本证明停止。包含夹爪，禁止直接清零速度制造证据。profile、资产、采样和时效全部记录；该模拟证明不能转借真机。

**持久与取消语义**：沿用现有五表，在准入事件增加元数据，在停止事件追加完整证据并更新 attempts.stop_state。无 DDL 或数据迁移。只有持久提交成功才解除资源隔离；操作结果仍 UNKNOWN，不自动重试旧动作，不解除 Guard 闩锁。已提交的重复请求幂等读取，不对新 owner 再发停止。

**替代方案**：协程结束等于线程退出、收到 ACK 等于停止、直接归零仿真速度、删除未知账本、只存客户端 stopped=true、观察后先解锁再写库（均拒绝）。

**重估触发器**：真实控制器/驱动可提供停止确认与速度反馈后新增其专属 profile；统一遥操作与 ActionEnvelope 后扩展全出口证明。云端仍固定已验收 `212f912`，本批不自动部署。

**落地记录（2026-10-31）**：`01f18b7` 完成受跟踪工作线程、维护围栏、确认式 RPC、连续速度/双侧接触力证据、先持久化后释放及旧 ID 不重放。539 测试通过，脚本基线 30/30、pick-v1 29/30；持久化故障探针与边界见 [stop-reconciliation.md](stop-reconciliation.md)。已修正未发布原型中夹爪反馈位置保持导致卸载夹持力的问题，不能将该原型视作最终策略。

## D019 · 有界远端快照读取，不使用跨机单调时钟比较

**日期**：2026-10-30 · **状态**：实施中。
**决策**：高层 GetWorldState RPC 返回纯类型版本化观察包络；客户端对往返与捕获耗时设界，检查来源、boot/epoch/sequence、有限值与坐标；规划与验证共享读取接口，失效为未知并阻断依赖。仿真 reset 显式增加场景代次。
**理由**：旧远端路径没有对象/区域，效果固定未知；仅给旧缓存补一个新时间戳会伪造新鲜度，不同主机 monotonic 也不能直接相减。
**替代方案**：把 UNKNOWN 当成功（拒绝）；无限复用最后快照（拒绝）；先引入全量传感器消息中间件（当前低频验证无需）。
**边界/重估**：当前只对仿真真值 reference profile 出具新鲜快照；感知对象自身 TTL、设备标定证明、受认证跨机来源和高频 ActionEnvelope 仍需后续实施。

**落地记录**：`fde3247` 接入异步远端快照、epoch/sequence/时效检查及一致性锁；本地应用测试 511 passed，原脚本任务 30/30，见 [remote-world-state.md](remote-world-state.md)。

## D020 · 云端先交付私网仿真参考控制台

**日期**：2026-10-30 · **状态**：部署候选准备中，未激活。
**决策**：沿用已有共享服务器与私网访问方式，但建立独立 embodied-agent 目录、镜像、容器与 SQLite 数据；仅绑定回环端口，候选 Tailscale HTTPS 8449。不复用姊妹项目密钥或容器，不改其 Serve 映射，不启用 Funnel。
**理由**：当前可运行的是 MuJoCo+脚本技能+控制台+操作账本；共享主机可承载受限 CPU 仿真，没有证据支持 GPU 训练或真实设备控制。私网演示先采用离线规划与模拟语音，页面明确区分。
**替代方案**：直接暴露 RPC/设备控制到公网、把训练加入常驻栈、复用另一项目 .env（均拒绝）；将同进程仿真演示当作独立 Guardian 真机拓扑（不成立）。
**激活条件**：干净提交、镜像/资产摘要、健康与场景渲染、WebSocket 离线任务测试、资源/端口检查通过后，只在用户明确批准该服务器与映射变更后激活。后续真正云边控制另行满足设备身份与现场停止契约。

**候选验收**：`212f912` 的 Linux 镜像通过隔离渲染、单次抓放、账本与实际容器限额检查；其他容器不变，候选已停止，尚未发布常驻入口。结果见 [cloud-deployment.md](cloud-deployment.md)。

**激活接线修正（2026-10-30）**：用户明确批准激活后，发现本机 Docker 内部网络未发布声明的回环端口，容器内健康正常、宿主访问内部容器地址正常。实测 Tailscale 1.102.2 支持 HTTP 代理到该内部地址，因此移除无效宿主端口声明，保持 internal 网络，通过 HTTPS 8449 直接代理已验证的容器 IP。常规 restart 后须核对 IP，recreate/升级后须重新发现并更新该映射；不增加外部网络、代理容器或宿主防火墙规则。应用镜像仍固定为已验收的 `212f912`，运行配置摘要单独记录。

**激活结果**：回执 `20261030T133438Z` 为 activated；应用 `212f912`、配置 `cfb5997`。实际 HTTPS/WSS、JPEG 和持久卷重启检查通过，其他 39 个容器及 Serve 映射未变；浏览器工具缺席，未声称完整浏览器验收。见 [部署记录](cloud-deployment.md)。

## D018 · 持久操作身份、单写者账本与未确认资源隔离

**日期**：2026-10-30 · **状态**：已获用户明确授权，实施中。

**决策**：用户在前一批具体 schema 与 CI 注释请求后授权继续；新建此前设计的 SQLite 五表，不修改既有业务数据库。执行入口记录 intent-before-dispatch，同 operation_id 不同请求摘要拒绝，已返回结果可查询/回放，未收敛调用重启后标未知。RETURNED 是调用结果，不是任务 SUCCEEDED。所有危险动作每次走原确认门，持久证据不能替代授权。

**资源与取消**：受信 manifest 声明组合资源，内部租约具有 epoch 和唯一 owner，写许可绑定租约；超时、取消和异常执行结果不能证明停止，资源隔离并阻止冲突新动作。未知资源跨重启恢复，普通 Guard Reset 不清除。RPC 查询与取消只暴露已有状态，不把请求受理包装为 MOTION_STOPPED_OBSERVED。

**替代方案**：仅存一份 JSON（缺事务与唯一约束）；数据库事务保证物理恰好一次（做不到）；超时后立即放锁（迟到线程或残余运动仍可能存在）；启动时无条件改判所有运行（可能有另一个活跃进程）。均不采用。

**存储边界**：OS 文件锁保证同库单执行进程；SQLite FULL 同步与事务记录状态/事件。保持原内存任务账本 API，不隐式迁移。schema 版本固定，未知版本失败；文件故障时拒绝准入，结果提交失败保留未知而非宣称成功。只有对应终态结果回放，不以技能参数摘要合并合法的新操作。

**重估触发器**：接入真正停止观测、人工对账或可验证恢复策略后，再添加解除资源隔离与新 attempt 的受控接口；跨主机或多个执行器部署需受认证身份与共享资源权威，当前本地锁不替代它们。

**落地记录**：`c6392de` 完成五表账本、单库进程锁、组合资源准入、未知结果跨重启隔离、RPC 操作查询与取消；实现范围和验证见 [operation-runtime.md](operation-runtime.md)。真实停止观测与解除隔离不在该提交中。

## D017 · 原仓平台化：三平面、八契约、先可信执行

**日期**：2026-10-29 · **状态**：目标生效，分批实现；细节见 [执行计划](platform-upgrade.md)。

**决策**：按用户指定完整报告，在原仓逐模块升级；三平面为现场执行、管理发布、数据学习。保留 MuJoCo、技能 ID、ACT、评测任务与历史。先落地参数/数值准入、调用写许可撤销、资源仲裁、效果门禁和跨轮对账，再接持久操作账本、本地/RPC 同语义与时效契约。八类核心契约在本仓维护，纯类型不依赖厂商 SDK。D006/D016 的跨场景验证前不抽库原则继续生效。

**理由**：现有 `wait_for(to_thread)`、弱参数校验、NaN 透传、同层物理并发、UNSAT/UNKNOWN 状态与跨轮上下文缺口会破坏可信执行。扩展更多模型或 UI 不能解决这些问题。

**替代方案**：清空重写（丢失可比较资产）；先接多个 NPU/机器人空壳（无真实任务证据）；以协程取消证明物理停止（语义错误）。均不采用。

**边界**：八契约为目标，不能因增加类型就声称接线完成。写许可撤销、控制器回执、停止观测分别记录。持久账本 schema 批准前仅设计；不把参数指纹去重当作物理操作 ID 或跨重启恰好一次保证。模型和发行必须绑定目标本体/后端/校准/动作语义与评测，维护态激活，未经恢复演练不承诺自动回滚。

**重估触发器**：第二同类本体或第二算力配置的实测暴露契约不足；硬件/SDK 可获取性变化。新增顶层 adapters/profiles/deploy 与 perception/deployment 子模块按实际实现批次建立，不预建无消费目录。

**与旧决策关系**：D001/D009 安全与分层保持；D002 的 ROS 按适配隔离扩展，核心仍单包；D003/D012 演进为原始证据 + Episode 索引 + LeRobot 导出；D008 的三进程为现有参考拓扑，模型/录制隔离按需要增加；旧 M5 无审核自动部署目标改为经评测的受控发布。

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

## D015 · 感知 v1：provider 化开放词汇感知链，真值只留给裁判
**日期**：2026-08-08 · **状态**：生效
**决策**：
1. **PerceptionProvider 族**（`providers/perception.py`）：接口 `detect(rgb, prompts) -> [Detection(bbox/score/mask?)]`，只管 2D 检测；3D 位姿由感知管线（`cognition/perception.py`）组合深度图反投影完成，产出与 `Observation.objects` 同构的 `{name: Pose}`——填的正是 D014 的 environment_state 替换缝，policy/技能/规划输入契约零改动。
2. **双 provider 起步**：`GroundingDinoProvider`（transformers，开放词汇，`perceive` 依赖组）+ `ColorBlobProvider`（纯 numpy HSV 色块，零依赖、离线与 CI 可测）。SAM/SAM2 mask 细化是 detector 的后处理挂点：v1 用 bbox 中心区深度中值 + 视线方向半尺寸补偿已到 cm 级，SAM2 包装稳定后再接。
3. **深度与相机模型**：MuJoCo Renderer depth pass + `cam_fovy` 推内参、`cam_xpos/xmat` 外参反投影；真机（M3）换 RGB-D 相机实现同一 `CameraModel` 接口。
4. **裁判永远吃真值**：`PerceivedSim` 包装器只替换 agent 可见的 `Observation.objects`（本体感受 qpos/ee 保持编码器真值），并暴露 `read_truth()` 给评测裁判——感知误差如实反映在成功率里，但测量本身不被感知污染。技能内部的效果自检（held/region）走感知视角：agent 只能用自己的眼睛证明自己。
5. **v1 已知限制（如实声明）**：单视角无姿态（quat 置 identity，主轴 yaw 留升级位）；感知按 sim 时间节流（默认 0.25s 缓存，检测器越重节流越粗）；bin 等区域是工作区标定物不是感知目标（v1 继续读场景标注）。
**理由**：与 D007/D014 的 provider 纪律一致；色块兜底使链路在无权重/无网环境可完整验证（仿真先行 + CI 友好）；裁判/感知分离延续 D009「验证不依赖 AI 输出」精神。
**放弃的替代方案**：感知同时喂裁判（自我证明，直接违反评测纪律）；等 SAM2 生态稳定再开工（链路验证被无限期推迟）；直接上真机 RGB-D SDK（违背仿真先行）。
**重估触发器**：SAM2 官方 PyPI 包稳定后接 mask 细化；M3 手眼标定引入后重审相机模型与多视角。
**落地记录（2026-08-08）**：ColorBlob 链路以 `--perception color` 过版本化评测 **30/30**（位姿估计 ~2mm：逐像素反投影 + 中位深度内点滤波（剔除臂色渗入）+ 每相机 push 标定 top 0.45 / side 0.3）。遮挡三防线各自挡下过实测故障：多相机后备（home 臂影遮扇区中部）、面积上下限门 0.35/4.0×（悬停夹爪致偏心月牙 0.06×、开放词汇误检超尺寸）、运动学附着信念（举升后目标不可见时随手移动，误抓由再见即纠）。GroundingDino provider 已接通（权重直连可下，干净帧 2mm/score 0.92）但本场景高置信误检与 CPU 时延未解，标**实验性**，鲁棒化（提示词/跨相机一致性）留后续。随行工程事实：MuJoCo Renderer 的 GL 上下文线程绑定——渲染器缓存按（线程,尺寸,种类）隔离，感知渲染汇聚专属单线程并在该线程 close()（跨线程析构段错误，实测两次）。

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

## D016 · D006 重估：第三个消费者 drone-agent 出现，公共内核仍以双场景验证为前提

**日期**：2026-09-19 · **状态**：生效（D006 维持生效）

**决策**：`drone-agent` 作为第三个消费者触发 D006 的重估。当前继续按清单复制改造、零跨仓运行时依赖、来源标注与测试随行，不立即建立共享库。只有机械臂与无人机在各自真实运行链路上通过同一组公共契约测试后，才抽出版本化 `agent-kernel`；候选范围是任务、技能生命周期、证据与不确定结果、权限、Provider 和评测契约，不包含本体控制/安全动作与物理状态形状。

**理由**：本仓以桌面操作场景演进，drone-agent 以持续飞行与空地任务交接演进。控制停止、取消、恢复、世界状态形状不能仅按同名概念共用。drone-agent M0 的契约与 wire 测试是准备性证据，尚不等于两个场景共同验证完成；因此 D006 的触发器已评估，但抽库条件尚未满足。

**替代方案**：立即抽共享内核（抽象未经两个运行场景检验）；将无人机直接并入本仓控制与安全模块（领域语义耦合）。两者均不采用。

**重估触发器**：两项目建立并通过同一组公共契约测试，同时保留各自本体评测基线；或再出现新的本体消费者。对应决策与迁移边界见 [drone-agent D001](https://github.com/SuperdeMan/drone-agent/blob/main/docs/decisions.md#d001--独立仓库复用姊妹项目采用复制改造抽公共内核有明确触发器) 与 [复用清单](https://github.com/SuperdeMan/drone-agent/blob/main/docs/reuse-from-embodied-agent.md)。本条只补充重估结论，不修改本仓执行代码或历史评测结果。

D027 验收补记（2026-11-04）：用户明确授权 v2 迁移与部署，`f27d0ac` 已在云端激活。停写后的 v1 备份与升级后旧业务内容摘要一致，新增任务、重启和外部访问后旧行仍完整。749 项测试与三组原任务回归见 `eval/task-recovery-20261104.json`，云端证据见 `eval/cloud-task-upgrade-20261104.json`。该交付不关闭自动续跑、多 attempt 或真机恢复门禁。


## D028 · 持久化故障不能中断取消与监督闩锁

**日期**：2026-11-04 · **状态**：实施中

**问题**：`cancel_all` 在首个账本异常后退出；ControlState.halt 在该调用之后才闩锁 Guard。组合故障可能令后续许可和驱动闩锁遗漏。

**决策**：批量取消分为同步全量撤权/取消、逐项持久化两阶段；单项读写失败仍取消协程并保留隔离；控制闩锁及驱动 Guard 先于数据库操作。批量错误按操作 ID 与异常类型报告并记录，监督循环继续运行。调用未知、停止未知与持久化失败分别表达，不把内存撤权当作 durable 或 physical stop。

**边界**：不新增表或 wire 字段，不引入自动续跑/重试/解除隔离；真实介质故障、父进程原生阻塞、跨机身份与真机停止继续独立验收。最小扩展现有取消链，避免新增第二套停止控制器。

D028 验收补记（2026-11-04）：`c1fc926` 已部署，757 项测试、28 项 RPC 与 120 项参考故障矩阵通过；Linux 实际 SQLITE_FULL 下先全量撤权、后尝试持久化，重启保持 UNKNOWN。原 v2 业务/任务日志逐行保留，未新增 schema 或开放自动恢复。准确范围见 `eval/cancellation-faults-20261104.json` 与 `eval/cloud-cancellation-upgrade-20261104.json`。


## D029 · 一致的恢复审查快照，不把确认标志当身份认证

**日期**：2026-11-04 · **状态**：实施中

**问题**：D027 评估逐步等待效果资格检查，期间场景/账本可能改变，较早的结果可能已过期；现有确认布尔值也不提供受信操作者身份。

**决策**：引入纯类型恢复审查契约和接收端变更指纹，在异步边界及返回前复核，变化则整体失效，最后再次扣除等待时间。返回操作身份、请求摘要及明确拒绝授权原因；缺失认证主体时永不授予执行权限。摘要不是授权凭据，不接受客户端自报主体或权限。

**边界**：只读审查协议，无新 schema、无新事件类型、无执行续跑/多 attempt；既有确认式停止对账保持原边界，不被此次审查升级成跨机认证。下一步需实现经过部署评审的身份适配与一次性审计授权，才能设计可执行恢复接口。

D029 验收补记（2026-11-04）：`a29ad0e` 已部署，787 项测试、38 项 RPC 和 150 项参考矩阵通过。Linux 仿真证明场景变化与 Guard 急停复位使评估整体失效，查询无写入。原 v2 七表历史逐行保留；本批没有受信身份适配或执行授权消费，不视为恢复权限全面完成。证据见 `eval/recovery-review-20261104.json` 和 `eval/cloud-review-upgrade-20261104.json`。


## D030 · 复用受控 Serve 身份，仅签发一次性效果重查授权

**日期**：2026-11-04 · **状态**：实施中

**决策**：在显式策略启用的 HTTP 操作者入口，校验可信代理实际 peer、HTTPS origin、单一 Serve 用户头及精确 allowlist。绑定主体摘要、policy、task/operation/attempt/request、账本 boot 和 D029 审查状态，签发短时一次性效果重查 challenge。消费意图先持久化，再调用既有只读重查，结束审计单独记录；所有失败不重试。

**边界**：不新增密钥、不修改 Tailnet 策略、不开放新宿主端口、不新增数据库表；新增 operation_events 类别兼容旧读取器。只授权效果重查，不授权物理停止、复位、任务执行或重试。认证主体不由 JSON/LLM 提供；宿主直连属于受信宿主边界，非白名单用户及 tagged 设备均拒绝。旧参考 RPC 保持现有边界，本批不宣称它已接入此身份协议。

**依据**：Tailscale Serve 官方 identity headers 文档（2026-11-04 核对）说明 Serve 覆盖客户端同名头、tagged/Funnel 不提供用户头，并要求限制后端直连。当前部署只有本项目容器处于 internal 网络，可信代理取该网络实际网关，策略单独只读挂载。


## D031 · 关节/夹爪反馈与语义采样分离

**日期**：2026-11-04 · **状态**：实施中

**问题**：D030 的 e1b2706 原 color 回归为 29/30，第 30 局 semantic_target_stale；动作原语仅取 qpos/gripper/末端方向却调用完整感知 read，额外检测会消耗原目标 TTL。更早 be8cc7d 两次完整测试 color 抓取失败缺少详情，不能据此声称根因相同。

**决策**：原语改用已有 read_control_feedback；物体感知仍在技能阶段显式采样，闭合后的附着更新由技能在抬升前显式执行。保持全部语义有效期、求解误差、轨迹与 Guard 参数，不重试旧动作、不延长旧观察寿命。新增测试证明只取本体字段不调用视觉，并保留真实过期目标拒绝。

D030/D031 验收补记（2026-11-04）：`5b080dd` 已部署。822 项完整测试、38 项 RPC、185 项参考矩阵通过；真值/color 各 30/30，ONNX 29/30。Serve 身份覆盖、只读策略挂载、一次消费审计及重启失效均经真实入口验证，原七表历史保留；只授权效果重查。be8cc7d 两次无详情失败及 e1b2706 color 29/30 保留，未宣称完全同因。证据见 `eval/operator-authorization-20261104.json` 与 `eval/cloud-operator-upgrade-20261104.json`。


## D032 · 显式作用域隔离下的一次性受控停止授权

**日期**：2026-11-22 · **状态**：生效（受控 HTTP 与私网仿真已验收，见 [stop-authorization.md](stop-authorization.md)）

**问题**：D030 仅在受控 HTTP 入口开放 `reconcile_effect`，而 `OUTCOME_UNKNOWN / STOP_UNCONFIRMED` 操作在确认停止前会隔离物理资源并阻断效果重查与后续任务；现有 `reconcile_stop` 仅有未认证的 Python/本机 RPC `confirmed=True` 路径，缺少代理身份、显式作用域边界、审查快照绑定和主体审计。

**决策**：在 `OperatorPolicy` 中增加可选 `scopes`（仅允许 `reconcile_effect` 与 `reconcile_stop`，默认仅 `reconcile_effect`），避免旧策略升级后静默扩权。复用 D030 的 Serve 代理身份、Origin/Host 校验、一次性内存 challenge 与 schema v2 `operation_events` 审计（无 DDL 迁移），并把 `stop_profile_digest`、`_stop_fence` 与在途停止状态纳入 `review_state` 指纹。`reconcile_stop` 在签发和消费前共享 `SkillRegistry` 停止准入检查，拒绝已停止、非未知、未跟踪或工作线程未退出的操作；允许跨重启中断任务与 Guard 已闩锁状态下的停止观测，但绝不清除 Guard 闩锁、绝不把 `OUTCOME_UNKNOWN` 改为成功、绝不授权重放或续跑。停止观测进行期间，控制台场景渲染主动返回不可用以避免争用仿真状态锁。

**替代方案**：把 `reconcile_stop` 合并进泛化“恢复”作用域或默认随旧策略开放（违反最小权限与显式分权，拒绝）；在 HTTP 直接透传客户端自证的停止证据或允许顺带复位 Guard（破坏安全隔离，拒绝）；放宽 `reconcile_effect` 使其绕过停止确认或跨重启生效（拒绝）。

**边界**：本批仅打通单机受控 HTTP 到现有 MuJoCo `SimStopController` 的受信停止对账；不改变本机参考 RPC 的未认证边界，不增加数据库表，不开放 Guard 复位、多 attempt、自动续跑或真机停止证明。跨主机控制身份、父进程物理控制隔离和真机停止控制器仍须独立验收。

D032 验收补记（2026-11-22）：`90135f0` 已部署激活（回执 `20261122T012458Z`）。859 项完整测试、38 项 RPC、10 组 222 项参考矩阵全通过；真值/color 各 30/30，ONNX 29/30。Linux 隔离容器验证缺省策略拒止停止、保夹持停止、跨重启停止和 Guard 闩锁保持；真实 Serve HTTPS 8449 验证 WSS 中断隔离、停止拒绝/确认与 5 事件审计行序（114→118）、隔离解除后恢复执行及重启七表行摘要一致、旧凭据 409。未改 schema，未授权 Guard 复位或任务续跑。证据见 `eval/stop-authorization-20261122.json` 与 `eval/cloud-stop-upgrade-20261122.json`。


## D033 · 控制与监督 RPC 启用 mTLS 证书固定与显式角色隔离

**日期**：2026-11-22 · **状态**：生效（本机与 Linux 容器三进程 mTLS 真栈已验收，见 [rpc-mtls-authorization.md](rpc-mtls-authorization.md)）

**问题**：`ControlService` 与 `SafetyService` 此前仅通过明文回环端口运行，任意可达客户端均可伪造 `SupervisorLink`、调用 `Reset` 解除急停，或仅凭布尔值 `confirmed=True` 触发 `ReconcileStop` / `ReconcileEffect`，缺少传输认证、角色分离与主体审计。

**决策**：在不新增第三方依赖、不修改 proto wire、不修改 SQLite schema v2 的前提下，引入纯类型 `RpcAccessPolicy` 与 `RpcTlsPaths`（`embodied.dev/v1alpha1`）。使用 `grpc` 原生 BoringSSL mTLS（`require_client_auth=True`）及标准库 `ssl.PEM_cert_to_DER_cert` 计算根 CA 与叶证书 SHA-256 指纹固定；非回环监听与非回环客户端连接强制要求 mTLS + 策略。在服务端硬编码 `agent` / `guardian` / `operator` 角色方法矩阵，操作者再按最小权限显式配置作用域（`read_only`、`cancel`、`reconcile_effect`、`reconcile_stop`、`halt`、`reset_guard`），禁止 `agent`/`guardian` 复位 Guard 或调用维护对账，禁止非 `guardian` 接入 `SupervisorLink`。认证维护操作（`ReconcileEffect` / `ReconcileStop`）先落账 `rpc_operator_action_started` 再执行并在结束后落账 `rpc_operator_action_finished`；`Halt` / `CancelOperation` 继续维持 D028“撤权与闩锁先于数据库”不变式。

**替代方案**：使用静态共享 bearer token 或环境变量密钥放进 gRPC metadata（易泄漏、无双向传输身份、需额外密钥管理，拒绝）；仅核对证书 CN 字符串而不固定叶证书 SHA-256 指纹（CA 签发同名证书即可绕过，拒绝）；新增 `cryptography` 依赖（无必要增加运行依赖，拒绝）；允许非回环明文监听并仅靠防火墙保护（违反纵深防御，拒绝）。

**边界**：本批关闭本机/跨机参考 RPC 的未认证与角色混用缺口，但尚未引入在线证书吊销（CRL/OCSP）、自动证书轮换、跨机视觉传感器时钟同步、父进程物理控制隔离或真实硬件急停；回环无策略模式仅用于既有本地参考开发与回归，不宣称为已认证边界。

D033 验收补记（2026-11-22）：`25343fa` 已验收。ruff、878 项完整测试、45 项 RPC 测试及 11 组 241 项参考故障矩阵全通过；真值/color 各 30/30，ONNX 29/30。Linux Docker 候选镜像（`sha256:3bc844ad...`）及三进程 BoringSSL mTLS 实测探针（`embodied-rpc-test:25343fa08480`）均通过，确认非回环明文拒止、CA/叶证书指纹固定、角色方法隔离、agent 断连触发 mTLS 急停、CLI mTLS 停止对账精确 4 事件审计序与 `Reset` 作用域边界。单进程 HTTP 控制台未变更，云端常驻维持已验收 `90135f0`。证据见 `eval/rpc-mtls-authorization-20261122.json`。


## D034 · 物理控制进程隔离与原生挂起/崩溃回收

**日期**：2026-11-22 · **状态**：生效（本机、Linux 容器 19 项实测与云端私网控制台均已验收，见 [physical-control-isolation.md](physical-control-isolation.md)）

**问题**：D026 已将检测、ONNX 与渲染迁入纯计算子进程，但 MuJoCo 物理仿真（`mj_step`、逆解、`SimStopController` 与驱动写路径）仍运行在父进程（`embodied console` / `embodied serve-control` / `embodied sim`）内。一旦物理原生调用挂起、长期持 GIL 或发生原生崩溃，会直接锁死或杀死承载 `SkillRegistry`、SQLite 账本与 HTTP/gRPC 控制面的父进程，导致无法响应取消、`Halt`、状态查询或回收受阻工作线程。

**决策**：新增协议 `proto/embodiedrpc/control_worker/v1/control_worker.proto` 与纯契约 `src/embodied/contracts/control_process.py`（无新增运行依赖、无 SQLite schema 变更、不改现有 `control.proto`/`safety.proto`/`compute.proto` wire）。实现独立物理控制子进程 `control_worker`、监督器 `ControlProcess` 与隔离本体适配器 `IsolatedTabletopSim`，将 MuJoCo 物理步进、逆解、驱动侧 `Guard` 与 `SimStopController` 全部迁入独立无头子进程，同时保留 D026 独立 `RenderBackend` 子进程承接离屏渲染与 JPEG 编码。父进程维护镜像 `Guard`、资源仲裁与 `ActionAuthority`：原生挂起、崩溃、坏协议或 `Halt` 遇卡死时，先同步闩锁父 `Guard`（D028），再在有界时间内终止并回收物理子进程树，置 `FAILED` 且不自动重启、不重放旧动作；仅允许在空闲且已回收后显式调用 `restart_physical_control()` 启动新代次并轮换 `controller_boot_id`，且维持 `Guard` 闩锁与未知操作隔离。应用入口（`console`、`serve-control`、`sim`）默认启用物理控制隔离，保留 `--in-process-sim` 与直接 `TabletopSim` 作为数值对照和单测夹具。

**替代方案**：把物理仿真和离屏渲染合并到同一个子进程（Mesa/OpenGL 渲染故障会连带击垮物理状态并违背 D026 隔离成果，拒绝）；直接在父进程用线程超时放弃原生 MuJoCo 线程（Python 无法强杀原生/持 GIL 线程，锁与内存均无法回收，拒绝）；子进程崩溃后静默自动重启并继续执行（破坏未知结果隔离与 `controller_boot_id` 边界，拒绝）；引入 ZeroMQ/共享内存新依赖（违反最小依赖原则，拒绝）。

**边界**：本批关闭仿真物理控制器原生挂起/崩溃对父进程控制面的连带失效，并支持经 D033 mTLS 监督链路触发对卡死仿真控制进程的强杀回收；不宣称 OS 文件/网络强沙箱，不代替真实机器人独立硬件急停（SG-0）、跨机视觉时钟同步或多 attempt 恢复（仍需 schema v3 审批）。

D034 验收补记（2026-11-22）：`cb05aee` 已验收并部署激活（回执 `20261122T042559Z`）。ruff、897 项完整测试、46 项 RPC 测试及 12 组 260 项参考故障矩阵全通过；默认启用物理控制隔离下的 3×30 回归保持真值/color 各 30/30、ONNX 29/30。Linux Docker 候选镜像（`sha256:f4c5a494...`）通过控制台验收、`compute_smoke.py` 与 `control_smoke.py`（`urgent_estop` 0.130s 强杀 Linux GIL 挂起子进程），独立测试镜像（`sha256:cf445026...`）在 Linux 容器内通过全部 19 项 D034 + BoringSSL mTLS 集成测试。云端常驻控制台升级保留 schema v2 原七表历史行（19→20 tasks / 34→36 ops），实测容器内 PID 1 / 8 / 25 三进程分离、抓放、重启持久性与真实 HTTPS 8449 Serve 身份及双作用域全部通过，未宣称 SG-0 真机急停。证据见 `eval/physical-control-isolation-20261122.json` 与 `eval/cloud-physical-isolation-upgrade-20261122.json`。


## D035 · P1 可观测延迟、过期动作率与 Episode 增量崩溃恢复基线

**日期**：2026-11-22 · **状态**：生效（本机 Windows 与 Linux Docker 容器均已验收，见 [p1-metrics-baseline.md](p1-metrics-baseline.md)）

**问题**：既有 `p1_fault_matrix.py` 仅输出测试用例运行时间，缺少针对停止延迟（Guard 闩锁、挂起物理子进程强杀、仿真停止观测）、过期动作拦截率、人工介入率与 episode 数据损失率的专门计量；同时 `EpisodeWriter` 仅在 `finish()`/`abort()` 时一次性写出 `steps.npz`，若进程中途遭遇硬杀（`SIGKILL`/`os._exit`），已采步骤 100% 丢失。

**决策**：新增纯契约 `src/embodied/contracts/metrics.py`（`embodied.p1-metrics/v1`）与轻量采样模块 `src/embodied/runtime/metrics.py`；在 `ActionAuthority`、`SkillRegistry` 与 `IsolatedTabletopSim` 挂载零额外依赖的内存计数与延迟采样（不改 SQLite schema v2、不改动作/停止协议或物理参数）。升级 `EpisodeWriter` 增加按 `checkpoint_every`（默认 10 步）增量刷盘的 `steps.journal.jsonl`、最终 `steps_sha256` 完整性摘要、`audit_episode()` 篡改/损坏审计与 `recover_episode()` 硬杀恢复（容忍截断尾行，向后兼容 `v0` 目录布局与 LeRobot 转换）。新增可重复提交绑定基准脚本 `scripts/p1_metrics_baseline.py`，同时在 Windows 本机与 Linux Docker 隔离容器采集分项延迟、100% 过期拦截与硬杀数据损失率，并将对应契约测试纳入 P1 故障矩阵第 13 组。

**替代方案**：把指标直接写入 SQLite 新表（触发未授权的 schema 变更且增加控制路径存储耦合，拒绝）；用第三方 Prometheus/OpenTelemetry SDK（违背最小依赖原则，拒绝）；改用 MCAP 重写 v0 录制格式（破坏既有 `load_episode` 与 LeRobot 转换契约，留待后续需要多模态流容器时单独评审，拒绝）。

**边界**：本批测量的是隔离仿真进程下的软件闩锁延迟、子进程强杀回收延迟、仿真停止观测耗时与本地文件增量持久化损失界限，`hardware_stop_measured` 固定为 `false`、`p1_complete` 固定为 `false`；真实硬件急停延迟、真实介质掉电与现场多模态录制仍保留为真机/现场门禁。

D035 验收补记（2026-11-22）：`b723417` 已验收。ruff、904 项完整测试、46 项 RPC 测试及 13 组 267 项参考故障矩阵全通过（零跳过）；默认物理控制隔离下 3×30 回归保持真值/color 各 30/30、ONNX 29/30。Windows 本机（`guard_estop_latch` p95 `0.0090 ms`、`urgent_estop_kill` p95 `138.25 ms`、`stop_reconciliation_wall` p95 `11.11 ms`）与 Linux Docker 隔离容器镜像 `sha256:0ed21bd5...`（`0.0128 ms` / `130.85 ms` / `51.71 ms`，宿主机 40 容器不变）均录得仿真停止观测 `100.0 ms`、100% 过期与重放动作拦截、正常录制 0% 损失及 `os._exit(9)` 硬杀 50/57 步恢复（损失 `7 < 10`，容忍截断尾行并检出篡改）；`hardware_stop_measured=false`、`p1_complete=false`。线上 HTTP/WS 控制面与 schema v2 未变，云端常驻控制台维持已激活 `cb05aee`。证据见 `eval/p1-metrics-baseline-20261122.json`。


## D036 · 跨机视觉/观察时钟契约与 RPC 对象级时效传播

**日期**：2026-11-22 · **状态**：生效（本机 Windows 与 Linux Docker 容器 35 项 mTLS 视觉时钟实测均已验收，见 [cross-host-observation-clock.md](cross-host-observation-clock.md)）

**问题**：D019 远端 `ObservationEnvelope` 仅支持即时 `sim_truth`，既无法表达跨主机视觉观察的时钟域与偏斜/漂移界限，也无法传递 D023 逐对象感知年龄；同时 `PerceivedSim` 未覆写 `read_reference_state()`，通过 `__getattr__` 穿透到底层仿真直接泄漏真值，导致远端规划与效果验证器无法在感知遮挡、相机故障或网络延迟时收口为 `UNKNOWN`。

**决策**：在不新增第三方依赖、不破坏 `control.proto` wire、不修改 SQLite schema v2 的前提下，扩展 `src/embodied/contracts/observation.py`（新增 `ClockMetadata`、`RemoteObjectEvidence`、`ClockAdmissionPolicy` 并向后兼容演化 `ObservationEnvelope`）与 `src/embodied/runtime/world_state.py`（升级 `SnapshotAdmission`）：强制禁止跨主机使用 `local_monotonic`；对 `wall_ntp`/`wall_ptp` 执行同步状态、偏斜/漂移预算、未来时间戳、本地墙钟跳变与 Cristian 中点往返偏斜反查；对 `simulation`/`rpc_rtt_bounded` 采用生产端自测年龄加接收端单调 RTT 与不确定度构造保守鲜度上界。在 `PerceivedSim` 显式实现 `read_reference_state()`、在 `Observation` 增加可选 `object_clock_now`、在 `ControlService.GetWorldState` 与 `embodied serve-control --perception` 透传感知对象证据；接收端剔除已超期对象并将新鲜对象的剩余 TTL 重基准到接收端本地单调时钟。将跨机时钟契约与 mTLS 感知 RPC 故障测试纳入 P1 故障矩阵第 14 组。

**替代方案**：直接把生产端 `time.monotonic()` 放进 RPC 供接收端相减（跨主机单调时钟无共同零点，必然出错，拒绝）；仅依赖远端自报 UTC 墙钟而不做偏斜上界与 Cristian 往返交叉检验（远端时钟失步或恶意/异常超前会绕过过期门禁，拒绝）；把对象鲜度校验移到 SQLite 新表或修改 gRPC proto 字段破坏旧客户端（触发未授权 schema 变更且无必要，拒绝）。

**边界**：本批关闭软件层跨机视觉时钟契约、时钟偏斜/跳变拒止与 RPC 对象级时效传播；不替代真实物理相机断连、真实硬件 PTP/NTP 同步测量或真机手眼标定验证。

D036 验收补记（2026-11-22）：`c549839` 已验收。ruff、924 项完整测试（+20）、49 项 RPC 测试（+3）及 14 组 287 项参考故障矩阵全通过（零跳过、`p1_complete=false`）；默认物理控制隔离下 3×30 回归保持真值/color 各 30/30、ONNX 29/30。Linux Docker 候选镜像（`sha256:cb2775cf...`）通过控制台冒烟、`compute_smoke.py`、`control_smoke.py` 与 `p1_metrics_baseline.py`，独立测试镜像 `embodied-vision-rpc-test:c549839dec3c`（`sha256:b6c6820d...`）在真实 Linux OSMesa 与高运行单调时钟下通过全部 35/35 项跨机时钟与 BoringSSL mTLS 视觉 RPC 测试（含隔离物理 + ColorBlob 感知与 reset 轮换），宿主机 40 个既有容器不变。线上 HTTP/WS 控制面与 schema v2 未变，云端常驻维持已激活 `cb05aee`。证据见 `eval/cross-host-observation-clock-20261122.json`。


## D037 · ReleaseBundle/ModelBundle 兼容预检与 v2 账本降级恢复演练

**日期**：2026-11-22 · **状态**：生效（本机 Windows 与 Linux Docker 双镜像真实降级/受损恢复演练均已验收，见 [release-compatibility.md](release-compatibility.md)）

**问题**：P1 故障矩阵仍留有“v2 账本产生新写入后的发行兼容与降级/恢复演练”门禁；此前虽有 `task_schema.py` 处理 v1→v2 迁移，但缺少 `ReleaseBundle` 与 `ModelBundle` 冻结契约、维护态激活/回滚预检（阻止错误模型语义、资产篡改、活跃操作或未验证回滚组合），以及受损 v2 账本的认证备份原子恢复工具与双镜像演练证据。

**决策**：在不新增第三方依赖、不修改 SQLite schema v2、不修改 gRPC proto 的前提下，新增纯契约 `src/embodied/contracts/release.py`（`ModelBundle`、`TargetEnvironment`、`ReleaseBundle`、`LedgerBackupManifest`、`ReleasePreflightReport`）与运行时模块 `src/embodied/runtime/release.py`（CLI `embodied release-check`）：对模型执行本体/关节序/动作语义/后端/权重哈希全维度校验（相同 shape 不等于物理兼容）；对激活与回滚强制校验独占账本锁、零 `RUNNING` 操作、零 `STOP_UNCONFIRMED` 尝试、schema 区间匹配、资产与停止 profile 摘要及显式 `verified_rollback_targets`（未经验证组合不得标 `rollback_safe=true`）；提供全七表确定性摘要、`backup_ledger()` 在线备份与 `restore_ledger_backup()` 认证原子恢复。将兼容与降级恢复契约纳入 P1 故障矩阵第 15 组，并在 Linux Docker 隔离容器中用真实新版镜像与前序 `cb05aee` 镜像演练新 v2 写入后的受控降级与受损账本恢复。

**替代方案**：允许仅凭张量维度匹配就加载模型或允许任意版本间默认自动回滚（易导致错误关节映射和静默数据损坏，拒绝）；用文件拷贝 `cp` 直接备份或覆盖正在运行的 SQLite 数据库（易造成半页损坏与锁竞争，拒绝）；顺带升级为 schema v3（触发未授权的数据库 schema 变更红线，拒绝）。

**边界**：本批关闭仿真/容器层面的 v2 发行兼容预检、双镜像受控降级与认证备份原子恢复门禁，`hardware_verified` 固定为 `false`；真实硬件固件 OTA 与未来跨大版本 schema（v3→v2）降级仍保留为独立门禁。

D037 验收补记（2026-11-22）：`75853f5` 已验收。ruff、936 项完整测试（+12）、49 项 RPC 测试及 15 组 299 项参考故障矩阵全通过（零跳过、`p1_complete=false`）；默认物理控制隔离下 3×30 回归保持真值/color 各 30/30、ONNX 29/30。Linux Docker 候选镜像（`sha256:a2a364d9...`）通过控制台冒烟、`compute_smoke.py`、`control_smoke.py` 与 `p1_metrics_baseline.py`，并在隔离容器完成真实双镜像（`75853f5` ↔ `cb05aee` `sha256:f4c5a494...`）演练：新版本写入全七表（摘要 `d5ddfe05...`）后拒绝 v1-only 降级与未验证回滚、放行已验证 `cb05aee` 回滚并在 `cb05aee` 中逐字节一致续写第二局任务（`1→2 tasks / 2→4 ops`），随后故意截断主库并注入垃圾 `-wal` 后经认证备份原子恢复至精确全七表摘要 `d5ddfe05...`，宿主机 40 个容器未变。线上 HTTP/WS 控制面与 schema v2 未变，云端常驻维持已激活 `cb05aee`。证据见 `eval/release-compatibility-20261122.json`。
