# Ported from car-agent agents/_sdk/tests/test_ledger.py @ f0b08f8, changes: pure-function tests kept verbatim; fake-asyncpg branch tests rewritten against the in-memory store; PG-degradation, row_to_task and SQL/schema source assertions dropped (storage adapted to in-memory).
"""Task Ledger 单测：状态机 / 幂等 / orphan 惰性判定 / 预算截停（in-memory 存储）。

分层策略沿 origin：纯函数层离线全覆盖；存储分支直接驱动 in-memory 实现断言行为。
"""
import time

import pytest

from embodied.runtime.ledger import (
    ACCEPTED,
    ACTIVE,
    CANCELLED,
    DONE,
    FAILED,
    ORPHANED,
    RUNNING,
    STOP_BUDGET,
    STOP_DEADLINE,
    STOP_USER,
    TERMINAL,
    Duplicate,
    LedgerTask,
    TaskLedger,
    budget_exhausted,
    idem_key,
    is_orphaned,
    merge_used,
    normalize_goal,
    orphan_ttl,
)

_STALE = 10_000  # 秒：远超默认 ORPHAN_TTL(90s)


# ── 纯函数：幂等键 ─────────────────────────────────────────────────────────

def test_idem_key_ignores_punctuation_and_whitespace():
    """「查一下固态电池」与「查一下 固态电池。」是同一个任务——连说两遍不该双跑。"""
    a = idem_key("u1", "research", "深入调研固态电池现状")
    b = idem_key("u1", "research", " 深入调研，固态电池现状。 ")
    assert a == b and len(a) == 16


def test_idem_key_separates_user_kind_and_goal():
    base = idem_key("u1", "research", "固态电池")
    assert idem_key("u2", "research", "固态电池") != base     # 换人
    assert idem_key("u1", "trip", "固态电池") != base         # 换类型
    assert idem_key("u1", "research", "钠离子电池") != base    # 换主题


def test_normalize_goal_keeps_content_chars():
    assert normalize_goal("A/B 对比！") == "a/b对比"


# ── 纯函数：预算 ───────────────────────────────────────────────────────────

def test_budget_exhausted_none_when_unset():
    """弱声明不制造误截停：没设上限 = 不限。"""
    assert budget_exhausted({}) == ""
    assert budget_exhausted(None) == ""
    assert budget_exhausted({"llm_calls_used": 99}) == ""


def test_budget_exhausted_deadline():
    now = 1_000_000.0
    assert budget_exhausted({"deadline_ts": now - 1}, now=now) == STOP_DEADLINE
    assert budget_exhausted({"deadline_ts": now + 10}, now=now) == ""


def test_budget_exhausted_call_caps():
    assert budget_exhausted({"llm_calls_max": 4, "llm_calls_used": 4}) == STOP_BUDGET
    assert budget_exhausted({"ext_calls_max": 20, "ext_calls_used": 21}) == STOP_BUDGET
    assert budget_exhausted({"llm_calls_max": 4, "llm_calls_used": 3}) == ""


def test_budget_exhausted_ignores_garbage_caps():
    assert budget_exhausted({"llm_calls_max": "abc", "llm_calls_used": 99}) == ""
    assert budget_exhausted({"deadline_ts": "nope"}) == ""


def test_merge_used_accumulates_and_keeps_ints():
    b = merge_used({"llm_calls_max": 4}, {"llm_calls": 1})
    assert b == {"llm_calls_max": 4, "llm_calls_used": 1}
    b = merge_used(b, {"llm_calls_used": 2, "ext_calls": 3})
    assert b["llm_calls_used"] == 3 and b["ext_calls_used"] == 3
    assert isinstance(b["llm_calls_used"], int)      # 整数不变成 3.0


def test_merge_used_never_touches_caps():
    b = merge_used({"llm_calls_max": 4, "deadline_ts": 123}, {"llm_calls": 1})
    assert b["llm_calls_max"] == 4 and b["deadline_ts"] == 123


# ── 纯函数：orphan 惰性判定 ───────────────────────────────────────────────

def test_is_orphaned_only_for_active_states():
    old = time.time() - _STALE
    for st in TERMINAL + (ORPHANED,):
        assert not is_orphaned(st, old, old, ttl=90)
    for st in ACTIVE:
        assert is_orphaned(st, old, old, ttl=90)


def test_is_orphaned_uses_created_at_when_never_beat():
    """刚 accepted 就崩：从没心跳过，以 created_at 为参照才判得出来。"""
    now = 1_000_000.0
    assert is_orphaned(ACCEPTED, 0.0, now - 200, now=now, ttl=90)
    assert not is_orphaned(ACCEPTED, 0.0, now - 10, now=now, ttl=90)


def test_is_orphaned_fresh_heartbeat_not_convicted():
    now = 1_000_000.0
    assert not is_orphaned(RUNNING, now - 5, now - 5000, now=now, ttl=90)


def test_orphan_ttl_env_override(monkeypatch):
    monkeypatch.setenv("LEDGER_ORPHAN_TTL_S", "30")
    assert orphan_ttl() == 30
    monkeypatch.setenv("LEDGER_ORPHAN_TTL_S", "garbage")
    assert orphan_ttl() == 90       # 垃圾值回默认，不炸


# ── 存储行为（in-memory）──────────────────────────────────────────────────

def _backdate(led: TaskLedger, task_id: str, ago: float = _STALE) -> None:
    """把账本里的行改老（模拟崩溃后遗留的孤儿；对应 origin 用假 SQL 行驱动）。"""
    stored = led._tasks[task_id]
    stored.heartbeat_at = time.time() - ago
    stored.created_at = time.time() - ago


async def test_open_creates_accepted_task_with_uuid_id():
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    assert isinstance(task, LedgerTask)
    assert task.status == ACCEPTED and task.active
    # task_id 必须 uuid4：id(obj) 会因 GC 地址复用撞键（origin 老教训）
    assert len(task.task_id) == 32 and task.task_id.isalnum()
    assert task.idempotency_key == idem_key("u1", "research", "固态电池")
    assert task.heartbeat_at > 0 and task.created_at > 0


async def test_open_returns_duplicate_on_active_same_goal():
    """连说两遍「慢慢查固态电池」→ 第二遍拿到 Duplicate，不重复开跑。"""
    led = TaskLedger()
    first = await led.open("u1", "s1", "researcher", "research", "固态电池")
    second = await led.open("u1", "s2", "researcher", "research", " 固态电池。 ")
    assert isinstance(second, Duplicate)
    assert second.existing.task_id == first.task_id
    assert len(led._tasks) == 1                     # 只有一单在账


async def test_open_different_user_or_kind_not_duplicate():
    led = TaskLedger()
    await led.open("u1", "s1", "researcher", "research", "固态电池")
    assert isinstance(await led.open("u2", "s1", "researcher", "research", "固态电池"),
                      LedgerTask)
    assert isinstance(await led.open("u1", "s1", "researcher", "trip", "固态电池"),
                      LedgerTask)


async def test_open_reclaims_orphan_and_proceeds():
    """幂等命中的是上次崩掉的尸体 → 就地改判 orphaned 后放行新开单，
    否则用户被一条永不销单的任务永久挡住重试。"""
    led = TaskLedger()
    dead = await led.open("u1", "s1", "researcher", "research", "固态电池")
    _backdate(led, dead.task_id)
    fresh = await led.open("u1", "s1", "researcher", "research", "固态电池")
    assert isinstance(fresh, LedgerTask) and fresh.task_id != dead.task_id
    assert led._tasks[dead.task_id].status == ORPHANED


async def test_open_can_separate_display_goal_from_idempotency_source():
    """规划槽位会波动，但同一用户原话必须仍命中同一个任务指纹。"""
    led = TaskLedger()
    raw = "慢慢查一下钠离子电池的产业化进展，查完告诉我"
    task = await led.open("u1", "s1", "researcher", "research",
                          "钠离子电池产业化进展", idempotency_goal=raw)
    assert task.goal == "钠离子电池产业化进展"
    assert task.idempotency_key == idem_key("u1", "research", raw)
    dup = await led.open("u1", "s1", "researcher", "research",
                         "钠电池产业化", idempotency_goal=raw)
    assert isinstance(dup, Duplicate)


async def test_terminal_task_does_not_block_reopen():
    """终态行要能共存：同一件事完成后可以再做一次（origin partial unique index 语义）。"""
    led = TaskLedger()
    first = await led.open("u1", "s1", "researcher", "research", "固态电池")
    await led.close(first.task_id, DONE)
    again = await led.open("u1", "s1", "researcher", "research", "固态电池")
    assert isinstance(again, LedgerTask) and again.task_id != first.task_id


async def test_heartbeat_missing_row_reports_cancelled():
    """行都没了（被清理/从没开成）→ 当作取消收尾，不空转。"""
    led = TaskLedger()
    assert await led.heartbeat("no-such-task") == CANCELLED


async def test_heartbeat_returns_cancelled_after_user_cancel():
    """拉模式的核心：用户 cancel 后，后台任务下一次心跳读到 cancelled 自行收尾。"""
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    assert await led.cancel(task.task_id) is True
    assert await led.heartbeat(task.task_id) == CANCELLED
    got = await led.get(task.task_id)
    assert got.stop_reason == STOP_USER


async def test_heartbeat_resurrects_orphan_to_running():
    """迟到心跳复活：orphaned 是判定不是结局，误判不该变成假的中断报告。"""
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    _backdate(led, task.task_id)
    assert await led.query_active("u1") == []                  # 惰性改判成孤儿
    assert led._tasks[task.task_id].status == ORPHANED
    assert await led.heartbeat(task.task_id, progress="检索中 3/9") == RUNNING
    assert led._tasks[task.task_id].status == RUNNING
    assert led._tasks[task.task_id].progress == "检索中 3/9"


async def test_heartbeat_enforces_budget_and_stops():
    """预算强制：累加后超上限 → 就地 cancelled + 写 stop_reason。"""
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池",
                          budget={"llm_calls_max": 2, "llm_calls_used": 1})
    assert await led.heartbeat(task.task_id, used={"llm_calls": 1}) == CANCELLED
    got = await led.get(task.task_id)
    assert got.status == CANCELLED
    assert got.stop_reason == STOP_BUDGET
    assert got.budget["llm_calls_used"] == 2


async def test_heartbeat_deadline_stops_with_deadline_reason():
    """超期与用户取消都返回 cancelled，靠 stop_reason 区分「超时停了」话术。"""
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池",
                          budget={"deadline_ts": time.time() - 1})
    assert await led.heartbeat(task.task_id) == CANCELLED
    assert (await led.get(task.task_id)).stop_reason == STOP_DEADLINE


async def test_heartbeat_under_budget_persists_usage_and_progress():
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池",
                          budget={"llm_calls_max": 9, "llm_calls_used": 1})
    assert await led.heartbeat(task.task_id, progress="step1",
                               used={"llm_calls": 1}) == RUNNING
    got = await led.get(task.task_id)
    assert got.budget["llm_calls_used"] == 2
    assert got.progress == "step1"
    # 空 progress 不清空已有进度（origin 的 CASE WHEN ''）
    assert await led.heartbeat(task.task_id) == RUNNING
    assert (await led.get(task.task_id)).progress == "step1"


async def test_close_rejects_non_terminal_status():
    led = TaskLedger()
    with pytest.raises(ValueError):
        await led.close("t1", RUNNING)


async def test_close_is_terminal_guarded():
    """终态不可逆：完成的任务不能被迟到的失败回调改写成 failed。"""
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    assert await led.close(task.task_id, DONE, result_ref={"sections": 9}) is True
    assert await led.close(task.task_id, FAILED) is False
    got = await led.get(task.task_id)
    assert got.status == DONE and got.result_ref == {"sections": 9}


async def test_close_missing_row_reports_false():
    led = TaskLedger()
    assert await led.close("no-such-task", DONE) is False


async def test_cancel_writes_stop_reason_and_guards_terminal():
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    assert await led.cancel(task.task_id, reason=STOP_DEADLINE) is True
    assert (await led.get(task.task_id)).stop_reason == STOP_DEADLINE
    assert await led.cancel(task.task_id) is False              # 已终态


async def test_query_active_drops_orphans_lazily():
    """查询侧惰性判定：孤儿不该出现在「还在跑」名单里。"""
    led = TaskLedger()
    alive = await led.open("u1", "s1", "researcher", "research", "固态电池")
    dead = await led.open("u1", "s1", "researcher", "research", "钠离子电池")
    _backdate(led, dead.task_id)
    tasks = await led.query_active("u1")
    assert [t.task_id for t in tasks] == [alive.task_id]
    assert led._tasks[dead.task_id].status == ORPHANED


async def test_query_active_filters_kind_and_excludes_terminal():
    led = TaskLedger()
    research = await led.open("u1", "s1", "researcher", "research", "固态电池")
    trip = await led.open("u1", "s1", "planner", "trip", "周末路线")
    done = await led.open("u1", "s1", "researcher", "research", "另一件事")
    await led.close(done.task_id, DONE)
    assert {t.task_id for t in await led.query_active("u1")} == \
        {research.task_id, trip.task_id}
    assert [t.task_id for t in await led.query_active("u1", kind="trip")] == \
        [trip.task_id]


async def test_recent_keeps_orphan_but_relabels():
    """「刚才那个任务怎么样了」要能答出「干到一半中断了」——recent 保留孤儿但改标。"""
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    _backdate(led, task.task_id)
    tasks = await led.recent("u1")
    assert len(tasks) == 1 and tasks[0].status == ORPHANED


async def test_recent_includes_terminal_and_orders_newest_first():
    led = TaskLedger()
    first = await led.open("u1", "s1", "researcher", "research", "固态电池")
    await led.close(first.task_id, DONE)
    led._tasks[first.task_id].created_at -= 10       # 保证时序可判
    second = await led.open("u1", "s1", "researcher", "research", "钠离子电池")
    tasks = await led.recent("u1")
    assert [t.task_id for t in tasks] == [second.task_id, first.task_id]
    assert tasks[1].status == DONE


async def test_get_relabels_orphan_and_returns_none_when_missing():
    led = TaskLedger()
    assert await led.get("no-such-task") is None
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    _backdate(led, task.task_id)
    got = await led.get(task.task_id)
    assert got is not None and got.status == ORPHANED


async def test_returned_tasks_are_snapshots():
    """读接口返回快照：调用方改返回值不得脏账本（origin row→dataclass 语义）。"""
    led = TaskLedger()
    task = await led.open("u1", "s1", "researcher", "research", "固态电池")
    task.status = "hacked"
    task.budget["llm_calls_max"] = 999
    stored = led._tasks[task.task_id]
    assert stored.status == ACCEPTED
    assert "llm_calls_max" not in stored.budget


async def test_init_ready_and_close_pool_surface():
    """API 兼容面：init/ready/close_pool 保留（in-memory 恒可用）。"""
    led = TaskLedger()
    assert led.ready is True
    assert await led.init() is True
    await led.close_pool()          # no-op，不抛
