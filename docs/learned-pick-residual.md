# ACT 学习抓放验收

2026-11-29，代码 `8618b8a`。在原版桌面任务上，Windows 的两种输入配置都完成了 30/30，追平同配置脚本。两个模型分别绑定观察来源，并与同一学习放置模型搭配。Linux 的仿真真值配置通过，相机配置未通过。[逐局结果、模型摘要和机器验收](../eval/learned-pick-residual-20261129.json)。

## 配置与结果

每组 30 局，固定 3 个种子、每个 10 局。输入来源在启动前选择。裁判统一读取仿真真值，检查立方体是否进入原盒区。保留集与比较集均采用原空间范围，研究过程中做过跨配置比较。

| 平台与策略 | 输入 | 原任务 0/1/2 | 比较集 13/17/23 | 保留集 31/37/41 |
|---|---|---|---|---|
| Windows 双学习 | 仿真真值 | 30/30 | 30/30 | 30/30 |
| Windows 双学习 | ColorBlob 相机 | 30/30 | 29/30 | 29/30 |
| Linux 单核双学习 | 仿真真值 | 30/30 | 未测 | 30/30 |
| Linux 单核双学习 | ColorBlob 相机 | 中断，已评 3/30，成功 0 | 未测 | 中断，已评 2/30，成功 0 |
| Linux 双核双学习 | ColorBlob 相机 | 中断，已评 1/30，成功 1 | 未测 | 0/30 |

Windows 脚本在原任务真值和相机配置均为 30/30。旧 `pick-v1` 配脚本放置为 29/30，第 14 局抓取超时。新真值抓取单独搭配脚本放置为 29/30，抓取成功 30/30，第 19 局放置失败。比较集和保留集相机分别在第 1、19 局失败。

Linux 使用同一镜像、ONNX Runtime CPU、OSMesa、1536 MiB、128 PID。单核和双核的脚本相机对照也未完成。单核的 5 次静态场景观察均找到方块，RGB-D 渲染及进程间往返耗时 0.424–0.755 秒，ColorBlob 检测往返 0.102–0.203 秒。完整任务仍会遇到观察超时或重置时控制器未空闲。中断报告保留已评分局次，整组成功率为空。

## 数据、模型与复现

新增 200 条均匀专家示教，采集种子 94173，200/200 成功。抓取分段含 55,800 帧，50 Hz。真值版每 10 帧取样，从相近的 8 个示教状态估计冻结 ACT 的动作误差。感知版额外用独立种子 81471 的 64 组观测标定桌面附近的输入，位置误差中位数 0.895 mm。两份 ONNX 均可重复生成相同字节。

| 用途 | 文件名或 SHA-256 |
|---|---|
| 真值抓取 | `pick-act-residual-0b75781ed8666b39f954a3f21f82c078276dfe30225555ffb45b4ec38c60be51.onnx` |
| ColorBlob 抓取 | `pick-act-residual-4f7f3c33a647e247c2132f7ca9726815a8061186e980ee271d3fe8612ca8965c.onnx` |
| 配对放置 | `place-v1.onnx`，`1adfaa2ed02759295b0cc5f1b3f096b20af8fff103fbb9783c8b14b6ed9fb02d` |

以下命令在仓库根目录运行。基础模型放在 `outputs/policies/`。转换目标须是新目录。校准和拟合命令会打印完整产物路径及 SHA-256。

```sh
uv run --group sim embodied collect --episodes 200 --seed 94173 --out outputs/collect/reference-pick-200
uv run --group learn embodied convert --root outputs/collect/reference-pick-200 --out outputs/lerobot/reference-pick-200 --segment skill --skills skill.manip.pick --fps 50
uv run --group sim python scripts/calibrate_pick_observation.py --seed 81471 --scenes 64
uv run --group learn --group policy python scripts/fit_pick_residual.py --dataset outputs/lerobot/reference-pick-200
uv run --group learn --group policy python scripts/fit_pick_residual.py --dataset outputs/lerobot/reference-pick-200 --color-calibration outputs/calibration/pick-color-e6e00768df2999dbfe01b718084b47e02bc746ee3140778058340e7a9fa65a0b.json
uv run --group sim --group policy embodied sim --task eval/tasks/tabletop_pick_place_v1.yaml --perception color --pick-policy outputs/policies/pick-act-residual-4f7f3c33a647e247c2132f7ca9726815a8061186e980ee271d3fe8612ca8965c.onnx --place-policy outputs/policies/place-v1.onnx
```

模型只有当前关节与物体位姿两个输入，输出 50 步绝对关节目标。关节顺序为 `Rotation, Pitch, Elbow, Wrist_Pitch, Wrist_Roll, Jaw`。维护态发行 CLI 已对两套真实权重校验文件摘要、ONNX 元数据、仿真停止配置与 v3 账本。私网控制台继续运行脚本配置。

## 负结果与后续

固定原 60 条示教，更换训练种子到 1001 后，四个检查点在比较集为 5、21、16、12/30，最终检查点在原任务为 11/30。旧模型将动作块执行前缀从 50 改到 25 得到 22/30，时序融合得到 25/30，三模型动作加权为 28/30。用新增 200 条示教继续训练 200/400 步，比较集为 28/30 和 23/30。模型及失败局次保留在验收记录中。

Grounding DINO 的 Windows 工作进程现可载入模型。单线程 CPU 离线 8 张图每帧 18.62–23.81 秒，7 个框覆盖投影中心，在线检测预算为 2 秒，端到端探针 0/1。后续先测目标设备的相机、推理与采集到使用的总延迟。[DINO 诊断](../eval/dino-cpu-20261129.json)。
