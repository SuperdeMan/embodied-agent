"""Revocable in-process invocation authority, propagated by asyncio.to_thread.

This is a migration guard for registered skills, not device authentication or a
cross-process control lease. Revocation stops new writes; physical stopping must
be observed separately by an embodiment adapter.


可撤销的进程内调用许可经线程上下文传播；这是迁移保护，不是设备认证或跨进程租约。撤销仅阻止新写入，物理停止仍需本体独立观测。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class InvocationPermit:
    def __init__(self, timeout_s: float):
        self.deadline = time.monotonic() + timeout_s
        self._lock = threading.RLock()
        self._revoked = False

    def revoke(self) -> None:
        # 同步覆盖实际设备写入，不只做事前检查。
        # Synchronize with the actual device write, not only a preflight check.
        with self._lock:
            self._revoked = True

    @contextmanager
    def access(self) -> Iterator[bool]:
        with self._lock:
            yield not self._revoked and time.monotonic() < self.deadline


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
