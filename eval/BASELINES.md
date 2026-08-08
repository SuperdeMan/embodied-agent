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
| 2026-08-08 | 6722ec9 | tabletop_pick_place@v1 | 17/30 (57%) | FAIL | **pick-v2 实验失败（负结果如实入账）**：120 episodes 含 33% 深位过采样、6000 步（5.7 epochs），loss 0.066 更低但失败集中于 r≈0.18-0.23 浅中区——深位过采样稀释常见区分布。教训：补弱区数据须保持整体配比。pick-v1 仍为部署策略 |
| 2026-08-08 | 6722ec9 | tabletop_pick_place@v1 | 21/30 (70%) | FAIL | pick-v2 × 感知输入（同上模型）。失败区与真值输入一致，佐证是策略问题非感知问题 |
| 2026-08-08 | 111463f | tabletop_pick_place@v1 | 20/30 (67%) | FAIL | **pick-v3 亦负**：平衡 100 eps（60 旧 + 20 新常规 + 20 深位）、8000 步/9.2 epochs、loss 0.061。数据取证：新旧 episode 组统计同分布（步数/采样节奏/技能时长/动作幅度全同）→ 排除数据污染，指向小数据 ACT 的**训练方差/数据组合敏感**。pick-v1（29/30）保持部署；方法级下一步见 roadmap（固定数据多种子方差研究、部署侧 temporal ensembling） |
| 2026-08-08 | 111463f | tabletop_pick_place@v1 | 24/30 (80%) | PASS* | pick-v3 × 感知输入（同上模型，压线过阈值）。*模型未达部署标准，仅作方差参考记录 |
