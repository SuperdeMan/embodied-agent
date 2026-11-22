# P1 故障矩阵与 D028 取消修复

> 2026-11-04，D028 已验收并部署 `c1fc926`。v2 schema 与运行配置保持；不开放自动续跑。

## 本批问题与修复

原 `cancel_all` 顺序调用单项取消，首项账本异常会中断循环；`ControlState.halt` 又在循环之后调用 Guard，形成存储故障阻断监督闩锁的组合缺口。

现在先撤销所有活跃许可并发出协程取消，再逐项尝试持久化；失败项返回操作 ID 与异常类型，后续项仍处理。单项取消也先撤权，读写失败保留资源隔离。控制服务先置闩锁及驱动 Guard，再尝试持久化，错误不退出监督 watchdog。Guard 自身异常仍抛出，但全部许可也会被撤销。

撤权不是物理停止；写入失败时磁盘记录可能仍为 RUNNING，重启后按既有规则归入 UNKNOWN/STOP_UNCONFIRMED。不得以报错、Reset 或恢复磁盘可写为由重放动作或解除隔离。本批不开放恢复执行权限。

## 可重复验收

`uv run --group sim --group rpc python scripts/p1_fault_matrix.py` 执行 [矩阵清单](../eval/p1-fault-matrix.json)，在 `outputs/p1-faults/` 保存 SHA、工作区状态、各组通过/失败/跳过数、JUnit 摘要和日志。任何缺模块、失败或跳过均不能标参考检查通过。

| 故障组 | 参考证据 | 实际边界 |
|---|---|---|
| 存储＋取消/监督 | 读写故障注入、SQLite 页数上限触发真实 SQLITE_FULL、首项失败仍全量撤权、RPC Halt、监督超时 | 不填满宿主磁盘，不代表真实介质故障 |
| 崩溃与回执 | 实际子进程退出、已接收记录恢复 UNKNOWN、结果写失败后同 ID 不重放 | 不代表真机断电恢复 |
| 任务恢复 | 损坏计划/事件、只有意图、取消、场景变更、只读评估 | 不授予自动续跑权限 |
| 不兼容账本 | 未知版本、活跃 owner 拒绝、迁移事务回滚 | 不代替完整发行兼容矩阵 |
| 计算进程 | 挂起、崩溃、坏帧、进程回收、无重放 | 父进程物理控制隔离未关闭 |
| 监督链路 | 本机真实 RPC 的断连/过期/确认/闩锁 | 跨机网络分区仍需现场验证 |
| 观察可用性 | 阻塞只读工作、缺测/过期的本地感知 | 真相机失联与跨机时钟未验收 |

报告时间分位数是**测试用例运行时间**，不冒充急停延迟；真实停止延迟、过期动作率、人工介入率与 episode 数据损失仍须专门测量。矩阵始终列出未关闭门禁，参考测试全绿不等于 P1 全部退出。

## 下一优先级

1. 明确恢复授权的主体、目标 task/operation/attempt、场景/profile 与期限绑定；先定义拒绝条件和审计记录，再设计执行接口。
2. 补父进程原生控制阻塞与独立监督的受控拓扑；设备缺席时保留真机门禁。
3. 在上述边界可验证后制定多 attempt 规则，避免把旧 UNKNOWN 翻转为成功或自动重试。

## 本批验收

固定提交 `c1fc926`：ruff、757 项完整测试及 28 项 RPC 测试通过；矩阵 7 组 120 项全通过，无跳过。真值/color 各 30/30，ONNX 29/30 保留既有第 14 局失败。[本地与矩阵证据](../eval/cancellation-faults-20261104.json)。

Linux 隔离容器的实际 SQLite FULL 验证了两项许可先撤销、全部尝试取消，以及重启后 UNKNOWN/STOP_UNCONFIRMED；未测量物理停止延迟。常驻升级保留既有 v2 七表，历史业务表、计划和事件逐行一致，抓放、重启、真实 HTTPS/WSS 与只读查询通过。[云端证据](../eval/cloud-cancellation-upgrade-20261104.json)。

## D029 增量

`a29ad0e` 增加恢复审查组：并发变更失效、Guard 生命周期、异常/过期证据及 RPC 授权声明拒绝，矩阵扩展为 8 组 150 项，全部通过。此前 D028 的 120 项仅属于其固定提交；当前结果见[恢复审查证据](../eval/recovery-review-20261104.json)。下一步是受信操作者身份与授权消费审计；只读审查摘要不授予执行权限。

## D030/D031 增量

`5b080dd` 新增操作者策略/HTTP 授权组及本体反馈边界，当前 9 组 185 项全部通过。真实身份、一次性消费与重启审计另有云端证据；不推及未接入的参考 RPC 或任务执行。前两次 color 套件失败、e1b2706 的 29/30 及最终 30/30 分开保留，见[本批证据](../eval/operator-authorization-20261104.json)。

## D032 增量

`90135f0` 新增 `stop_authorization` 组与显式策略作用域检查，矩阵扩展为 10 组 222 项全部通过（零跳过）。覆盖缺省策略拒止、双作用域停止确认与 5 事件审计序、跨重启停止、Guard 闩锁保持、快照变更失效、各落账点失败与 WebSocket 中断隔离恢复；实际设备/跨机/父进程原生控制与多 attempt 门禁仍保留未关闭。证据见[本地证据](../eval/stop-authorization-20261122.json)与[云端证据](../eval/cloud-stop-upgrade-20261122.json)。

## D033 增量

`25343fa` 新增 `rpc_mtls_authorization` 组，矩阵扩展为 11 组 241 项全部通过（零跳过，`p1_complete: false`）。覆盖 CA/叶证书 SHA-256 指纹固定、非回环明文启动与连接拒止、硬编码 `agent`/`guardian`/`operator` 角色方法矩阵、未授权不污染连接标志、认证维护审计写失败阻断、D028 存储故障下 `Halt`/`CancelOperation` 优先撤权闩锁，以及 Linux Docker 三进程 BoringSSL mTLS 实测；CRL/OCSP 吊销、跨机视觉时钟、父进程物理控制隔离、多 attempt 与真机急停门禁仍保留未关闭。证据见[本批证据](../eval/rpc-mtls-authorization-20261122.json)。

## D034 增量

`cb05aee` 新增 `physical_control_isolation` 组，矩阵扩展为 12 组 260 项全部通过（零跳过，`p1_complete: false`）。覆盖无头 MuJoCo 物理控制子进程与 D026 离屏渲染子进程分离（三进程 PID 互异、数值一致）、持 GIL 原生挂起/崩溃/`abort`/坏协议回收且零重放、`urgent_estop` 与 D033 mTLS Guardian Halt 跨线程强杀卡死物理子进程、`/healthz` 503 与 `RecoveryReview` 阻塞项、以及显式 `restart_physical_control()` 轮换 `controller_boot_id` 并保留 Guard 闩锁与资源隔离；真机 SG-0 独立硬件急停、跨机视觉时钟、延迟/数据损失基线与 schema v3 多 attempt 门禁仍保留未关闭。证据见[本地证据](../eval/physical-control-isolation-20261122.json)与[云端证据](../eval/cloud-physical-isolation-upgrade-20261122.json)。

## D035 增量

`b723417` 新增 `metrics_and_episode_integrity` 组，矩阵扩展为 13 组 267 项全部通过（零跳过，`p1_complete: false`），并新增专门的可重复基线脚本 `scripts/p1_metrics_baseline.py` 取代对“测试用例运行时间”的误读。实测涵盖隔离仿真下 Guard 闩锁、持 GIL 物理子进程强杀回收、仿真停止对账（`100.0 ms` 仿真窗）、100% 过期与重放动作拦截、人工介入计数、以及 `EpisodeWriter` 增量刷盘（`checkpoint_every=10`）在真实 `os._exit(9)` 硬杀下将损失压至 `< 10` 步（恢复 50/57 步、容忍截断尾行并检出 SHA-256 篡改），Windows 本机与 Linux Docker 隔离容器双平台均已采集；`hardware_stop_measured` 固定为 `false`，真机 SG-0 急停延迟、跨机视觉时钟与 schema v3 多 attempt 门禁仍保留未关闭。证据见[本批证据](../eval/p1-metrics-baseline-20261122.json)。

## D036 增量

`c549839` 新增 `cross_host_observation_clock` 组，矩阵扩展为 14 组 287 项全部通过（零跳过，`p1_complete: false`）。覆盖跨主机 `local_monotonic` 拒止、`wall_ntp`/`wall_ptp` 同步状态/偏斜/漂移/未来时间戳/本地墙钟跳变与 Cristian 中点往返偏斜反查、无 epoch 变更切换时钟 ID/时钟域/标定拒止、`PerceivedSim.read_reference_state()` 阻断真值泄漏、逐对象 `RemoteObjectEvidence` 叠加 RTT 与偏斜后重基准至接收端单调时钟并在遮挡/超时下使远端验证器返回 `UNKNOWN` 阻断依赖，以及 Linux Docker 隔离容器 35 项 BoringSSL mTLS + OSMesa ColorBlob 实测；真实物理相机掉线、硬件 PTP/NTP 同步质量、真机 SG-0 急停与 schema v3 多 attempt 门禁仍保留未关闭。证据见[本批证据](../eval/cross-host-observation-clock-20261122.json)。

## D037 增量

`75853f5` 新增 `release_compatibility_and_rollback` 组，矩阵扩展为 15 组 299 项全部通过（零跳过，`p1_complete: false`）。覆盖 `ModelBundle` 超越张量维度的物理语义与权重哈希校验、`ReleaseBundle` 维护态独占锁/活跃操作/未确认停止/配置与资产摘要预检、新 v2 写入后拒止 v1-only 降级与未验证回滚目标、全七表在线备份与受损账本（含脏 `-wal`）认证原子恢复，以及 Linux Docker 真实双镜像（`75853f5` ↔ `cb05aee`）降级读写与恢复演练；真机固件 OTA、真机 SG-0 急停、物理相机断连与未来 schema v3→v2 降级门禁仍保留未关闭。证据见[本批证据](../eval/release-compatibility-20261122.json)。
