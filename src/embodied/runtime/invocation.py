"""Revocable in-process invocation authority, propagated by asyncio.to_thread.

This is a migration guard for registered skills, not device authentication or a
cross-process control lease. Revocation stops new writes; physical stopping must
be observed separately by an embodiment adapter.
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
        # Existing direct sim/teleop callers remain reference paths until Arbiter migration.
        yield True
    else:
        with permit.access() as allowed:
            yield allowed
