# 评测基线账本（append-only）

规则（docs/roadmap.md 评测驱动）：**基线只能被数据推翻**。每次正式跑版本化任务后追加一行；不删除、不改写历史行。策略/模型/抓取参数变更必须先跑对应任务确认不回退再合入。结果明细 JSON 在本地 `outputs/eval/`（不进 git），本账本记录结论与复现信息。

| 日期 | commit | 任务 | 结果 | 判定 | 备注 |
|---|---|---|---|---|---|
| 2026-08-05 | c368814 | tabletop_pick_place@v1 | 30/30 (100%) | PASS | 首条基线。脚本技能 + 真值感知 + 离线规划器；两段式下降修复后。运行方式：`uv run --group sim embodied sim --task eval/tasks/tabletop_pick_place_v1.yaml` |
| 2026-08-07 | 3f67945 | tabletop_pick_place@v1 | 30/30 (100%) | PASS | M2 管线合入后的回归确认（`held_object` 判定共享化重构涉及技能文件）。脚本技能路径无回退 |
| 2026-08-07 | 3f67945 | tabletop_pick_place@v1 | 29/30 (97%) | PASS | **首个学习策略**：ACT pick（60 专家 demos 按技能切分 16.7k 帧、4000 步 CPU、loss 0.077）经 ONNX 替换脚本 pick，place 仍脚本。唯一失败局 seed=1 pos=(0.013,-0.248)（深位 pick 超时）。运行：`uv run --group sim --group policy embodied sim --task eval/tasks/tabletop_pick_place_v1.yaml --pick-policy outputs/policies/pick-v1.onnx` |
| 2026-08-07 | 3f67945 | tabletop_pick_place@v1 | 29/30 (97%) | PASS | 双学习：pick+place 均 ACT/ONNX（place 3000 步、loss 0.060）。学习 place 在送达的 29 局全成；失败局与上行同一局（pick 深位超时）。距「不劣于脚本 30/30」差 1 局，路径：失败位补采示教/加练——基线只能被数据推翻 |
| 2026-08-08 | 2882181 | tabletop_pick_place@v1 | 30/30 (100%) | PASS | **感知驱动闭环**（D015）：agent 只见 ColorBlob+深度反投影的感知物体（多相机后备+可见度门+运动学附着信念，估计误差 ~2mm），裁判仍读真值。脚本技能 + 感知输入与真值基线打平。运行：`uv run --group sim embodied sim --task eval/tasks/tabletop_pick_place_v1.yaml --perception color` |
| 2026-08-08 | 2882181 | tabletop_pick_place@v1 | 29/30 (97%) | PASS | **全 AI 输入链**：感知物体（color）× 学习 pick（ACT/ONNX）+ 脚本 place。感知 ~2mm 噪声与 identity quat 未劣化策略表现（与真值输入 29/30 同分同失败局：seed=1 深位）。运行：追加 `--perception color --pick-policy outputs/policies/pick-v1.onnx` |
