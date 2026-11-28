# 云端仿真控制台

> 2026-11-29，私网 HTTPS 8449 常驻仿真控制台已激活 `77b754d`，运行账本 v3、离线规划、模拟音频和隔离物理控制。后续 Git 提交与实际激活分别记账。

## D042 浏览器文本回执（2026-11-29）

激活回执 `d042-20261128T162907Z`，应用 `77b754d8ac7dcf2300570f27f5fc50ca19b6f310`。备份现有 v3 账本后更新应用，原命名卷、操作者双作用域、私网映射和其余 39 个容器均保持。已用前版 `463199e` 回读候选新写入的 v3 历史。

Edge 在私网正式页空闲 76.9 秒后只点击一次发送。页面显示抓取与放置均完成，账本从 22 个任务、40 个操作变为 23 个任务、42 个操作，原八表业务行全部保留。无动作探测、文本回执丢失提示和不重放分别经过故障注入。相机预览曾短暂出现破图，随后解码为 640 × 480；真实麦克风和扬声器尚待实测。[客户端、候选及常驻证据](../eval/console-text-receipts-20261129.json)。

## D041 常驻账本 v3 升级（2026-11-28）

激活回执 `d041-20261128T153630Z`，应用 `463199e2fc41d7387f3541f314c79bf70bf629fe`，镜像 `sha256:4def79751cf0a032930e0e8bf7991cf7bb0b5c9f43c3c99d4fef418d7a0b52b4`。维护窗口重新取得独占权，生成认证 v2 备份并核对七表历史投影，迁移前后摘要均为 `1358ccc465b89ef7ab4fef343ed341cb891277175aaf0bb8516b7bde5e25c230`。沿用原命名卷、Compose、私网路由和双作用域操作者策略。

外部 Windows 客户端通过真实 HTTPS/WSS 完成仿真抓取及放置，两步效果均为 SAT。随后重启常驻控制台，21 个任务、38 个操作和 38 次尝试的八表总摘要保持 `9fb825bc68abff900904caa7c67f12db5384a87bfe8fda144038f4e48da41ddd`，历史任务可查，旧场景的执行权限维持关闭。39 个相邻运行容器和其他 Serve 映射保持原状。重启后 JPEG 预览经历 10 次 HTTP 503 后返回 200，属于需要跟踪的冷启动体验。[常驻验收回执](../eval/cloud-v3-upgrade-20261128.json) · [隔离演练与跨主机证据](../eval/controlled-step-retry-20261128.json)。

## D034 升级（2026-11-22 历史）

应用由 `90135f0` 升级至 `cb05aeeb3ada4c8a64930f3e00006b97773463a6`，激活回执 `20261122T042559Z`，镜像 ID `sha256:f4c5a4946d2cce1ed434a20f3c689e7cbb58bf34f9a5f03a202abaf0932229d0`。在控制台内默认启用 `IsolatedTabletopSim`，将 MuJoCo 物理步进、逆解、驱动 Guard 与 `SimStopController` 迁入独立无头物理控制子进程，继续保留 D026 独立离屏渲染子进程，未改 schema v2。

继续沿用运行文件 `/home/ubuntu/embodied-agent/runtime/compose-55a9bea3a639e02d.json`（SHA-256 `55a9bea3a639e02dc99b38ba9a6d07e73c608645b4f9fd8587a119755be7b0b7`）与只读双作用域策略 `/home/ubuntu/embodied-agent/runtime/operator-policy-4ad094e27f820e05.json`（`scopes: ["reconcile_effect", "reconcile_stop"]`，`policy_digest` `33d665e9ba1dd084...`），保持原代理网关、原唯一主体、internal 网络、无宿主端口及 1 CPU / 1536 MiB / 128 PID 只读根限制。

升级前备份并校验 19 tasks / 34 operations / 34 attempts / 132 operation_events / 13 task_plans / 71 task_events 逐行保留。升级后实测容器内三进程分离：PID 1（`embodied console`）、PID 8（`control_worker`，PPID 1）、PID 25（`compute_worker` 渲染，PPID 1）；抓放与重启后账本推进至 20 tasks / 36 operations / 36 attempts / 139 operation_events / 14 task_plans / 77 task_events，七表行摘要 `1299f223b87950dd...` 持久保留。外部真实 Tailscale Serve HTTPS 8449 验证 `/healthz` 报告 `control_isolation: "process"`、伪造身份头被 Serve 覆盖且保持双作用域与 `task_execution_authorized: false`、重启后只读任务评估生效；其他 39 个容器和 Serve 映射保持，Funnel 关闭。证据见 [cloud-physical-isolation-upgrade-20261122.json](../eval/cloud-physical-isolation-upgrade-20261122.json)。

## D032 升级（2026-11-22 历史）

应用由 `5b080dd` 升级至 `90135f05c95d5e974c4cb4e7b898346622fd9dd8`，激活回执 `20261122T012458Z`，镜像 ID `sha256:146a73f5933d2855c92db57a97fe962d915e61a9f5dcd721f99792cdf198c877`。在受控 HTTP 入口新增显式作用域隔离及一次性 `reconcile_stop` 审计授权，未改 schema v2。

运行文件为 `/home/ubuntu/embodied-agent/runtime/compose-55a9bea3a639e02d.json`，SHA-256 `55a9bea3a639e02dc99b38ba9a6d07e73c608645b4f9fd8587a119755be7b0b7`，挂载只读策略 `/home/ubuntu/embodied-agent/runtime/operator-policy-4ad094e27f820e05.json`（SHA-256 `4ad094e27f820e05549aeab583c26755fa12a7be2f78c117649fc34a5755d46d`，`policy_digest` `33d665e9ba1dd084f07c038f13a5d7fb96ae7d632e0ad887e6062550780d9d00`）。策略仅显式补充 `scopes: ["reconcile_effect", "reconcile_stop"]`，保持原代理网关、原唯一登录主体、internal 网络、无宿主端口和 1 CPU / 1536 MiB / 128 PID 只读根限制；旧策略与旧 Compose 文件均保留。

升级前 14 tasks / 28 operations / 28 attempts / 101 operation_events / 8 task_plans / 48 task_events 逐行保留。外部真实入口验收后最终为 19 tasks（12 FINISHED / 6 LEGACY / 1 INTERRUPTED）、34 operations（33 RETURNED / 1 OUTCOME_UNKNOWN）、34 attempts（33 NOT_REQUESTED / 1 MOTION_STOPPED_OBSERVED）、132 operation_events、13 task_plans、71 task_events。

真实入口验证：Serve 覆盖伪造身份头，容器内直连伪造返回 403；实测 WSS 中断触发隔离与新指令拒绝、未停前效果重查拒绝、停止确认拒绝保持隔离、二次确认完成 `MOTION_STOPPED_OBSERVED`（审计行序 114→115→116→117→118）且原操作保持 `OUTCOME_UNKNOWN`、重复确认与重签停止拒绝、随后新 WSS 回零与抓放及一次性效果重查通过；容器重启后七表行摘要 `37251619081aff54...` 完全一致，预留待确认凭据被 409 拒绝。其他 39 个容器和 Serve 映射保持，Funnel 关闭，仍为仿真/离线规划/模拟音频，未授权 Guard 复位或任务续跑。证据见 [cloud-stop-upgrade-20261122.json](../eval/cloud-stop-upgrade-20261122.json)。

## D030/D031 升级（2026-11-04 历史）

应用由 `a29ad0e` 升级至 `5b080dd9d5add932e236aebbc73e581a656687d2`，激活回执 `20261104T150233Z`，镜像 ID `sha256:c5ba7f40c28a4911dcc4495c18b56b307705a1c5468107948260fecbac102f82`。新增受控 HTTP 的 Serve 身份和一次性效果重查审计；同时分离本体反馈与语义采样，TTL 与物理参数不变。

运行文件为 `/home/ubuntu/embodied-agent/runtime/compose-302d04dae607c0a7.json`，SHA-256 `302d04dae607c0a72cd334ce21cebb6e068d6f39099b329f2c81cb9735156b98`，只增加操作者策略的只读 bind mount 和对应 CLI 参数；其他资源/环境配置保持。策略精确绑定代理网关及一个登录主体，没有修改 Tailnet 策略、新增密钥或发布宿主端口。旧 `9b1c4be840f31b21` 配置保留为历史。

升级前 12 tasks / 24 operations / 24 attempts / 82 operation_events / 6 task_plans / 36 task_events 逐行保留。最终 14 tasks / 28 RETURNED operations / 28 attempts / 101 operation_events / 8 task_plans / 48 task_events。效果重查新增一条效果记录及签发/消费/结束审计，另一次待确认签发在重启后失效；七表行摘要在该重启前后相同，没有自动重放。

真实入口验证：Serve 覆盖伪造身份头，直连伪造身份被拒绝，成功消费一次、重复消费拒绝，旧 challenge 在重启后拒绝。其他 39 个容器和 Serve 映射保持，Funnel 关闭，仍为仿真/离线规划/模拟音频。普通 HRI/RPC 不因此获得统一认证；只开放效果重查，不授权停止、复位、技能重试或任务续跑。证据见 [cloud-operator-upgrade-20261104.json](../eval/cloud-operator-upgrade-20261104.json)。

中间候选 `be8cc7d/e1b2706` 未激活，失败运行和配置预演均保留。color 稳定性边界与最终回归见 [body-feedback.md](body-feedback.md)，不把后续通过当成所有早期失败已归因。

## D029 升级（2026-11-04 历史）

应用由 `c1fc926` 升级至 `a29ad0e796c201d7ea1eb1ead5f7d3d946553f4f`，激活回执 `20261104T123844Z`，镜像 ID `sha256:615787e0467da6996ef1da776d51969fa56c0c7beb60c08d9f2a212e749809ab`。恢复评估新增一致快照、操作身份绑定与明确拒绝执行授权；Guard 生命周期变化和传输/等待老化均纳入失效检查。

维持原 v2 schema、运行配置及持久卷。升级前 10 tasks / 20 operations / 20 attempts / 68 operation_events / 4 task_plans / 24 task_events 在升级、抓放和 restart 后逐行保留；外部 HTTPS/WSS 后为 12 tasks / 24 RETURNED operations / 6 task_plans / 36 task_events。全部任务审查通过一致性检查且未产生写入或重放。

Linux 候选、三种真实仿真审查路径和真实入口验证通过；其他 39 个容器与 Serve 映射保持。仍为私网仿真、离线规划、模拟音频；受信操作者认证、执行恢复、完整浏览器/麦克风及真机未验收。证据见 [cloud-review-upgrade-20261104.json](../eval/cloud-review-upgrade-20261104.json)。

## D028 升级（2026-11-04 历史）

应用由 `f27d0ac` 升级至 `c1fc92643ca739dc7f9a33f34ddabc62bfd05a5d`，激活回执 `20261104T113328Z`，镜像 ID `sha256:6cd3883e5b5adfde91d1f488632c60c084bfecfa70b2ef33c1e6725259d615c7`。修复持久化失败导致批量撤权或监督 Guard 闩锁遗漏的问题；既有 v2 schema、运行配置和数据卷保持。

升级前备份包含 8 tasks / 16 operations / 16 attempts / 54 operation_events / 2 task_plans / 12 task_events；升级、抓放和重启后旧行逐条保留。真实 HTTPS/WSS 验证后为 10 tasks / 20 RETURNED operations / 4 task_plans / 24 task_events，全部任务查询无写入或重放。

Linux 隔离候选与 SQLite FULL 取消检查通过，常驻健康、资源限制、39 个其他容器及 Serve 映射保持。仍为私网仿真/离线规划/模拟音频；未声称物理急停、完整浏览器/麦克风或真机验收。详见 [cloud-cancellation-upgrade-20261104.json](../eval/cloud-cancellation-upgrade-20261104.json)。

首次部署前置检查 `20261104T113212Z` 在停写后的只读卷上无法打开 SQLite WAL 共享内存文件，未迁移 schema 或写入业务数据。先恢复原 v2 服务，再改为检查停写后的完整临时副本，第二次激活成功；未覆盖备份或丢弃记录，失败日志保留。

## D027 升级（2026-11-04 历史）

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
