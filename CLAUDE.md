# CLAUDE.md — embodied-agent

具身智能体项目：跨本体的「大脑—小脑—本体」分层运行时，桌面操作机器人场景起步，仿真先行。架构与理由见 `docs/architecture.md`（单一事实来源），阶段与退出标准见 `docs/roadmap.md`。

## 当前阶段

**M0 规范与骨架（2026-08）**。文档已齐备，代码尚未落地。动手前先读 `docs/roadmap.md` 对应阶段的任务清单与 DoD；移植 car-agent 代码前先读 `docs/reuse-from-car-agent.md` 的分级清单与迁移规矩。

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

- 文档中文；代码、标识符、注释、commit message 英文；commit 用 Conventional Commits（`feat:`/`fix:`/`docs:`/`refactor:`/`test:`/`chore:`）。
- 术语纪律：vehicle/cockpit/座舱等 car-agent 领域词不得出现在本仓库代码中。

## 验证

- **当前（文档阶段）**：改动后自查文档间相对链接有效、`roadmap.md` 阶段状态与实际一致。
- **代码落地后（M0 完成时补充具体命令）**：`uv run ruff check` + `uv run pytest` 必须绿；移植模块的测试随行迁移；契约测试（加技能不动内核、危险动作须确认、S2S 无执行通道）不许跳过或注释。

## 工程红线（继承全局 CLAUDE.md，另加本项目条款）

- **安全架构红线**（详见 `docs/decisions.md` D009，只加严不放松）：LLM 只产出计划与技能调用，永不直接触达执行通道；S2S 语音会话唯一工具是 `escalate`；危险动作 `require_confirm`；Safety Guardian（SG-0~2）不得依赖任何 AI 输出的正确性。
- **真机纪律**（M3 起生效）：真机运行必须急停在手边、首跑降速、无人在场不运行；任何上真机的代码必须先过 sim 验证与 Safety Guardian 覆盖。
- **密钥**：API key 不进代码/commit/日志；**严禁复制 car-agent 的 `.env`**（含真实密钥），只参考其 `.env.example` 变量名。
- **依赖**：MuJoCo/LeRobot/openpi 等生态库的大版本升级属于架构级变更，先在 `decisions.md` 记录再升级。

## 节奏机制

- 季度技术雷达（1/4/7/10 月）：复查 `docs/architecture.md` §8.2 失效触发器，纪要存入 `docs/`。
- 评测驱动（M2 起）：策略与模型变更以版本化评测集数字说话，基线只能被数据推翻。
