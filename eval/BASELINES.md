# 评测基线账本（append-only）

规则（docs/roadmap.md 评测驱动）：**基线只能被数据推翻**。每次正式跑版本化任务后追加一行；不删除、不改写历史行。策略/模型/抓取参数变更必须先跑对应任务确认不回退再合入。结果明细 JSON 在本地 `outputs/eval/`（不进 git），本账本记录结论与复现信息。

| 日期 | commit | 任务 | 结果 | 判定 | 备注 |
|---|---|---|---|---|---|
| 2026-08-05 | c368814 | tabletop_pick_place@v1 | 30/30 (100%) | PASS | 首条基线。脚本技能 + 真值感知 + 离线规划器；两段式下降修复后。运行方式：`uv run --group sim embodied sim --task eval/tasks/tabletop_pick_place_v1.yaml` |
