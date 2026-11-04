# 云端仿真控制台

> 2026-11-04，当前 **activated**：应用镜像固定 `f27d0ac`，运行配置仍为 `cfb5997`，私网 HTTPS 8449。后续 main 推送不自动更新该镜像；下方 D026/D025/D024/2026-10-30 记录保留为历史。

## D027 最近升级（2026-11-04）

应用由 `505b60e` 升级至 `f27d0aca617bc09b3e24f2369fe5fdee7b78eeb4`，激活回执 `20261104T075640Z`，镜像 ID `sha256:6b474409db455e4d9fab20062cd02e7ffea36078ef384ad20763a3c0ea588cb8`。用户明确授权新增 `task_plans/task_events`；停止旧写者后独占账本、生成 v1 备份、单事务升级 v2，并核对旧业务内容摘要一致。

原 6 tasks、12 operations、12 attempts、40 operation_events 全部逐行保留。内部抓放及容器 restart 后记录和任务日志保留，旧 SAT 在重启后不再具备当前证据资格；HTTP 查询与评估不写账本、不重放动作。外部 HTTPS/WSS 抓放后账本为 8 tasks / 16 RETURNED operations / 2 task_plans / 12 task_events。

Linux 候选、真实 HTTPS/WSS 和重启持久性均通过；其他 39 个运行容器与 Serve 映射未变，继续私网仿真、离线规划、模拟音频，原资源限制与持久卷保持。证据见 [cloud-task-upgrade-20261104.json](../eval/cloud-task-upgrade-20261104.json)。不提供自动续跑，真机、完整浏览器及麦克风仍未验收。

旧镜像不兼容 schema v2，禁止自动启动旧镜像或覆盖备份丢弃新记录；失败保留数据、进入维护状态并滚前修复。本轮没有降级演练。

## D026 升级（2026-11-04 历史）

由 `e8df7ad` 升级至 `505b60eed5b796991c9b84e0c4bf00473d604a1e`，激活回执 `20261104T061033Z`，镜像 ID `sha256:8baee1182d4351541359076442cfa6617aefd37819e2c35feae83c2b1c6c6c7a`。渲染/JPEG 改在独立 worker 中处理状态副本，控制器和账本仍在父进程；常驻模式继续为仿真、离线规划、模拟音频。

原卷与 schema v1 保留，升级前备份的 4 tasks、8 operations、8 attempts、26 events 逐行一致。内部验收与 restart 后，标记和记录保留；外部 HTTPS/WSS 抓放后账本为 12 条 RETURNED / 12 条效果事件。实际检查到单独的渲染 worker PID，其父 PID 为应用 PID 1，进程组及 session 独立。39 个其他运行容器和其他 Serve 映射未变，Funnel 关闭；1 CPU / 1536 MiB / 128 PID、只读根、非 root、drop ALL、无宿主端口保持。

Linux 隔离候选验证了持有 GIL 的阻塞、崩溃/abort、坏代次和超长帧的回收及显式恢复，且未重发旧请求。真实入口完成 TLS、HTML/JPEG、并行预览与 WSS 抓放验证。子进程冷启动期间仍可能返回有界 503，有限就绪重试后恢复；不把本地 30/30 转借云端。DINO/ONNX 权重未部署到该控制台，完整浏览器交互、麦克风和真机仍未验收。

准确证据见 [cloud-compute-upgrade-20261104.json](../eval/cloud-compute-upgrade-20261104.json)。旧镜像、备份及候选日志保留；本轮未执行回滚演练。

## D025 升级（2026-11-04 历史）

由 `5e2311f` 升级至 `e8df7ad84e3a1f03604a608b836756f230f0dd5b`，激活回执 `20261104T030336Z`，镜像 ID `sha256:1f2cf92cb78a9fede23f16771ea36e18dca6f4802ca8a7c0d9db51cdbf34ea39`。新增有界观察和预览交接；仍为仿真、离线规划、模拟音频。

原卷与 schema v1 保留。升级前已备份 SQLite，升级后对原有 2 tasks、4 operations、4 attempts、12 events 逐行核对一致。内部验收后容器 restart 的记录和标记保留；真实 HTTPS/WSS 抓放后账本为 8 条 RETURNED / 8 条效果事件，其中新增放置绑定事件 2 条。其他 39 个运行容器 ID、其他 Serve 映射和原 CPU/内存/安全限制未变，Funnel 关闭。

Linux 候选在并行预览时完成抓放；注入 0.1 秒观察预算约 0.101 秒返回，拒绝替代队列并可在旧线程完成后接受新观察。外部验证覆盖 TLS、HTML、已固定 bundle 的前端文件、JPEG、健康请求与并行预览下的 WSS 抓放。冷启动首帧可按 1 秒预算返回 503，随后有限重试恢复；浏览器改为上一帧完成后再轮询。已有页面需刷新以加载新脚本。未做完整浏览器交互或麦克风验收，也不称为云端全套 30/30。

部署与数据保留证据见 [cloud-observation-upgrade-20261104.json](../eval/cloud-observation-upgrade-20261104.json)。`47f68cc` 候选未激活；首次探针未兼容有界冷帧 503，相关诊断保留。旧镜像和升级前备份继续保留，本轮未执行回滚演练。

## D024 升级（2026-11-04 历史）

用户授权后，由 `212f912` 升级至 `5e2311f355034d32d1fb9fa4241f79fe1c9c159d`，激活回执 `20261104T014232Z`。镜像 ID 为 `sha256:02ee4be924c811a7667e577161e3813fec07f314ad59be3169a94e4b2889eacc`。新增 D021–D024 的仿真停止对账、统一动作准入、对象时效和持久效果能力；常驻入口继续使用仿真真值、离线规划与模拟音频。

升级前已确认无运行中操作、备份 SQLite，并保留 `embodied-agent-cloud_runtime` 原卷；当时账本为 **0 条操作**，不夸称保留了不存在的历史任务。常驻抓放产生 2 条 RETURNED 与 2 条 SAT 效果事件，容器 restart 后记录和持久标记相同；随后真实 HTTPS/WSS 抓放再次成功，最终为 4 条 RETURNED / 4 条效果事件。

实际核查：1 CPU / 1536 MiB / 128 PID、只读根、10001:10001、drop ALL、no-new-privileges、internal 网络、无宿主端口。39 个其他运行容器 ID 未变，其他 Serve 映射未变，Funnel 仍关闭；8449 目标由本项目容器实际 IP 重新发现并核对。Compose 文件及哈希与首次激活相同。准确证据见 [cloud-upgrade-20261104.json](../eval/cloud-upgrade-20261104.json)。

外部入口验证覆盖 TLS、HTML、JPEG、带匹配 Origin 的 WSS 和完整离线抓放；未做完整浏览器交互/麦克风验收，也未把本地 30/30 转借云端。首次 D024 候选 `2f547c7` 因放置效果记录缺失未激活；日志与候选均保留。旧应用镜像及升级前备份可用于受控回退，本轮未执行回滚演练。

## 可部署内容

已有 Web 控制台、文本任务、MuJoCo 场景预览、脚本抓放、效果验证与 SQLite 操作账本可以形成私网参考环境。初版采用 `--provider offline`，ASR/TTS 为模拟回退，不能标成真实模型或真实语音识别。模型训练、GPU 策略和真实机械臂控制不纳入此部署。

控制台当前为单进程仿真参考入口，不等于已验证的 agent/control/guardian 独立真机拓扑。云端仿真数据与现场设备数据分别保存。

## 主机只读核查

通过已有进程环境中的 SSH 连接参数访问，未读取或复制姊妹项目 .env。核查时：4 CPU、7685 MiB 内存、约 5332 MiB available、根盘约 70 GiB 可用，39 个容器运行。已有 Serve 端口为 443 与 8443–8448，Funnel 未开启。数值是本次快照，激活前须再次检查。

## 当前运行边界

- 独立 `~/embodied-agent/`，按 Git SHA 保存源码/镜像与验收；数据目录独立持久化。
- 一个 `embodied-agent-cloud` Compose project，console 容器限制 1 CPU、1536 MiB、128 PID、无 capabilities、只读根文件系统；只挂载本项目数据与临时目录。
- 不发布宿主端口；私网 HTTPS `8449` 直接代理经检查的内部容器地址 `:8390`。保留所有其他项目容器、网络、服务和映射。此项为激活实测后的接线修正，见 D020。
- 镜像使用锁定 Python 依赖、固定 Menagerie 资产；CPU Mesa 离屏渲染。无设备映射、无 Docker socket、无姊妹项目 secrets。
- 页面标明仿真、离线规划、模拟音频；健康接口报告当前构建版本与模式。
- 激活前验证健康、JPEG 场景、WebSocket 文本任务、账本与资源限额。失败只停止本项目候选，不重启共享 Docker，不删除其他项目对象。

常驻应用激活和 Tailscale 映射已获用户明确授权。候选构建和验收使用无网络、无发布端口的临时运行配置，结束后停止并保留日志；不新增公网入口或改写其他项目的主机配置。

## 首次 Linux 候选验收（2026-10-30 历史）

镜像 `embodied-agent:212f912d067d933a12949c12d507db493b0a9f13`，image ID 为 `sha256:4f91b02ee5a724907339dde4c38bb0d7ca66567e39aa8db5d7b2f0dc4a67ba3a`。源码和固定 Menagerie 资产作为独立 bundle 上传后逐文件核对 SHA-256；构建没有读取其他项目 .env。

候选在真实 Linux Docker 主机中通过：健康接口报告准确 release、离屏 JPEG 渲染（31053 字节）、WebSocket 文本指令完成 pick/place、两条操作身份与 RETURNED 账本结果。只验证了这一条云端正常任务，不称为云端全套 30/30。

候选阶段 Docker inspect 实际确认：1 CPU、1610612736 字节内存、128 PID、用户 10001:10001、只读根、drop ALL capabilities、no-new-privileges、network none、无宿主端口。候选容器已停止，前后其他运行容器的 ID 和名称保持一致。常驻配置的独立核验及接线修正见下节，不能用该候选检查代替。

首次 `fde3247` 候选在 Debian HTTP 包索引下载停滞时中止，只终止匹配该候选 SHA 的 Docker build 进程，未操作共享 daemon。后续镜像内改用已验证可达的 HTTPS 镜像源及有限下载超时，第二候选构建成功。失败日志与两个源码包均保留。

候选机器可读验收见 [cloud-candidate-20261030.json](../eval/cloud-candidate-20261030.json)。服务器日志位于本项目 `artifacts/<SHA>/`。

## 首次常驻激活与接线修正（2026-10-30 历史）

用户在候选验收后明确授权，激活回执 `20261030T133438Z` 已保存到服务器 `~/embodied-agent/current-release.json`，真实访问地址也由此读取；仓库仅保存 URL 模板 `https://<tailnet-host>:8449/`。访问端需要连接原 Tailnet，页面使用 WSS。

首次启动时，应用在容器内健康，但内部 Docker 网络没有实际发布声明的 127.0.0.1:8390 映射。该尝试停止了新服务，未改其他 Serve 映射；日志保留为 `activation-20261030T132416Z.json`。随后确认宿主直接访问本项目内部容器 IP 正常，已安装 Tailscale 1.102.2 支持该代理目标，因此移除无效 ports 声明，保持 internal 网络及无宿主端口。没有新增外部网络、代理容器或宿主防火墙规则。

当前运行文件为 `~/embodied-agent/runtime/compose-9b1c4be840f31b21.yaml`，SHA-256 为 `9b1c4be840f31b21bf4fa54c4e77a4c5530db1ced4e93d48c32b53becbed6b5b`。配置与应用镜像分开记录，原 `212f912` 源码 bundle 没有被改写。

激活后实际核验：

- 容器镜像 ID 与候选一致，1 CPU / 1536 MiB / 128 PID、只读根、非 root、drop ALL capabilities；只有本项目 internal 网络，无宿主端口绑定。
- 持久卷为 `embodied-agent-cloud_runtime`，挂载 `/app/outputs`；审计标记在本项目容器 restart 后完整读取。
- 客户端经真实 HTTPS 完成健康和 HTML 读取、JPEG 获取；带真实 Origin 的 WSS 建连并完成离线对话回复。
- 场景图已人工检查，机械臂、红色方块和蓝色目标区域正常。浏览器控制工具未连接，本轮**没有完整浏览器交互/录音验收**。
- 其他 39 个运行容器保持原 ID，其他 Serve TCP/Web 映射未变，Funnel 仍关闭。

机器可读证据见 [cloud-activation-20261030.json](../eval/cloud-activation-20261030.json)。本轮没有改动 Python 应用，验收针对部署配置与真实访问链，不重复引用旧单测数字为新的运行证据。

## 重启与后续升级

操作前读取 `current-release.json`，使用其确切 Compose 路径和应用 SHA。普通 restart 后核对健康、容器 IP 与 Serve 8449 目标；recreate/升级后必须重新发现本项目内部 IP 并更新这一个映射，不能假定原 IP 永远不变。保持原数据卷，未知操作或资源隔离须按执行契约对账，不能通过换卷清空。

本版本仍是离线规划、模拟音频的单进程仿真参考入口。真实 Provider、独立现场 Guardian、真机停止与恢复不属于本次上线能力。
