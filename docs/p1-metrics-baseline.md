# P1 可观测延迟、过期动作率与 Episode 崩溃完整性基线 · D035

> 2026-11-22，D035 建立提交绑定的仿真 P1 延迟、过期动作拦截、人工介入与 Episode 硬杀恢复基线，并补齐 `EpisodeWriter` 增量日志与完整性审计。无新增运行依赖、无 SQLite schema 变更、向后兼容 `v0` 录制与 LeRobot 转换。

## 指标口径与诚实边界

| 指标 | 含义 | 口径边界 |
|---|---|---|
| `guard_estop_latch_ms` | `guard.estop()` 完成父进程 Guard 闩锁与代次递增的墙钟耗时 | 进程内软件闩锁，不代表真机硬件停机 |
| `urgent_estop_kill_ms` | `urgent_estop()` 跨线程强杀持 GIL 挂起物理子进程并确认 `reaped=true` 的墙钟耗时 | 仿真子进程树回收延迟（含 `cancel_grace_s` 宽限窗），不代表 SG-0 硬件急停 |
| `stop_reconciliation_wall_ms` / `stop_reconciliation_sim_ms` | 真实隔离 MuJoCo 停止控制器完成保持/双侧接触力稳定窗口观测的墙钟与仿真毫秒时长 | 仅限 `mujoco-hold-current-v1` 仿真控制器 |
| `stale_interception_rate` | 过期命令、陈旧控制反馈、陈旧语义目标与重放序号注入测试中的拦截比例 | 要求 `1.0`（0 条过期或重放动作放行） |
| `manual_intervention_rate` | 需要危险确认、取消、停止对账、效果重查或物理控制重启的操作占比 | 由 `SkillRegistry.metrics_snapshot()` 精确计数 |
| `normal_data_loss_rate` / `abrupt_kill_loss_rate` | 正常录制与真实子进程 `os._exit(9)` 硬杀恢复后的步骤丢失率 | 增量日志按 `checkpoint_every` 刷盘，硬杀损失严格有界（`< checkpoint_every` 步） |

所有报告固定携带 `hardware_stop_measured: false` 与 `p1_complete: false`。

## Episode 增量日志与完整性审计

- `EpisodeWriter(..., checkpoint_every=10)` 在 `on_step()` 期间同步把步骤追加到 `steps.journal.jsonl` 并按周期刷新；正常 `finish()`/`abort()` 先原子写出 `steps.npz`，记录 `steps_sha256` 至 `meta.json`，全部完成后移除临时日志。
- `audit_episode(episode_dir)` 校验 `meta.json`、`steps.npz` 的 SHA-256、数组形状对齐、状态/动作有限性、时间戳单调非降与 `events.jsonl` 逐行合法性；检测任意文件损坏或篡改。
- `recover_episode(episode_dir)` 面向硬杀残留目录（`meta.json` 草稿 + `steps.journal.jsonl`、无 `steps.npz`），自动丢弃不完整截断尾行并重建完整 `steps.npz` 与最终 `meta.json`（`success=false`、`aborted=true`、`recovered_from_crash=true`），恢复结果可直接供 `load_episode()` 与 `to_lerobot()` 消费。

## 可重复测量命令

```bash
uv run --group sim python scripts/p1_metrics_baseline.py --out outputs/p1-metrics
```
