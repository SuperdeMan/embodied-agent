# car-agent 复用评估与迁移清单

> 状态：v1.0（2026-08-04，基于对 car-agent 全库的勘察）。car-agent 位于同级目录 `../car-agent`，Apache-2.0，约 17.5 万行代码（非测试约 6.5 万行）、3996 个通过测试、54k 行设计文档。
> 关联：[架构设计](architecture.md) §4-§5 说明各资产在新架构中的落位。

## 1. 结论

- **直接挪用约 1.7 万行**：LLM/ASR/TTS/S2S 多供应商网关、记忆服务、Agent SDK 工具层、可观测、注册中心、权限引擎、浏览器语音全双工栈。
- **改造后复用约 7 千行**：云端规划-执行引擎（planning/DAG executor/verify/context/skills 检索）——这是 car-agent 最有价值的设计资产，其「LLM 只出计划、确定性执行、执行后验证世界状态」对机器人是刚需。
- **只学设计不搬代码**：边缘快慢双路仲裁模式、15 个领域 agent、座舱 UI、车控知识库。
- **总体**：car-agent 非测试代码的 25-30% 可迁移，另有一整套工程方法论（proto 先行、manifest 声明式扩展、golden-gate 评测、契约测试钉红线）全盘继承。**复用的是模块与契约，不是 35 服务的微服务拓扑**——新项目单机三进程，car-agent 服务以库形式内嵌。

## 2. 分模块判定

分级：**A 直接挪用**（复制后小改即用）/ **B 改造复用**（保留骨架，替换领域概念）/ **C 只参考设计** / **D 不复用**。

| car-agent 模块 | LOC | 级 | 在新项目的落位与改造点 |
|---|---|---|---|
| `llm-gateway/providers.py` + `llm_runtime.py` + `cache.py` + `ratelimit.py` + `metrics.py` | ~4,200 | A | → `src/embodied/providers/`。五族 provider（LLM/ASR/流式ASR/TTS/流式TTS）+ 工厂 + 热切换 + 降级链，零车辆概念。新增 PolicyProvider/PerceptionProvider 两族，仿照同一模式 |
| `llm-gateway/s2s/`（session/provider/protocol/reflux） | 1,183 | A | → `src/embodied/hri/s2s/`。改两处常量：persona 与 `escalate` 工具描述（均已是 env 可覆盖）。「S2S 无执行通道」红线原样继承 |
| `memory/`（store/pg_store/extract/relation/weighting） | 3,321 | A | → 独立可选服务或内嵌库。全部领域无关；空间记忆作为语义记忆子类扩展 |
| `agents/_sdk/`（ledger/grounding/manifest/testing/http 等） | 2,734 | A | → `src/embodied/runtime/` 与技能 SDK。manifest 加载器改造为 SkillManifest；`landmark.py` 弃用 |
| `observability/`（events/logging/tracing/collector） | 1,752 | A | → `src/embodied/runtime/obs/`。NATS 传输改为进程内直写 SQLite/文件 sink，事件 subject 改名 |
| `registry/` | 909 | A | → 技能注册中心底座（Postgres 换 SQLite 起步），语义检索能力保留给技能路由 |
| `runtime/`（grpcio 工厂/privacy_registry） | 687 | A | → `src/embodied/runtime/`。gRPC keepalive/优雅退出/mTLS 工厂原样用；privacy_registry 的 `context_scopes` 声明制平移到相机/位置数据 |
| `security/`（permission/injection/content/audit） | 590 | A/B | 引擎直接用；scope 命名从车控域改为机器人域（`arm.move`、`gripper.close`、`camera.read`…） |
| `hmi/src/` 语音栈（voiceLoop/vadEngine/kwsEngine/pcm*/ttsQueue/s2sClient/audio.ts） | ~3,550 | A | → `console/`。作者已把 FSM 设计为引擎无关（只消费 speech-start/end），唤醒词换预设即可。真机原生音频阶段，VoiceLoop 状态机设计平移到 Python + sherpa-onnx |
| `orchestrator/cloud/`（engine/planning/executor/loop/context/verify/session/skills/exemplars/dispatch/circuit） | 6,908 | B | → `src/embodied/cognition/`。核心改造：`VehicleStateMirror`→`WorldState`（本体+物体注册表+场景图）；计划叶子从 agent 调用→技能调用；`dispatch` 的 edge-call 路由→realtime-control 调用；`verify.eval_state_match` 语义不变（对机器人更关键）。skills/exemplars 的「YAML 声明式规划知识 + 双路检索 + few-shot 注入」机制保留，内容重写 |
| `proto/`（agent/llm/audio/memory/registry） | ~600 | B | 可移植部分直接继承并重命名 package（`embodied.<service>.v1`）；`channel.proto`/`orchestrator.proto` 含 vehicle 概念，重写 |
| `proactive/`（governor/delivery_store） | 1,391 | B | 主动播报仲裁器：`driving-load` 门改为 `robot-busy/task-state` 门。M1 后按需接入 |
| `orchestrator/edge/nlu.py` + `scripts/train_edge_nlu.py` | 766 | B | 端侧意图分类器（WordPiece+ONNX，3-8ms）：断网降级的受限指令集入口，用机器人语料重训 |
| `gateway/`（Go：WS hub/auth/幂等/双向流） | 2,980 | C | 单机形态暂不需要独立网关；若未来做远程接入/多机，其 WS↔gRPC 与幂等设计是模板 |
| `orchestrator/edge/`（fast_intent/val/capabilities/knowledge yaml） | ~5,000+2,162 | C | 内容是 1861 行中文车控正则 + 车辆抽象层，一行都不搬；但「高频安全关键走确定性快路、复杂任务走云端慢路」的双路模式正是机器人 reflex/planner 分层的原型 |
| `agents/` 15 个领域 agent | ~19,400 | C | 领域不同。值得读的模式：`mcp_bridge/`（外部工具准入控制）、`reminder/timeparse`、`scene_orchestrator/`（触发器编排）、`deep_research/` |
| `hmi/src/components/` 座舱 UI、`dashboard/` | ~12,000 | C | 控制台重写，卡片/设置面板的信息架构可参考 |
| `scripts/run_e2e.py` + `e2e_contract.py` | ~10,500 | C/B | E2E 编排思想（lane/manifest/陈旧策略）到 M2 评测 harness 时再决定搬多少，先不背这个重量 |
| `payment-gateway/`、车控知识库内容、导航/充电/泊车 agent | — | D | 领域不相干 |

## 3. 迁移执行清单（按阶段）

**P0（M0 执行）**：`providers/`（LLM 部分先行）、`runtime/`、`observability/`（含 sink 改造）、`agents/_sdk/` 的 ledger/manifest/testing、`security/permission`、`.env.example` 精简版。
**P1（M1 执行）**：`orchestrator/cloud/` 规划-执行引擎改造、`proto/` 继承部分、HMI 语音栈接入 console、providers 的 ASR/TTS 部分、`registry/`。
**P2（M2+ 按需）**：`memory/` 全量、S2S、proactive、edge NLU、E2E harness 思想。

### 执行记录

- **P1 主体完成（2026-08-05）**：①规划-执行引擎（plan/plan_builder/executor/verify/engine，改造要点见各文件头）；②providers 音频段全量（audio.py 1196 行，21 类，与源 diff 仅格式差异）；③控制台语音：**HMI 栈按「设计移植、代码精简重写」处理**——v0 采用按住说话（无 VAD/KWS），浏览器端 pcm.js 仅承载采集/回放，car-agent 的 VoiceLoop 状态机、silero 端点、sherpa KWS 与打断体系推迟到 S2S 升级时按原设计接入（端点判定权在客户端这一结论已内建到 hri.v0 协议）。proto 与 registry 的移植随三进程拆分执行。

- **P0 完成（2026-08-04）**：providers LLM 体系（llm/runtime/cache/ratelimit/health，Redis 剥离）、obs（events 改 JSONL sink、logging、tracing 可选 OTel、redact 随行）、ledger（PG → in-memory，公开 API 不变）、privacy scope 过滤、permission + 机器人域 scopes、testing 助手。约 2,300 行实现 + 189 个测试随行/新增，全部通过。
- **对本文档的两处事实修正**：① `context_scopes` 过滤机制的真实来源是 `orchestrator/cloud/clients.py`（`_SENSITIVE_SCOPE` + `_merge_meta`）；`runtime/privacy_registry.py` 实为 GDPR 目标注册表，其 adapter 依赖未移植模块，留 M1。② `observability/collector/` 未搬，由 JSONL sink 替代（按计划）。

## 4. 迁移规矩（写给未来的每次移植）

1. **复制改造，不做跨仓依赖**：embodied-agent 永不 import car-agent；文件头注明来源与改造说明（`# Ported from car-agent <path> @ <commit>, changes: ...`），License 同为 Apache-2.0，合规。
2. **测试随行**：car-agent 模块自带测试的，测试一起搬并改造；搬完 `uv run pytest` 绿才算完成。
3. **禁止搬运 `.env`**：car-agent 的 `.env` 含 30+ 真实密钥，**只允许参考 `.env.example` 的变量名**。新项目密钥全部重新申请或按需填入，绝不复制文件。
4. **概念改名彻底**：vehicle/cockpit/座舱 术语不得残留在新代码中（契约测试可加词表扫描）。
5. **能不搬就不搬**：清单外模块想搬时，先在 decisions.md 记一条再动手。
