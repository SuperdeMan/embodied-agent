"""Interactive recording, concurrent turns and missing evidence. 交互录制、并发轮次和缺失证据。"""

import asyncio
from types import SimpleNamespace

from embodied.cognition.plan import StepResult, StepStatus
from embodied.control.hal import ActionCommand, Observation, Pose
from embodied.data_engine.interactive import InteractiveRecorder
from embodied.data_engine.recorder import load_episode, replay_episode


class Sim:
    def spec(self):
        return SimpleNamespace(embodiment_id="sim.test")

    def set_hooks(self, on_step, on_guard_event):
        self.step_hook = on_step

    def sample(self, value):
        obs = Observation(t=value, qpos=(value,), qvel=(0.0,), gripper_opening=0.5, ee_pose=Pose((0, 0, 0)))
        self.step_hook(obs, ActionCommand((value,), 0.5))


async def test_concurrent_turns_keep_their_own_samples_and_receipts(tmp_path):
    sim = Sim()
    recorder = InteractiveRecorder(sim, source="console", seed=7, root=tmp_path)
    barrier = asyncio.Barrier(2)

    class Engine:
        async def turn(self, text):
            await barrier.wait()
            await asyncio.to_thread(sim.sample, float(text))
            step = StepResult("s1", StepStatus.OK, operation_id=f"op-{text}", attempt_id=f"attempt-{text}")
            return SimpleNamespace(task_id=f"task-{text}", results=[step])

    try:
        first, second = await asyncio.gather(*(recorder.turn(Engine(), str(n)) for n in (1, 2)))
        for n, (turn, receipt) in enumerate((first, second), start=1):
            assert receipt["status"] == "complete" and receipt["dropped"] == 0
            episode_dir = tmp_path / receipt["episode_id"]
            recorded = replay_episode(episode_dir, frame=0)
            assert recorded["ok"] and recorded["success"] is None
            assert recorded["frame"]["state"] == [float(n), 0.5]
            assert recorded["frame"]["action"] == [float(n), 0.5]
            assert [event["payload"].get("attempt_id") for event in recorded["events"]][0] == f"attempt-{n}"
            assert recorded["events"][-1]["payload"]["task_id"] == turn.task_id
            assert replay_episode(episode_dir, frame=1)["issues"] == ["frame_out_of_range"]
    finally:
        await recorder.close()
    assert sim.step_hook is None


async def test_queue_limit_and_storage_failure_report_capture_separately(tmp_path, monkeypatch):
    sim = Sim()
    recorder = InteractiveRecorder(sim, source="console", seed=0, root=tmp_path, max_pending=1)
    calls = []

    class Engine:
        async def turn(self, text):
            calls.append(text)
            await asyncio.to_thread(sim.sample, 0.5)
            return SimpleNamespace(task_id="one", results=[])

    try:
        _, partial = await recorder.turn(Engine(), "first")
        assert partial["status"] == "partial" and partial["dropped"] == 1
        assert load_episode(tmp_path / partial["episode_id"]).meta["success"] is None

        def unavailable(*args, **kwargs):
            raise OSError("disk unavailable")

        monkeypatch.setattr(recorder.recorder, "start", unavailable)
        turn, failed = await recorder.turn(Engine(), "second")
        assert turn.task_id == "one"
        assert failed["status"] == "unavailable" and failed["write_error"] == "OSError"
        assert calls == ["first", "second"]
    finally:
        await recorder.close()


async def test_cancel_retains_a_partial_recording_and_fences_late_samples(tmp_path):
    sim = Sim()
    recorder = InteractiveRecorder(sim, source="console", seed=2, root=tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    late = []

    class Engine:
        async def turn(self, text):
            await asyncio.to_thread(sim.sample, 0.5)

            async def delayed():
                await release.wait()
                await asyncio.to_thread(sim.sample, 1.0)

            late.append(asyncio.create_task(delayed()))
            started.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(recorder.turn(Engine(), "cancel"))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await asyncio.gather(*late)
        ep = load_episode(next(tmp_path.iterdir()))
        assert ep.meta["aborted"] and ep.timestamp.tolist() == [0.5]
    finally:
        await recorder.close()
