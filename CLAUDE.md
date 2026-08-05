# CLAUDE.md — embodied-agent

具身智能体项目：跨本体的「大脑—小脑—本体」分层运行时，桌面操作机器人场景起步，仿真先行。架构与理由见 `docs/architecture.md`（单一事实来源），阶段与退出标准见 `docs/roadmap.md`。

## 当前阶段

**M1 仿真闭环，收尾（2026-08-05）**。已通：sim driver + 脚本技能（评测三种子 30/30）+ Safety Guard v0 + episode 录制 + planner 引擎（plan-execute-verify + 有界循环）+ 控制台 v0 语音链路（`embodied console`，keyless 可跑）。剩余：评测任务集版本化、三进程拆分、控制台真机浏览器人工验证。动手前先读 `docs/roadmap.md` 的 M1 任务与阶段进展；移植 car-agent 代码前先读 `docs/reuse-from-car-agent.md`。仿真代码涉及抓取参数改动时，先跑 `uv run --group sim embodied sim --eval 10` 确认不回退（当前基线 10/10，多种子 30/30）。

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
- **例外**：从 car-agent 移植的文件保留原有中文注释（它们承载事故复盘与设计动机，且保持与源文件可对照）；移植文件中新增/改写的注释与全部新写代码一律英文。
- 术语纪律：vehicle/cockpit/座舱等 car-agent 领域词不得出现在本仓库代码中。

## 验证

- 代码改动后必须绿：`uv run ruff check .` + `uv run pytest -q`。
- 契约测试（确认门禁 `test_registry_contract`、术语纪律 `test_no_forbidden_terms`、技能扩展缝）不许跳过、注释或放宽断言。
- proto 变更后运行 `scripts/gen-proto.ps1`（或 `.sh`）确认编译通过；生成物在 `gen/`（不进 git）。
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
