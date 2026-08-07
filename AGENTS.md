# AGENTS.md — embodied-agent

> 本文件面向所有 AI 编码代理（Claude Code、Codex、Gemini CLI 等）。**项目规则的单一事实来源是 [CLAUDE.md](CLAUDE.md)**，本文件只做入口与摘要；两者冲突时以 CLAUDE.md 为准，改规则先改 CLAUDE.md。

## 必读顺序

1. [CLAUDE.md](CLAUDE.md) — 项目规则全文（阶段状态、目录约定、命名、验证、红线）
2. [docs/architecture.md](docs/architecture.md) — 架构单一事实来源；架构级变更**先改文档再动代码**
3. [docs/roadmap.md](docs/roadmap.md) — 阶段任务与退出标准
4. [docs/decisions.md](docs/decisions.md) — 技术决策记录，只增不删

## 最低纪律（摘要，全文见 CLAUDE.md）

- **验证**：任何代码改动后 `uv run ruff check .` + `uv run pytest -q` 必须绿；抓取/策略参数改动必须跑 `uv run --group sim embodied sim --task eval/tasks/tabletop_pick_place_v1.yaml` 确认基线不回退。
- **语言**：文档中文；代码、标识符、注释、commit message 英文（Conventional Commits）。
- **安全红线**：LLM 只产出计划与技能调用，永不直接触达执行通道；Safety Guardian 不依赖任何 AI 输出；危险动作 `require_confirm`。契约测试不许跳过或放宽。
- **密钥**：API key 不进代码/commit/日志；严禁复制 car-agent 的 `.env`。
- **网络**：uv 拉包用每次命令的环境变量走镜像：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`，不写入全局配置。
- **路径**：仓库路径含中文，原生库（MuJoCo 等）加载文件前需复制到 ASCII 临时目录（见 `src/embodied/control/simassets.py`）。
- **术语**：vehicle/cockpit/座舱等 car-agent 领域词不得出现在本仓库代码中（契约测试扫描）。
