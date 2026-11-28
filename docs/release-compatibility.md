# 发行兼容契约与 v2 账本降级恢复 · D037

> 2026-11-22，D037 冻结 `ModelBundle` 与 `ReleaseBundle` 纯契约，实现维护态激活/回滚预检、全七表 v2 账本在线备份与原子恢复，并完成双镜像降级与损坏恢复演练。零新增运行依赖、零 SQLite schema 变更、零 gRPC proto 变更。

## 1. 解决的核心缺口

1. **相同张量形状不等于物理兼容**：此前 ONNX 策略仅检查 `state_dim`/`env_dim`/`action_dim`，缺少 `robot_id`、`joint_order`、单位、`action_semantics`、归一化方式、推理后端与文件 SHA-256 的语义兼容门禁。
2. **缺少发行激活与回滚前维护态门禁**：版本切换前未强制检查独占账本锁、`RUNNING` 活跃操作、`STOP_UNCONFIRMED` 隔离状态、资产/停止 profile 摘要、配置摘要与 schema 版本范围，也未禁止将未经演练的版本组合标为“可自动回滚”。
3. **v2 账本新写入后的降级与损坏恢复未演练**：P1 故障矩阵第 7 条门禁要求在产生新 v2 写入后，实测受控回滚至前序兼容版本、拒绝不兼容降级（如 v1-only 运行时），并演练账本损坏后的认证备份原子恢复。

## 2. 契约与预检门禁

`src/embodied/contracts/release.py` 定义纯类型（`embodied.dev/v1alpha1`，`extra="forbid"`、`strict=True`、`allow_inf_nan=False`）：

- **`ModelBundle`**：绑定 `model_id`、`skill_name`、`artifact_path`、`artifact_sha256`、`robot_id`、`joint_order`、`units`、`action_semantics`、`normalization_mode`、输入输出维度、`backend`、`precision`、`license` 及评测证据摘要；强校验关节语义下 `len(joint_order) == action_dim`。
- **`ReleaseBundle`**：绑定 `release_id`、`commit_sha`（40 位 hex）、`runtime_version`、`robot_id`、`driver_firmware_compat`、`min_ledger_schema..max_ledger_schema`、`asset_digest`、`stop_profile_digest`、`config_digests`、`skills_catalog_sha256`、`model_bundles`、`eval_evidence_sha256` 与显式 `verified_rollback_targets`；`hardware_verified` 固定为 `false`。
- **`ReleasePreflightReport`**：维护态预检结果（`allowed`、`rollback_safe`、`reasons`、`ledger_schema_version`、`ledger_all_tables_sha256`）。

### 强制拒绝条件（fail-closed）

| 检查类别 | 拒绝条件 | 结果 |
|---|---|---|
| 本体与固件 | `robot_id` 不匹配、驱动固件不在 `driver_firmware_compat` | `allowed=false, rollback_safe=false` |
| 资产与配置完整性 | `asset_digest` / `stop_profile_digest` 不匹配、配置文件缺失/路径穿越/SHA-256 篡改 | `allowed=false, rollback_safe=false` |
| 模型物理语义 | 即使维度相同，只要 `robot_id`/`joint_order`/`units`/`action_semantics`/`backend`/`normalization_mode` 不符或权重哈希不匹配 | `allowed=false, rollback_safe=false` |
| 维护态与账本锁 | 其他进程持有 `.lock`、存在 `RUNNING` 操作、或存在未完成停止确认的 `OUTCOME_UNKNOWN` 尝试 | `allowed=false, rollback_safe=false` |
| Schema 兼容 | 账本损坏、未知 schema（如 v99），或目标版本 `[min_ledger_schema, max_ledger_schema]` 不覆盖当前账本（如 v1-only 降级读 v2） | `allowed=false, rollback_safe=false` |
| 回滚资格 | 目标回滚版本不在当前版本的 `verified_rollback_targets` 中 | `allowed=false, rollback_safe=false`（`unverified_rollback_target`） |

D038 补充。激活和回滚都要提供 `--ledger` 并取得独占检查。涉及配置或模型文件时还要提供 `--bundle-root`；ONNX 文件需由能读取其元数据的运行时核验。缺少所需证据时，CLI 返回退出码 2，报告列出原因。

2026-11-28 的 `142e6eb` 在本机通过 939 项测试。Windows 实测 8 项 CLI 条件，包含实际 `pick-v1.onnx`；Linux 隔离镜像实测 7 项条件及离线 MuJoCo 抓放、监督和故障回收。候选校验前后均保留宿主原有 40 个运行容器。逐项结果见[发行预检验收记录](../eval/release-preflight-20261128.json)。

## 3. 全七表备份与原子恢复

`src/embodied/runtime/release.py`（及 CLI `embodied release-check`）：

- `full_ledger_digest(db)` 对当前 schema 全部业务表（v2 为全七表：`schema_version`、`tasks`、`operations`、`attempts`、`operation_events`、`task_plans`、`task_events`）计算确定性 SHA-256。
- `backup_ledger()` 须独占 `.lock`、确认无 `RUNNING` 操作并通过全表完整性与外键校验后，使用 SQLite 在线备份 API 原子生成备份文件与 `LedgerBackupManifest`（含文件 SHA-256 与全七表摘要）。
- `restore_ledger_backup()` 须独占 `.lock`，先校验备份文件 SHA-256、SQLite `integrity_check`、`foreign_key_check` 与期望全七表摘要，全部通过后才经临时文件 `fsync` + `os.replace()` 原子替换受损目标账本；备份被篡改或存在活跃锁时原账本零改动。

## 4. 诚实边界

- 本批覆盖仿真/容器交付的 `ReleaseBundle`/`ModelBundle` 语义兼容预检、v2 账本新写入后的真实双镜像受控降级（新版 ↔ `cb05aee`）、不兼容 v1 降级拒止，以及受损账本的认证备份原子恢复。
- **不宣称**：真机固件刷写（OTA）、多机集群灰度发布，或跨大版本 schema（如未来 v3 → v2）降级兼容；这些仍保留在后续实机与 schema v3 门禁中。

## 5. 本批验收

固定干净提交 `75853f5`（source tree `869beb17...`，无新增运行依赖、无 SQLite schema 变更、无 proto 变更）：

- **本机回归**：`ruff` 全绿；完整 5 组 `pytest` **936 项全通过**（+12 项新测，零跳过，4 条既有 ONNX 导出告警）；RPC 套件 **49 项全通过**；[P1 故障矩阵](p1-fault-matrix.md) 扩展至 **15 组 299 项全通过**（零跳过，`p1_complete: false`）。默认物理控制隔离下 3×30 `tabletop_pick_place@v1` 回归保持真值 30/30、color 30/30、ONNX 29/30（既有第 14 局 `horizon reached`）。
- **Linux Docker 候选与双镜像真实降级/恢复演练**：
  - 候选镜像 `sha256:a2a364d9...`（`--network none`、1 CPU / 1536 MiB、UID 10001）通过控制台冒烟、`compute_smoke.py`、`control_smoke.py` 与 `p1_metrics_baseline.py`，宿主机既有 40 个容器未变。
  - **双镜像演练（`embodied-agent:75853f5` `sha256:a2a364d9...` ↔ `embodied-agent:cb05aee` `sha256:f4c5a494...`）**：
    1. `75853f5` 在隔离容器内跑完真实抓放任务写入全七表（`1 task / 2 ops / 2 attempts / 7 op_events / 1 plan / 6 task_events`，全七表摘要 `d5ddfe05...`）；`embodied release-check` 拒绝 v1-only 降级（`ledger_schema_incompatible:v2_not_in_[1,1]`）与未验证目标，放行已验证的 `cb05aee` 回滚（`rollback_safe=true`）并生成认证备份（`d093da85...`）。
    2. 将该 v2 账本直接挂载入真实前序镜像 `cb05aee`：写前全七表摘要逐字节一致（`d5ddfe05...`），正确读取 D037 历史任务并完成第二局抓放写入（`tasks 1→2 / ops 2→4 / plans 1→2 / task_events 6→12`）。
    3. 故意截断破坏主库并注入 17 字节垃圾 `-wal`：`release-check` 拒止损坏账本、拒绝错误哈希恢复；经认证 `--restore-from` 清除残留脏 WAL 并原子还原至精确备份摘要 `d5ddfe05...`，重开 `OperationLedger` 无错，宿主机 40 个容器全程未变。
- **云端常驻控制台**：本批未改动线上 HTTP/WS 控制面或 schema v2，常驻控制台保持已激活的 `cb05aee`（HTTPS 8449 健康运行）。完整提交绑定证据见 [release-compatibility-20261122.json](../eval/release-compatibility-20261122.json)。
