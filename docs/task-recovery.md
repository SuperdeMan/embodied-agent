# 任务日志与只读恢复评估 · D027

> 2026-11-04，实施中。用户已明确授权本页 v2 升级与部署；现网暂仍为 D026 `505b60e`，待验收通过再迁移。

## schema v2 方案

在同一 SQLite 账本追加两表：

- `task_plans(task_id, revision, digest, plan_json, recorder_boot_id, created_at)`：主键为任务/版本，固定经接收端 manifest 重建的计划。相同版本只允许相同摘要。
- `task_events(event_id, task_id, revision, step_id, event_type, payload_json, created_at)`：追加计划登记、步骤意图、执行器回执、任务结束或重启中断记录。关联操作 ID 在载荷中保存，不制造日志专用的物理尝试。

原 `tasks/operations/attempts/operation_events` 的列和行不迁移、不删除；仅把 `schema_version` 从 1 更新为 2。新增数据库直接使用 v2，已有 v1 不隐式升级；v1 可查询旧操作，但启用任务日志的规划执行会在动作前拒绝并提示升级。

升级流程：只读检查 → 停止本项目写者并取得既有文件锁 → 备份 → `BEGIN IMMEDIATE` → 两表及索引 DDL → 更新版本 → 验证旧行与外键 → COMMIT。任一步失败回滚事务。不得在有 RUNNING 操作时升级。干跑在副本上执行并比较历史行，不修改原库。

旧应用 `505b60e` 不认识 v2，因此不能把“切回旧镜像”当作完整回滚。版本升级后如果已经产生新记录，应保留 v2 数据并滚前修复；只有确认没有后续写入、历史行与备份完全一致时，才可另行审核备份恢复。不自动丢弃新记录回退。

## 恢复边界

保存的计划包含每个步骤、依赖、参数引用、固定 operation_id 和接收端 manifest。意图必须先持久化，确认仍由 Registry 独立强制。执行器回执用于历史展示，不能代替 operation/attempt 或效果证据。

重启后未结束任务归为 INTERRUPTED；已保存的回执和 SAT 保留历史属性，不恢复为当前执行权限。无计划的旧任务为 LEGACY，不根据结果反推原计划。只读查询/评估列出剩余步骤、未知结果、停止状态和当前效果资格，`automatic_resume_allowed=false`。它不调用技能、停止、重试或解除隔离。

本批交付计划记录、意图/回执记录、任务查询与只读评估。执行续跑、多 attempt 和跨控制器身份恢复留给后续协议，不在本次迁移中暗中开放。

云端副本预演已通过：v1 的 6 tasks、12 operations、12 attempts、40 operation_events 在 v2 副本中保持相同行数与内容摘要。该预演未修改现网；实际升级前仍重新检查无运行中操作并生成备份。

重启中断判定针对持有任务日志的控制接收端；客户端断连若没有结束回执，任务保留未收敛历史，不推定完成。D027 不提供客户端身份认证或自动执行接管。计划/事件每类每任务最多 1 MiB，最多 8 个版本、512 步；达到容量上限拒绝继续派发，保留人工审查路径。


## 查询与升级入口

- 控制台 WebSocket 的 `step`、`reply` 返回 `task_id`；`GET /api/tasks` 列出最近任务，`GET /api/tasks/{task_id}` 查看历史，追加 `?assess=1` 检查已有证据当前资格，三个接口均不执行动作。
- RPC 客户端：`uv run embodied task --remote 127.0.0.1:8391 --id TASK_ID --reconcile`；省略 ID 列出最近任务。新增任务 RPC 使用接收端技能清单，不接受客户端下调确认或安全要求。
- 迁移预演：`uv run embodied ledger-upgrade --ledger outputs/runtime/operations.sqlite3`。维护态停止写者后，追加 `--apply --backup-dir outputs/deployment-backups` 才修改原库；已是 v2 时只报告当前状态。
- `FINISHED` 仅表示规划轮已结束，不保证目标成功。每步实际调用、效果、停止以及执行器报告分别展示；历史 SAT 不等于当前 SAT，`INTENT_ONLY` 不推断动作是否发生。

后续优先完成 P1 故障矩阵与恢复权限协议，再引入明确授权的多 attempt；本批不提供可执行续跑接口。跨主机身份、父进程物理控制隔离、真机停止仍须独立验收。
