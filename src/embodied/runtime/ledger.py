# Ported from car-agent agents/_sdk/ledger.py @ f0b08f8, changes: Postgres/asyncpg storage replaced by an in-process dict store with the same public async API (pg_ok renamed ready; dsn param dropped); PG row converters (row_to_task/_epoch/_jsonb), ledger_schema.sql and the GDPR PERSONAL_DATA_TARGETS wiring dropped; pure logic (idem key / budgets / orphan detection / state machine) unchanged.
"""Task Ledger：跨轮持久任务账本。

一句话定位：**「谁在替用户干活、干到哪了、还让不让它干」的唯一权威记录。**

**为什么存在**：异步长任务（深度调研、慢技能）若只是进程内 asyncio.create_task——
无法取消、无预算、用户问「干得怎么样了」没人答得上。会话态只盖确认/补槽挂起窗
（秒-分钟），Ledger 管的是任务生命周期（分钟-小时、跨会话），两者分层不重叠。

**存储（本移植的改造点）**：origin 用 Postgres 换取跨重启诚实；M0 单机单进程，
存储降为进程内 dict、公开 API 不变（全部保持 async），后续可换持久后端而不动调用面。
注意：进程重启即丢——ack 话术不应承诺跨重启可查询（诚实降级原则继承）。

**cancel 走拉模式（零新通道）**：用户说「别干了」→ 执行方 `cancel()` 置态 →
后台任务下一次 `heartbeat()` 读到 `cancelled` 自行收尾。取消延迟上限≈心跳间隔（≤10s）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace

logger = logging.getLogger("runtime.ledger")

# 状态机（单向，终态不可逆；orphaned 是**判定**不是结局，见 heartbeat 的复活分支）
ACCEPTED, RUNNING = "accepted", "running"
DONE, FAILED, CANCELLED, ORPHANED = "done", "failed", "cancelled", "orphaned"
ACTIVE = (ACCEPTED, RUNNING)                 # 用户可见的「在跑」态
TERMINAL = (DONE, FAILED, CANCELLED)         # 真终态（orphaned 可被迟到心跳复活）

# 停止原因（写进 budget.stop_reason，供上层区分话术：用户取消 / 超时停了 / 预算用尽）
STOP_USER, STOP_DEADLINE, STOP_BUDGET = "user", "deadline", "budget"

# 心跳节律建议 ≤10s；ORPHAN_TTL 默认 90s ≈ 9 个心跳的余量（防抖动误判）
DEFAULT_ORPHAN_TTL_S = 90.0
_PUNCT_RE = re.compile(r"[\s，,。.！!？?、；;：:~～\-—_“”\"'‘’()（）]+")


@dataclass
class LedgerTask:
    """一条任务账目。时间统一 epoch 秒（float）。"""
    task_id: str
    user_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    kind: str = ""
    goal: str = ""
    idempotency_key: str = ""
    status: str = ACCEPTED
    progress: str = ""
    budget: dict = field(default_factory=dict)
    result_ref: dict = field(default_factory=dict)
    origin_trace_id: str = ""
    heartbeat_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def stop_reason(self) -> str:
        """主动截停时写入的原因（user|deadline|budget），无则空串。"""
        return str((self.budget or {}).get("stop_reason") or "")

    @property
    def active(self) -> bool:
        return self.status in ACTIVE


@dataclass
class Duplicate:
    """幂等命中：同一 (user_id, kind, 归一化 goal) 已有在跑任务。

    供上层出「已经在干了，大概还要 N 分钟」话术——防连说两遍/重试风暴双跑。
    """
    existing: LedgerTask


# ── 纯函数（无 IO，可离线单测；存储层只做搬运）─────────────────────────────

def normalize_goal(goal: str) -> str:
    """幂等键的目标归一：去标点空白 + 小写。中文小写是空操作，英文主题受益。"""
    return _PUNCT_RE.sub("", (goal or "").strip()).lower()


def idem_key(user_id: str, kind: str, goal: str) -> str:
    """sha256(user_id|kind|归一化 goal)[:16]。"""
    raw = f"{user_id}|{kind}|{normalize_goal(goal)}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def budget_exhausted(budget: dict | None, now: float | None = None) -> str:
    """预算是否用尽 → 返回停止原因（""=没用尽）。

    - `deadline_ts`（epoch 秒）过期 → `deadline`
    - `llm_calls_used >= llm_calls_max` / `ext_calls_used >= ext_calls_max` → `budget`
    上限缺省/非数 = 不限（弱声明不制造误截停）。
    """
    b = budget or {}
    now = time.time() if now is None else now
    try:
        deadline = float(b.get("deadline_ts") or 0)
    except (TypeError, ValueError):
        deadline = 0.0
    if deadline and now >= deadline:
        return STOP_DEADLINE
    for used_k, max_k in (("llm_calls_used", "llm_calls_max"),
                          ("ext_calls_used", "ext_calls_max")):
        try:
            cap = float(b.get(max_k) or 0)
            used = float(b.get(used_k) or 0)
        except (TypeError, ValueError):
            continue
        if cap and used >= cap:
            return STOP_BUDGET
    return ""


def merge_used(budget: dict | None, used: dict | None) -> dict:
    """把本次心跳上报的用量累加进 budget（只累加 *_used 计数键，不动上限）。"""
    b = dict(budget or {})
    for k, v in (used or {}).items():
        key = k if k.endswith("_used") else f"{k}_used"
        try:
            b[key] = float(b.get(key) or 0) + float(v or 0)
        except (TypeError, ValueError):
            continue
        if b[key].is_integer():
            b[key] = int(b[key])
    return b


def is_orphaned(status: str, heartbeat_at: float, created_at: float,
                now: float | None = None, ttl: float | None = None) -> bool:
    """惰性判定：active 态但超过 ORPHAN_TTL 没心跳 = 崩溃/重启遗留的孤儿。

    从未心跳过（accepted 刚开单就崩）时以 created_at 为参照。判定只在**读**的时候做
    （query_active/get），不额外起扫描进程。
    """
    if status not in ACTIVE:
        return False
    now = time.time() if now is None else now
    ttl = orphan_ttl() if ttl is None else ttl
    ref = heartbeat_at or created_at
    return bool(ref) and (now - ref) > ttl


def orphan_ttl() -> float:
    try:
        return float(os.getenv("LEDGER_ORPHAN_TTL_S", "") or DEFAULT_ORPHAN_TTL_S)
    except ValueError:
        return DEFAULT_ORPHAN_TTL_S


def _snapshot(task: LedgerTask) -> LedgerTask:
    """返回账目快照（与 origin 的 row→dataclass 语义一致：调用方改快照不脏账本）。"""
    return replace(task, budget=dict(task.budget or {}),
                   result_ref=dict(task.result_ref or {}))


# ── 存储层 ─────────────────────────────────────────────────────────────────

class TaskLedger:
    """进程内任务账本（origin 为 Postgres；同一公开 API，见模块 docstring）。

    单进程单事件循环下，每个方法体内无 await 打断，check-then-write 天然原子——
    origin 用 INSERT ... ON CONFLICT 交给数据库裁决的并发窗在这里不存在。
    """

    def __init__(self):
        self._tasks: dict[str, LedgerTask] = {}
        self._init_done = False

    @property
    def ready(self) -> bool:
        """存储是否可用（in-memory 恒 True；origin 的 pg_ok 对应位）。"""
        return True

    async def init(self) -> bool:
        """幂等初始化。in-memory 无池无表，保留调用面（恒 True）。"""
        self._init_done = True
        return True

    # ── 开单 ──
    async def open(self, user_id: str, session_id: str, agent_id: str, kind: str,
                   goal: str, *, budget: dict | None = None,
                   origin_trace_id: str = "",
                   idempotency_goal: str = "") -> LedgerTask | Duplicate | None:
        """开一条任务账目。

        同 (user_id, idempotency_key) 已有 active 任务 → `Duplicate`（上层据此出
        「已经在干了」话术，不重复开跑）。幂等命中的可能是个孤儿（上次崩了没销单）
        ——那不算「已经在干了」，就地改判后放行新开单，否则用户永远被一条尸体挡住重试。

        idempotency_goal：幂等指纹的来源文本（缺省用 goal）。规划槽位会波动，
        用用户原话作指纹可保证同一句话命中同一任务。
        返回 None 仅保留给「存储不可用」的降级语义（持久后端接入后生效）。
        """
        key = idem_key(user_id, kind, idempotency_goal or goal)
        existing = self._latest_active(user_id, key)
        if existing is not None:
            if is_orphaned(existing.status, existing.heartbeat_at,
                           existing.created_at):
                self._mark_orphaned(existing)
            else:
                return Duplicate(existing=_snapshot(existing))

        now = time.time()
        task = LedgerTask(
            task_id=uuid.uuid4().hex,   # 禁 id(obj)：内存地址 GC 复用会撞键
            user_id=user_id,
            session_id=session_id or "",
            agent_id=agent_id,
            kind=kind,
            goal=goal,
            idempotency_key=key,
            status=ACCEPTED,
            budget=dict(budget or {}),
            origin_trace_id=origin_trace_id or "",
            heartbeat_at=now,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        return _snapshot(task)

    # ── 心跳（cancel 拉模式 + 预算强制的载体）──
    async def heartbeat(self, task_id: str, *, progress: str = "",
                        used: dict | None = None) -> str:
        """打一次心跳，返回**当前 status**。

        - 返回 `cancelled` → 任务自行收尾退出（用户取消 / 超 deadline / 预算用尽；
          具体原因经 `get(task_id).stop_reason` 区分话术）。
        - `used` 累加进 budget 计数（如 `{"llm_calls": 1}`）；累加后若超上限，就地
          置 `cancelled` 并写 `stop_reason`——预算强制在此兑现。
        - **迟到心跳复活**：被惰性判定成 orphaned 的任务若又打上心跳，说明它其实活着
          → 拉回 running。orphaned 是判定不是结局，误判不该变成假的中断报告。
        - 行不存在（被清理/从没开成）→ 返回 `cancelled`，后台任务据此收尾不空转。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return CANCELLED
        if task.status not in ACTIVE + (ORPHANED,):
            # 不在可推进态（已 done/failed/cancelled）→ 原样回报，
            # 拉模式的 cancelled 正是从这条路返回的。
            return task.status

        task.status = RUNNING
        if progress:
            task.progress = progress
        now = time.time()
        task.heartbeat_at = now
        task.updated_at = now

        merged = merge_used(task.budget, used)
        reason = budget_exhausted(merged)
        if reason:
            merged["stop_reason"] = reason
            task.budget = merged
            task.status = CANCELLED
            logger.info("TaskLedger: 任务 %s 因 %s 截停", task_id[:8], reason)
            return CANCELLED
        if used:
            task.budget = merged
        return task.status

    # ── 销单 ──
    async def close(self, task_id: str, status: str, *,
                    result_ref: dict | None = None, progress: str = "") -> bool:
        """终态落账（done|failed|cancelled）。终态不可逆：已终态的行不再被覆盖。"""
        if status not in TERMINAL:
            raise ValueError(f"close 只接受终态 {TERMINAL}，收到 {status!r}")
        task = self._tasks.get(task_id)
        if task is None or task.status in TERMINAL:
            return False
        task.status = status
        task.result_ref = dict(result_ref or {})
        if progress:
            task.progress = progress
        task.updated_at = time.time()
        return True

    async def cancel(self, task_id: str, *, reason: str = STOP_USER) -> bool:
        """置取消态（拉模式：后台任务下一次心跳读到即自行收尾）。已终态返回 False。"""
        task = self._tasks.get(task_id)
        if task is None or task.status in TERMINAL:
            return False
        task.status = CANCELLED
        task.budget = {**(task.budget or {}), "stop_reason": reason}
        task.updated_at = time.time()
        return True

    # ── 读 ──
    async def query_active(self, user_id: str, *, kind: str = "",
                           limit: int = 10) -> list[LedgerTask]:
        """该用户「还在跑」的任务（含惰性 orphaned 改判后剔除）。最近受理在前。"""
        rows = self._user_tasks(user_id, kind)
        out: list[LedgerTask] = []
        for task in rows:
            if task.status not in ACTIVE:
                continue
            if is_orphaned(task.status, task.heartbeat_at, task.created_at):
                self._mark_orphaned(task)
                continue
            out.append(_snapshot(task))
            if len(out) >= limit:
                break
        return out

    async def recent(self, user_id: str, *, kind: str = "",
                     limit: int = 5) -> list[LedgerTask]:
        """最近任务（含终态），供「刚才那个任务怎么样了」在无 active 时回答。

        孤儿保留但改标 orphaned——要能答出「干到一半中断了」。
        """
        rows = self._user_tasks(user_id, kind)[:limit]
        out: list[LedgerTask] = []
        for task in rows:
            if is_orphaned(task.status, task.heartbeat_at, task.created_at):
                self._mark_orphaned(task)
            out.append(_snapshot(task))
        return out

    async def get(self, task_id: str) -> LedgerTask | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if is_orphaned(task.status, task.heartbeat_at, task.created_at):
            self._mark_orphaned(task)
        return _snapshot(task)

    # ── 内部 ──
    def _user_tasks(self, user_id: str, kind: str) -> list[LedgerTask]:
        rows = [t for t in self._tasks.values()
                if t.user_id == user_id and (not kind or t.kind == kind)]
        rows.sort(key=lambda t: t.created_at, reverse=True)
        return rows

    def _latest_active(self, user_id: str, key: str) -> LedgerTask | None:
        rows = [t for t in self._tasks.values()
                if t.user_id == user_id and t.idempotency_key == key
                and t.status in ACTIVE]
        rows.sort(key=lambda t: t.created_at, reverse=True)
        return rows[0] if rows else None

    @staticmethod
    def _mark_orphaned(task: LedgerTask) -> None:
        """惰性改判。写前再钉一次判定（origin 在 UPDATE WHERE 里重验 TTL 的对应位）。"""
        if task.status in ACTIVE and is_orphaned(task.status, task.heartbeat_at,
                                                 task.created_at):
            task.status = ORPHANED
            task.updated_at = time.time()

    async def close_pool(self) -> None:
        """保留 origin 的关停调用面；in-memory 无池，清空即可重复使用。"""
        return None
