"""Scripted-expert collection — hermetic (fake sim, real registry, tmp dirs)."""

from __future__ import annotations

import json
from pathlib import Path

from embodied.control.hal import Observation, Pose
from embodied.data_engine.collect import COLLECTOR_ID, collect_episodes
from embodied.skills.manifest import ParamSpec, SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


def obs(t: float, cube=(0.16, -0.18, 0.02)) -> Observation:
    return Observation(
        t=t, qpos=(0.0,), qvel=(0.0,), gripper_opening=0.5, ee_pose=Pose(pos=(0, 0, 0.2)),
        objects={"obj_cube": Pose(pos=cube)},
        extras={"regions": {"bin_region": {"center": (0.16, -0.18, 0.045), "half": (0.045, 0.045, 0.04)}}},
    )


class FakeSim:
    """Sim time advances on every read() so boundary sim_t values are distinguishable."""

    def __init__(self):
        self.t = 0.0
        self.hooks: list[tuple] = []

    def spec(self):
        class S:
            embodiment_id = "sim.fake"

        return S()

    def reset(self, randomize=False):
        return obs(self.t)

    def read(self):
        self.t += 1.0
        return obs(self.t)

    def set_hooks(self, on_step=None, on_guard_event=None):
        self.hooks.append((on_step, on_guard_event))


def make_skill(name: str, calls: list[str], *, ok: bool = True) -> tuple[SkillManifest, object]:
    manifest = SkillManifest(
        name=name, description="fake",
        params={"object": ParamSpec(type="string", required=False, default="obj_cube")},
    )

    async def handler(object: str = "obj_cube") -> SkillResult:
        calls.append(name)
        return SkillResult(ok=ok, detail=f"{name} {'done' if ok else 'exploded'}")

    return manifest, handler


def read_events(path: str) -> list[dict]:
    lines = (Path(path) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def read_meta(path: str) -> dict:
    return json.loads((Path(path) / "meta.json").read_text(encoding="utf-8"))


async def test_happy_path_records_boundaries_and_judges(tmp_path):
    calls: list[str] = []
    registry = SkillRegistry()
    for n in ("skill.fake.pick", "skill.fake.place"):
        registry.register(*make_skill(n, calls))
    report = await collect_episodes(
        FakeSim(), registry, episodes=2, command="do the thing",
        skill_sequence=(("skill.fake.pick", {"object": "obj_cube"}), ("skill.fake.place", {})),
        judge=lambda snap: True, root=tmp_path, seed=3, progress=lambda s: None,
    )
    assert len(report.episodes) == 2 and report.successes == 2 and report.rate == 1.0
    assert calls == ["skill.fake.pick", "skill.fake.place"] * 2

    events = read_events(report.episodes[0]["path"])
    kinds = [(e["kind"], e["payload"].get("skill")) for e in events]
    assert kinds == [
        ("skill_start", "skill.fake.pick"), ("skill_end", "skill.fake.pick"),
        ("skill_start", "skill.fake.place"), ("skill_end", "skill.fake.place"),
    ]
    # sim_t is promoted to the top level and strictly increases across boundaries
    sim_ts = [e["sim_t"] for e in events]
    assert all(isinstance(t, float) for t in sim_ts)
    assert sim_ts == sorted(sim_ts) and len(set(sim_ts)) == len(sim_ts)

    meta = read_meta(report.episodes[0]["path"])
    assert meta["success"] is True and meta["aborted"] is False
    assert meta["task"] == "do the thing" and meta["seed"] == 3
    assert meta["extra_meta"]["collector"] == COLLECTOR_ID
    assert meta["extra_meta"]["skill_sequence"] == ["skill.fake.pick", "skill.fake.place"]


async def test_failed_skill_aborts_sequence_but_keeps_episode(tmp_path):
    calls: list[str] = []
    registry = SkillRegistry()
    registry.register(*make_skill("skill.fake.pick", calls, ok=False))
    registry.register(*make_skill("skill.fake.place", calls))
    report = await collect_episodes(
        FakeSim(), registry, episodes=1, command="c",
        skill_sequence=(("skill.fake.pick", {}), ("skill.fake.place", {})),
        judge=lambda snap: True, root=tmp_path, progress=lambda s: None,
    )
    assert calls == ["skill.fake.pick"]  # place never invoked after pick failed
    ep = report.episodes[0]
    assert ep["success"] is False and "skill.fake.pick" in ep["detail"]
    meta = read_meta(ep["path"])
    assert meta["success"] is False and meta["aborted"] is False  # kept, labeled, not aborted
    end = [e for e in read_events(ep["path"]) if e["kind"] == "skill_end"]
    assert len(end) == 1 and end[0]["payload"]["ok"] is False


async def test_judge_overrules_skill_self_report(tmp_path):
    calls: list[str] = []
    registry = SkillRegistry()
    registry.register(*make_skill("skill.fake.pick", calls))  # reports ok=True
    report = await collect_episodes(
        FakeSim(), registry, episodes=1, command="c",
        skill_sequence=(("skill.fake.pick", {}),),
        judge=lambda snap: False, root=tmp_path, progress=lambda s: None,
    )
    ep = report.episodes[0]
    assert ep["success"] is False and "judge" in ep["detail"]
    assert read_meta(ep["path"])["success"] is False


async def test_hooks_detached_after_each_episode(tmp_path):
    registry = SkillRegistry()
    registry.register(*make_skill("skill.fake.pick", []))
    sim = FakeSim()
    await collect_episodes(
        sim, registry, episodes=2, command="c",
        skill_sequence=(("skill.fake.pick", {}),),
        judge=lambda snap: True, root=tmp_path, progress=lambda s: None,
    )
    # attach, detach, attach, detach — and every detach clears both hooks
    assert len(sim.hooks) == 4
    assert sim.hooks[1] == (None, None) and sim.hooks[3] == (None, None)
