"""Per-turn simulation recording outside the control thread. 在控制线程外按交互轮次录制仿真。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, replace

from embodied.data_engine.recorder import EpisodeRecorder, EpisodeWriter
from embodied.runtime.obs.redact import gate_content


@dataclass
class _TurnCapture:
    writer: EpisodeWriter | None = None
    accepting: bool = True
    dropped: int = 0
    error: str = ""


class InteractiveRecorder:
    """Route each control worker's samples to its originating turn. 按来源轮次分发控制工作线程的采样。"""

    def __init__(self, sim, *, source: str, seed: int, root=None, max_pending: int = 512):
        self.sim = sim
        self.recorder = EpisodeRecorder(root)
        self.embodiment = sim.spec().embodiment_id
        self.seed = seed
        self.source = source
        self._current: ContextVar[_TurnCapture | None] = ContextVar("interactive_episode", default=None)
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="episode-writer")
        self._slots = threading.BoundedSemaphore(max_pending)
        self._lock = threading.Lock()
        self._turns: set[asyncio.Event] = set()
        self._closed = False
        sim.set_hooks(self._step, self._guard)

    def _start(self, capture, text):
        try:
            capture.writer = self.recorder.start(gate_content(text), self.embodiment, seed=self.seed,
                                                 extra_meta={"source": self.source, "action_source": "driver_accepted"})
        except Exception as exc:
            capture.error = type(exc).__name__

    def _step(self, obs, action):
        if self._current.get() is not None:
            snapshot = replace(obs, objects=dict(obs.objects), extras={}, object_evidence=None)
            self._enqueue(EpisodeWriter.on_step, snapshot, replace(action) if action is not None else None)

    def _guard(self, event):
        self._enqueue(EpisodeWriter.on_event, "safety", {"kind": event.kind, "reason": gate_content(event.reason, 200)})

    def _enqueue(self, method, *args):
        capture = self._current.get()
        if capture is None:
            return
        with self._lock:
            if not capture.accepting:
                return
            if not self._slots.acquire(blocking=False):
                capture.dropped += 1
                return
            self._io.submit(self._write, capture, method, args)

    def _write(self, capture, method, args):
        try:
            if capture.writer is not None and not capture.error:
                method(capture.writer, *args)
        except Exception as exc:
            capture.error = type(exc).__name__
        finally:
            self._slots.release()

    def _finish(self, capture, turn):
        writer = capture.writer
        receipt = {"status": "unavailable", "episode_id": None, "dropped": capture.dropped}
        try:
            if writer is not None:
                receipt["episode_id"] = writer.path.name
                for step in getattr(turn, "results", ()):
                    writer.on_event("skill_end", {
                        "step": step.step_id, "status": step.status.value, "operation_id": step.operation_id,
                        "attempt_id": step.attempt_id, "detail": gate_content(step.detail or step.error, 200),
                    })
                writer.on_event("note", {"task_id": getattr(turn, "task_id", ""), "dropped": capture.dropped,
                                         "write_error": capture.error})
                if turn is None:
                    writer.abort("interactive turn interrupted")
                    receipt["status"] = "interrupted"
                else:
                    writer.finish_unjudged("interactive turn without a task judge")
                    receipt["status"] = "partial" if capture.error or capture.dropped else "complete"
        except Exception as exc:
            capture.error = type(exc).__name__
        finally:
            self._slots.release()
        if capture.error:
            receipt["write_error"] = capture.error
        return receipt

    async def turn(self, engine, text):
        if self._closed:
            raise RuntimeError("interactive recorder closed")
        if not self._slots.acquire(blocking=False):
            return await engine.turn(text), {"status": "unavailable", "reason": "recording_queue_full"}
        drained = asyncio.Event()
        self._turns.add(drained)
        capture = _TurnCapture()
        self._io.submit(self._start, capture, text)
        token = self._current.set(capture)
        turn = None
        try:
            turn = await engine.turn(text)
        finally:
            self._current.reset(token)
            with self._lock:
                capture.accepting = False
                finished = self._io.submit(self._finish, capture, turn)
            drained.set()
            self._turns.discard(drained)
            receipt = await asyncio.shield(asyncio.wrap_future(finished))
        return turn, receipt

    async def close(self):
        self._closed = True
        if self._turns:
            await asyncio.gather(*(done.wait() for done in tuple(self._turns)))
        self.sim.set_hooks(None, None)
        await asyncio.to_thread(self._io.shutdown, wait=True)
