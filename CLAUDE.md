# CLAUDE.md — embodied-agent

具身智能体项目：跨本体的「大脑—小脑—本体」分层运行时，桌面操作机器人场景起步，仿真先行。架构与理由见 `docs/architecture.md`（单一事实来源），阶段与退出标准见 `docs/roadmap.md`。

## 当前阶段

**当前进度（2026-11-29）**。P1 仿真与跨主机单步恢复已验收。Windows 上 M2 双学习抓放在原任务的真值与 ColorBlob 两种配置各 30/30，Linux 单核的真值原任务和保留集各 30/30。Linux 相机闭环和 P2 现场设备仍待验收。证据见 `docs/learned-pick-residual.md`。优先级见 `docs/roadmap.md`，各批次退出条件见 `docs/platform-upgrade.md`。脚本、感知、策略与部署证据按提交和运行配置分别记录。

本地开发组：`uv sync --group sim --group rpc --group learn --group policy --group perceive`。抓取/策略与动作写路径变更须复测原任务；权重、数据和录制不进 Git。本轮 commit/push、D018 新 SQLite 五表、D027 两表追加及 v2 云端迁移部署、nightly 注释补齐已获用户明确授权；后续不同范围的敏感变更仍按红线确认。

云端私网仿真控制台已激活 `723cc80`（2026-11-29），HTTPS 8449、隔离物理控制、离线规划、模拟音频。`463199e` 完成 v3 迁移，`77b754d` 验收了空闲连接单次发送文本，`723cc80` 为缺帧保留最近收到的画面并标记时间。最新只读验收前后八表 SHA-256 完全一致，23 个任务、42 个操作和 42 次尝试。常驻操作者仅有 `reconcile_effect` 与 `reconcile_stop` 权限，`retry_step` 在独立授权的跨主机 mTLS 仿真链路验收。入口与证据见 `docs/cloud-deployment.md` 及 `docs/roadmap.md`。

开发主线 D021–D025 已接入仿真停止对账、统一动作准入、对象时效、持久效果与有界观察。D026 接入纯计算进程隔离（`docs/isolated-compute.md`）：检测、ONNX 与状态副本渲染不接收执行句柄，失败不重发。D027 已验收计划/意图/回执日志、只读恢复评估与授权的 schema v2 云端迁移（`docs/task-recovery.md`）；D028 已修复存储故障中断批量取消/Guard 闩锁的问题，P1 参考故障矩阵 120 项通过（`docs/p1-fault-matrix.md`）；D029 已接入一致恢复审查快照、Guard 生命周期失效及明确的权限拒绝契约（`docs/recovery-review.md`），参考故障矩阵扩展为 150 项；D030 已在受控 HTTP 入口接入 Serve 身份与一次性效果重查审计（`docs/operator-authorization.md`），D031 分离本体反馈与语义采样（`docs/body-feedback.md`），参考矩阵 185 项通过；D032 已在受控 HTTP 入口接入显式作用域隔离与一次性 `reconcile_stop` 审计授权（`docs/stop-authorization.md`，缺省仍仅 `reconcile_effect`，不清除 Guard 闩锁），参考矩阵扩展至 10 组 222 项通过；D033 已为 `ControlService`/`SafetyService` 接入固定 CA/叶证书 SHA-256 的 BoringSSL mTLS、非回环明文拒止、硬编码角色方法矩阵与维护审计（`docs/rpc-mtls-authorization.md`），参考矩阵扩展至 11 组 241 项并通过 Linux Docker 三进程 mTLS 真栈验证；D034 已将 MuJoCo 物理仿真、逆解、驱动 Guard 与 `SimStopController` 迁入独立受监管子进程（`docs/physical-control-isolation.md`），与父控制面及 D026 渲染子进程三进程分离，参考矩阵扩展至 12 组 260 项并通过 Linux Docker 19 项实测（含 mTLS Guardian Halt 强杀持 GIL 挂起子进程）及云端控制台激活（`cb05aee`）；D035 已验收仿真 P1 可观测延迟、100% 过期/重放动作拦截、人工介入率与 `EpisodeWriter` 增量崩溃恢复基线（`docs/p1-metrics-baseline.md`，`b723417` 通过本机 904 项测试、13 组 267 项参考矩阵与 Linux Docker 候选容器实测，硬杀损失严格 `< 10` 步且向后兼容 `v0`/LeRobot，云端常驻控制台维持 `cb05aee`）；D036 已验收跨机视觉/观察时钟契约与 RPC 对象级感知时效传播（`docs/cross-host-observation-clock.md`，`c549839` 通过本机 924 项测试、49 项 RPC、14 组 287 项参考矩阵与 Linux Docker 35 项 BoringSSL mTLS + OSMesa ColorBlob 实测，禁止跨机 `local_monotonic`、执行 `wall_ntp`/`wall_ptp` 偏斜与 Cristian 中点反查、阻断 `PerceivedSim` 真值泄漏并将剩余 TTL 重基准至接收端单调时钟，云端常驻控制台维持 `cb05aee`）；D037 已验收 `ReleaseBundle`/`ModelBundle` 兼容预检与 v2 账本降级恢复演练（`docs/release-compatibility.md`，`75853f5` 通过本机 936 项测试、49 项 RPC、15 组 299 项参考矩阵及 Linux Docker 双镜像 `75853f5` ↔ `cb05aee` 真实 v2 写入降级与受损账本认证恢复演练，云端常驻控制台维持 `cb05aee`）。以上为 D037 及更早批次的验收历史，后续运行状态见上段。

## 目录结构

完整结构定义与理由见 `docs/architecture.md` §10，本节只列约定要点：

- `docs/` — 设计文档。架构级变更**先改 `architecture.md` + `decisions.md` 增条目，再动代码**；`decisions.md` 只增不删。
- `src/embodied/` — 单包多子模块（hri/cognition/skills/control/safety/data_engine/providers/runtime），不搞多包微服务仓库。
- `proto/` — 进程间契约，proto 先行：改跨进程接口必先改 proto 再生成代码。
- `assets/` — 模型与场景文件；**权重、数据集、录制数据不进 git**，只放来源与版本指针（README 记录获取方式）。
- `experiments/` — 一次性实验脚本，禁止被 `src/` 依赖；超过 30 天未引用可清理（删除前仍须按全局红线先问）。
- 新增顶层目录属于架构变更，走文档先行流程。

## 命名

- 目录与 Python 模块 `snake_case`；文档文件 `kebab-case.md`；技能名 `skill.<domain>.<action>`（如 `skill.manip.pick`）；proto 包 `embodied.<service>.v1`。
- 从 car-agent 移植的文件，文件头必须标注：`# Ported from car-agent <path> @ <commit>, changes: <summary>`。

## 语言与风格

- 文档中文；代码、标识符和 commit message 英文；commit 用 Conventional Commits（`feat:`/`fix:`/`docs:`/`refactor:`/`test:`/`chore:`）。
- **代码注释统一中英双语**：源码、测试、脚本和文档字符串中的说明性注释必须同时提供中文与英文，表达同一含义；短注释可同行，长说明可相邻分段。已有单语注释逐项补齐，保留原有设计动机、事故说明与来源信息，不用无关套话凑双语。
- shebang、编码声明、lint/type/coverage 指令、协议保留字及自动生成代码不翻译或改写；必要说明另加双语注释。移植代码保留原注释并补对应译文，来源头的路径、SHA 与来源名称保持原样。
- 术语纪律：vehicle/cockpit/座舱等 car-agent 领域词不得出现在本仓库代码中。

## 验证

- 代码改动后必须绿：`uv run ruff check .` + `uv run pytest -q`。
- 契约测试（确认门禁 `test_registry_contract`、术语纪律 `test_no_forbidden_terms`、技能扩展缝）不许跳过、注释或放宽断言。
- learn/policy 组相关测试（LeRobot 转换器、ONNX 策略）在依赖缺席时自动跳过，CI 不覆盖——**本地装组后必跑**（与 tests/rpc 同规）。
- proto 变更后运行 `scripts/gen-proto.ps1`（或 `.sh`）确认编译通过；生成物在 `gen/`（不进 git）。进程拆分相关改动加跑 `uv run --group rpc pytest tests/rpc`（无 stubs/grpc 时自动跳过，CI 不覆盖，本地必跑）。
- 文档改动后自查相对链接有效、`roadmap.md` 阶段状态与实际一致。

## 已知环境约束

- **国内网络**：uv 拉包直连 PyPI 会超时，用每次命令的环境变量走镜像（不写入任何全局配置）：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`。
- **仓库路径含中文**（`产品/`）：MuJoCo 等原生库无法直接打开非 ASCII 路径下的文件，加载前需复制到 ASCII 临时目录（参考 `scripts/sim_smoke.py` 的 `_load_model` 回退）。新增涉及原生库文件加载的代码时注意同类处理。

## 工程红线（继承全局 CLAUDE.md，另加本项目条款）

- **安全架构红线**（详见 `docs/decisions.md` D009，只加严不放松）：LLM 只产出计划与技能调用，永不直接触达执行通道；S2S 语音会话唯一工具是 `escalate`；危险动作 `require_confirm`；Safety Guardian（SG-0~2）不得依赖任何 AI 输出的正确性。
- **真机纪律**（M3 起生效）：真机运行必须急停在手边、首跑降速、无人在场不运行；任何上真机的代码必须先过 sim 验证与 Safety Guardian 覆盖。
- **密钥**：API key 不进代码/commit/日志；**严禁复制 car-agent 的 `.env`**（含真实密钥），只参考其 `.env.example` 变量名。
- **依赖**：MuJoCo/LeRobot/openpi 等生态库的大版本升级属于架构级变更，先在 `decisions.md` 记录再升级。

## 节奏机制

- 季度技术雷达（1/4/7/10 月）：复查 `docs/architecture.md` §8.2 失效触发器，纪要存入 `docs/`。
- 评测驱动（M2 起）：策略与模型变更以版本化评测集数字说话，基线只能被数据推翻。
