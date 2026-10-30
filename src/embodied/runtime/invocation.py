"""Revocable in-process invocation authority, propagated by asyncio.to_thread.

This is a migration guard for registered skills, not device authentication or a
cross-process control lease. Revocation stops new writes; physical stopping must
be observed separately by an embodiment adapter.


可撤销的进程内调用许可经线程上下文传播；这是迁移保护，不是设备认证或跨进程租约。撤销仅阻止新写入，物理停止仍需本体独立观测。
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import partial
from typing import Callable, Iterator


class InvocationPermit:
    def __init__(self, timeout_s: float, authority: Callable[[], bool] | None = None):
        self.deadline = time.monotonic() + timeout_s
        self._lock = threading.RLock()
        self._revoked = False
        self._authority = authority
        self._workers = 0
        self.handler_task: asyncio.Task | None = None

    def reserve_worker(self) -> None:
        with self._lock:
            if self._revoked or time.monotonic() >= self.deadline:
                raise RuntimeError("invocation_revoked_or_expired")
            self._workers += 1

    def finish_worker(self) -> None:
        with self._lock:
            self._workers -= 1

    @property
    def drained(self) -> bool:
        with self._lock:
            return self._workers == 0 and (self.handler_task is None or self.handler_task.done())

    def revoke(self) -> None:
        # 同步覆盖实际设备写入，不只做事前检查。
        # Synchronize with the actual device write, not only a preflight check.
        with self._lock:
            self._revoked = True

    @contextmanager
    def access(self) -> Iterator[bool]:
        with self._lock:
            yield (
                not self._revoked and time.monotonic() < self.deadline
                and (self._authority is None or self._authority())
            )


CURRENT_INVOCATION: ContextVar[InvocationPermit | None] = ContextVar("invocation_permit", default=None)


@contextmanager
def invocation_access() -> Iterator[bool]:
    permit = CURRENT_INVOCATION.get()
    if permit is None:
        # Arbiter 迁移前，直接仿真和遥操作仍作为参考路径。
        # Existing direct sim/teleop callers remain reference paths until Arbiter migration.
        yield True
    else:
        with permit.access() as allowed:
            yield allowed


async def run_blocking(function, *args, **kwargs):
    """Track actual worker completion even after coroutine cancellation.

    协程取消后仍追踪实际工作线程完成，排队任务也不能丢失退出记录。
    """
    permit = CURRENT_INVOCATION.get()
    if permit is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    permit.reserve_worker()
    call = partial(function, *args, **kwargs)

    def worker():
        try:
            with permit.access() as allowed:
                if not allowed:
                    raise RuntimeError("invocation_revoked_or_expired")
            return call()
        finally:
            permit.finish_worker()

    try:
        future = asyncio.get_running_loop().run_in_executor(None, copy_context().run, worker)
    except BaseException:
        permit.finish_worker()
        raise
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(future)
