"""One read-only worker, bounded waits, no replacement queue. 单只读工作线程，有界等待，不排替代队列。"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from concurrent.futures import Future, TimeoutError
from contextvars import Context

from embodied.runtime.invocation import OBSERVATION_ONLY

OBSERVATION_TIMEOUT_S = 1.0


class ObservationUnavailable(RuntimeError):
    pass


class ObservationLane:
    def __init__(self, timeout_s: float = OBSERVATION_TIMEOUT_S):
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 2.0:
            raise ValueError("observation timeout must be finite and in (0, 2]")
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._future: Future | None = None
        self._waiting = False
        self.started = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._waiting or self._future is not None and not self._future.done()

    def _submit(self, function, args, kwargs) -> Future:
        with self._lock:
            if self._waiting or self._future is not None and not self._future.done():
                raise ObservationUnavailable("observation_worker_busy")
            future = Future()
            self._future = future
            self._waiting = True
            self.started += 1

        def work():
            token = OBSERVATION_ONLY.set(True)
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                error = exc
                result = None
            else:
                error = None
            finally:
                OBSERVATION_ONLY.reset(token)
            # 工作及上下文清理完成后才标记结束；工作线程永不提交账本。
            # Complete only after user work and context cleanup; workers never commit the ledger.
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)

        try:
            threading.Thread(target=Context().run, args=(work,), name="observation-read", daemon=True).start()
        except BaseException as exc:
            future.set_exception(exc)
            with self._lock:
                self._waiting = False
            raise
        return future

    async def call(self, function, *args, **kwargs):
        deadline = time.monotonic() + self.timeout_s
        future = asyncio.wrap_future(self._submit(function, args, kwargs))
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                raise ObservationUnavailable("observation_timeout") from None
            if time.monotonic() >= deadline:
                raise ObservationUnavailable("observation_timeout")
            return result
        finally:
            with self._lock:
                self._waiting = False

    def call_sync(self, function, *args, **kwargs):
        deadline = time.monotonic() + self.timeout_s
        future = self._submit(function, args, kwargs)
        try:
            try:
                result = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                raise ObservationUnavailable("observation_timeout") from None
            if time.monotonic() >= deadline:
                raise ObservationUnavailable("observation_timeout")
            return result
        finally:
            with self._lock:
                self._waiting = False
