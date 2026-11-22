# 跨机视觉与观察时钟契约 · D036

> 2026-11-22，D036 建立 `ObservationEnvelope` 跨主机时钟域、偏斜/漂移/RTT 鲜度预算与 RPC 对象级感知时效传播契约。零新增运行依赖、零 SQLite schema 变更、零 gRPC proto wire 破坏性变更。

## 1. 解决的核心缺口

1. **跨主机禁止直接比较 `monotonic`**：D023 本地对象证据 `ObjectEvidence.captured_monotonic` 仅在同一操作系统内核的同一启动周期内有效；跨主机或跨进程比较两台主机的原始单调时钟会产生任意错判。
2. **远端感知真值泄漏与时效丢失**：此前 `PerceivedSim` 未实现 `read_reference_state()`，通过 `__getattr__` 穿透到底层仿真直接返回真值；`ControlService.GetWorldState` 也固定输出 `source="sim_truth"` 并丢弃 `object_evidence`/`object_context`，导致远端规划与效果验证器将已遮挡或接近过期的感知物体误认为“刚检出”。
3. **跨机墙钟偏斜校验缺失**：跨主机同步墙钟（`wall_ntp` / `wall_ptp`）缺乏显式同步状态、最大偏斜界限、漂移率、未来时间戳拒绝、Cristian 中点往返偏斜反查与接收端本地墙钟跳变保护。

## 2. 跨机时钟域与数学鲜度上界

`src/embodied/contracts/observation.py` 新增纯契约 `ClockMetadata`、`RemoteObjectEvidence` 与 `ClockAdmissionPolicy`（全部 `extra="forbid"`、`strict=True`、`allow_inf_nan=False`）：

| 时钟域 (`clock_domain`) | 允许场景 | 鲜度与安全校验 |
|---|---|---|
| `simulation` | 仿真控制面（默认，向后兼容 D019） | 服务端按自身单调时钟计算对象墙钟年龄 `age_wall_s`；接收端以 `round_trip_s + capture_duration_s + max_clock_skew_s` 约束快照，单对象取 `age_wall_s + drift + round_trip_s + skew` |
| `rpc_rtt_bounded` | 无共享墙钟同步的跨主机拉取 RPC | 不依赖双端 UTC 同步；完全以生产端自测年龄 + 接收端自测 `round_trip_s` + 声明不确定度构建保守上界 |
| `local_monotonic` | 仅限同主机同内核时钟域 | 接收端校验 `clock.host_id == policy.consumer_host_id`；跨主机立即拒收（`cross_host_monotonic_forbidden`） |
| `wall_ntp` / `wall_ptp` | 声明已同步的跨主机 UTC 墙钟 | `sync_status` 必须为 `synchronized`；`max_clock_skew_s` 与 `clock_drift_ppm` 必须不超策略预算；拒绝未来时间戳（`source_unix_s - rx_unix > skew`）；提供 `publish_unix_s` 时执行 Cristian 中点偏斜反查（`|publish_unix_s - (rx_unix - rtt/2)| <= skew + rtt/2`）；检测接收端本地墙钟阶跃跳变（`local_wall_clock_step_detected`） |

### 接收端本地单调时钟重基准（Rebase）

当远端 `ObservationEnvelope` 携带 `object_evidence`（`source` 为 `perceived_sim` / `vision` / `kinematic`）时：

- `ObservationEnvelope` 强校验：`objects` 中每个物体都必须有对应 `RemoteObjectEvidence`，且 `scene_epoch` 与 `calibration_id` 一致；`sim_truth` 禁止混带 `object_evidence`；禁止对象采集仿真时刻晚于包络时刻。
- `SnapshotAdmission` 计算每个对象的保守墙钟年龄 `conservative_wall_age_s` 与仿真年龄 `conservative_sim_age_s`；凡任一达到或超过 `ttl_wall_s` / `ttl_sim_s` 者在入口即剔除，不进入 `WorldSnapshot.objects`。
- 对仍新鲜的对象，接收端以**自身本地单调时钟 `rx_mono`** 签发新 `ObjectContext` 与重基准后的 `ObjectEvidence`（`captured_monotonic = rx_mono`、`ttl_wall_s = orig_ttl_wall_s - conservative_wall_age_s`）。即使调用方缓存该 `WorldSnapshot`，随着本地时间推进，剩余 TTL 耗尽时 `object_available()` 自动转 `False`、效果验证器自动返回 `UNKNOWN`，全程不比较任何跨机 `monotonic` 数值。

## 3. 诚实边界

- 本批完成跨机视觉/观察时钟软件契约、偏斜/漂移/RTT 鲜度门禁、接收端重基准与 mTLS gRPC 端到端感知对象时效传播；已支持 `embodied serve-control --perception color`。
- **不宣称**：真实物理相机硬件失联、真实多网卡硬件 PTP/NTP 时钟同步质量证明，或真机手眼标定验证；这些仍保留在 P1/P2 实机门禁中。

## 4. 本批验收

固定干净提交 `c549839`（source tree `d870ea25...`，含功能提交 `b5e4214` 与测试适配提交 `0dab61a`/`c549839`；无新增运行依赖、无 SQLite schema 变更、无 proto wire 变更）：

- **本机回归**：`ruff` 全绿；完整 5 组 `pytest` **924 项全通过**（+20 项新测，零跳过，4 条既有 ONNX 导出告警）；RPC 套件 **49 项全通过**（+3 项 mTLS 跨机视觉 RPC 测试）；[P1 故障矩阵](p1-fault-matrix.md) 扩展至 **14 组 287 项全通过**（零跳过，`p1_complete: false`）。默认物理控制隔离下 3×30 `tabletop_pick_place@v1` 回归保持真值 30/30、color 30/30、ONNX 29/30（既有第 14 局 `horizon reached`）。
- **Linux Docker 候选与 35 项 mTLS 视觉真栈实测**：
  - 候选镜像 `sha256:cb2775cf...`（`--network none`、1 CPU / 1536 MiB、UID 10001）同步通过控制台冒烟、`compute_smoke.py`、`control_smoke.py` 与 `p1_metrics_baseline.py`，宿主机 40 个既有容器全程未变。
  - 独立测试镜像 `embodied-vision-rpc-test:c549839dec3c`（`sha256:b6c6820d...`，`--network none`、UID 10001）在真实 Linux 98 天高运行单调时钟及 1 CPU 软件 OSMesa 下通过全部 **35/35** 项快照、跨机时钟与 BoringSSL mTLS 视觉 RPC 测试（`10.99s`、0 告警，含真实隔离 MuJoCo + `PerceivedSim(ColorBlobProvider)` 经 mTLS RPC 传递逐对象 `RemoteObjectEvidence` 与场景 reset 代次轮换，全程不放宽 `1.0s` 观察窗或 `1.0s` 跨机鲜度预算）。
- **云端常驻控制台**：本批未改动线上 HTTP/WS 控制面或 schema v2，常驻控制台保持已激活的 `cb05aee`（HTTPS 8449 健康运行）。完整提交绑定证据见 [cross-host-observation-clock-20261122.json](../eval/cross-host-observation-clock-20261122.json)。
